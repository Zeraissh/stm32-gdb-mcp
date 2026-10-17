"""Detect ELF load layouts that GDB's ``load`` would flash incompletely.

GDB flashes each loadable section at its LMA, and BFD derives a section's LMA
from the program header that contains it only when the section's VMA ALSO falls
inside that segment's address range. Keil ARMCC5 armlink emits a single PT_LOAD
at the flash address whose file image carries both ER_IROM1 and the RW_IRAM1
initializer bytes, while RW_IRAM1's section header names its SRAM run address.
The VMA check fails, BFD falls back to LMA = VMA, and GDB writes those bytes to
SRAM instead of flash: the scatter-load initializer region is left unprogrammed
and flash_firmware reported success (issue #64, measured on hardware: a full
readback differed from the fromelf BIN exactly over the final 96 bytes).

This module only reads headers with ``struct``; it adds no dependency.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

_PT_LOAD = 1
_SHT_PROGBITS = 1
_SHF_ALLOC = 0x2


@dataclass(frozen=True)
class MisplacedSection:
    """A section whose file bytes belong in a load segment it will not be flashed to."""

    name: str
    vma: int
    load_address: int
    size: int


def _segments_and_sections(data: bytes):
    if len(data) < 52 or data[:4] != b"\x7fELF":
        return None
    if data[4] != 1:  # ELFCLASS32 only: every Cortex-M image is 32-bit
        return None
    endian = "<" if data[5] == 1 else ">"
    (e_phoff, e_shoff) = struct.unpack_from(endian + "II", data, 28)
    (e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx) = struct.unpack_from(
        endian + "HHHHH", data, 42
    )

    segments = []
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        if off + 32 > len(data):
            return None
        p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz = struct.unpack_from(
            endian + "IIIIII", data, off
        )
        if p_type == _PT_LOAD:
            segments.append((p_offset, p_vaddr, p_paddr, p_filesz, p_memsz))

    headers = []
    for i in range(e_shnum):
        off = e_shoff + i * e_shentsize
        if off + 40 > len(data):
            return None
        sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size = struct.unpack_from(
            endian + "IIIIII", data, off
        )
        headers.append((sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size))

    strtab = b""
    if 0 <= e_shstrndx < len(headers):
        _, _, _, _, str_off, str_size = headers[e_shstrndx]
        strtab = data[str_off:str_off + str_size]

    def name_of(index: int) -> str:
        end = strtab.find(b"\0", index)
        return strtab[index:end if end >= 0 else None].decode("latin-1", "replace")

    sections = [
        (name_of(sh_name), sh_addr, sh_offset, sh_size)
        for sh_name, sh_type, sh_flags, sh_addr, sh_offset, sh_size in headers
        if sh_type == _SHT_PROGBITS and sh_flags & _SHF_ALLOC and sh_size
    ]
    return segments, sections


def misplaced_load_sections(data: bytes) -> list[MisplacedSection]:
    """Sections GDB would load at their run address instead of their load address.

    A section qualifies when its file bytes sit inside a PT_LOAD's file image but
    its VMA lies outside that segment's address range, and the address the bytes
    occupy in the segment differs from that VMA. Anything that is not a parsable
    32-bit ELF yields an empty list, leaving the verdict to GDB.
    """
    parsed = _segments_and_sections(data)
    if parsed is None:
        return []
    segments, sections = parsed
    found = []
    for name, vma, offset, size in sections:
        holders = [
            seg for seg in segments
            if seg[0] <= offset and offset + size <= seg[0] + seg[3]
        ]
        # Any holder whose address range covers the VMA lets BFD map it correctly
        # (the GCC .data layout: its own PT_LOAD with vaddr in RAM, paddr in flash).
        if not holders or any(
            p_vaddr <= vma and vma + size <= p_vaddr + p_memsz
            for _, p_vaddr, _, _, p_memsz in holders
        ):
            continue
        p_offset, _, p_paddr, _, _ = holders[0]
        load_address = p_paddr + (offset - p_offset)
        if load_address != vma:
            found.append(MisplacedSection(name, vma, load_address, size))
    return found


def check_flashable(path: str) -> None:
    """Raise if GDB would flash ``path`` incompletely; return quietly otherwise.

    An unreadable file is left for GDB to report, so its own error wording (and
    the elf_load_failed classification) is unchanged.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return
    misplaced = misplaced_load_sections(data)
    if not misplaced:
        return
    detail = "; ".join(
        f"{s.name} ({s.size} bytes) belongs at {s.load_address:#010x} but GDB would write "
        f"it to its run address {s.vma:#010x}"
        for s in misplaced
    )
    raise RuntimeError(
        f"Refusing to flash {path}: unsupported load layout (scatter-load initializer "
        f"outside its segment's address range). {detail}. GDB's load would leave that "
        "flash range unprogrammed and the RW data uninitialized after reset. This is the "
        "Keil ARMCC5 armlink layout; convert it with "
        "'fromelf --bin --output fw.bin <file>.axf' and program the BIN at the flash base "
        "(e.g. STM32CubeProgrammer, or OpenOCD 'program fw.bin 0x08000000 verify'), then "
        "load_symbols with the AXF to debug."
    )
