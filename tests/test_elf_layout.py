"""Issue #64: refuse ELF layouts GDB's load would flash incompletely."""

import struct

import pytest

from mcp_server.elf_layout import check_flashable, misplaced_load_sections
from mcp_server.error_taxonomy import classify_error
from mcp_server.gdb_client import GdbClientManager

_PT_LOAD, _SHT_PROGBITS, _SHT_STRTAB, _SHF_ALLOC = 1, 1, 3, 2


def _elf(segments, sections):
    """A minimal little-endian ELF32 image.

    ``segments`` are (p_offset, p_vaddr, p_paddr, p_filesz, p_memsz);
    ``sections`` are (name, sh_addr, sh_offset, sh_size). Contents are never
    read, so the file only needs to be long enough for the headers.
    """
    names = b"\0" + b"".join(n.encode() + b"\0" for n, *_ in sections) + b".shstrtab\0"
    phoff = 52
    strtab_off = phoff + 32 * len(segments)
    shoff = strtab_off + len(names)
    shnum = len(sections) + 2  # null + sections + .shstrtab
    header = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, phoff, shoff, 0,
                          52, 32, len(segments), 40, shnum, shnum - 1)
    phdrs = b"".join(struct.pack("<IIIIIIII", _PT_LOAD, o, v, p, fs, ms, 5, 4)
                     for o, v, p, fs, ms in segments)
    shdrs = bytes(40)
    name_at = 1
    for name, addr, offset, size in sections:
        shdrs += struct.pack("<IIIIIIIIII", name_at, _SHT_PROGBITS, _SHF_ALLOC,
                             addr, offset, size, 0, 0, 4, 0)
        name_at += len(name) + 1
    shdrs += struct.pack("<IIIIIIIIII", name_at, _SHT_STRTAB, 0, 0,
                         strtab_off, len(names), 0, 0, 1, 0)
    return header + phdrs + names + shdrs


# The reported Keil ARMCC5 AXF: one PT_LOAD at flash, 18376 bytes of file image
# holding ER_IROM1 (18280) and the 96-byte RW_IRAM1 initializer, whose section
# header names its SRAM run address.
_KEIL = _elf(
    segments=[(0x34, 0x08000000, 0x08000000, 18376, 18376)],
    sections=[("ER_IROM1", 0x08000000, 0x34, 18280),
              ("RW_IRAM1", 0x20000000, 0x34 + 18280, 96)],
)

# GCC: .data gets its own PT_LOAD with vaddr in RAM and paddr in flash, which BFD
# maps correctly.
_GCC = _elf(
    segments=[(0x10000, 0x08000000, 0x08000000, 0x4000, 0x4000),
              (0x20000, 0x20000000, 0x08004000, 0x60, 0x200)],
    sections=[(".text", 0x08000000, 0x10000, 0x4000),
              (".data", 0x20000000, 0x20000, 0x60)],
)


def test_keil_rw_initializer_is_flagged_with_its_flash_load_address():
    (found,) = misplaced_load_sections(_KEIL)

    assert found.name == "RW_IRAM1"
    assert found.vma == 0x20000000
    assert found.load_address == 0x08000000 + 18280
    assert found.size == 96


def test_gcc_data_section_layout_is_accepted():
    assert misplaced_load_sections(_GCC) == []


@pytest.mark.parametrize("data", [b"", b"not an elf", b"\x7fELF\x02" + bytes(60)])
def test_anything_unparsable_is_left_to_gdb(data):
    assert misplaced_load_sections(data) == []


def test_check_flashable_refuses_with_the_conversion_route(tmp_path):
    axf = tmp_path / "fw.axf"
    axf.write_bytes(_KEIL)

    with pytest.raises(RuntimeError) as excinfo:
        check_flashable(str(axf))

    message = str(excinfo.value)
    assert "RW_IRAM1 (96 bytes) belongs at 0x08004768" in message
    assert "fromelf --bin" in message
    assert classify_error(message)["code"] == "elf_layout_unsupported"


def test_check_flashable_ignores_a_missing_file(tmp_path):
    check_flashable(str(tmp_path / "missing.axf"))  # GDB reports this one itself


def test_load_firmware_refuses_before_any_gdb_command(tmp_path):
    axf = tmp_path / "fw.axf"
    axf.write_bytes(_KEIL)

    class RecordingGdb:
        def __init__(self):
            self.commands = []

        def write(self, command, timeout_sec=1.0):
            self.commands.append(command)
            return [{"type": "result", "message": "done", "payload": None}]

    manager = GdbClientManager()
    manager.gdb = RecordingGdb()

    with pytest.raises(RuntimeError, match="unsupported load layout"):
        manager.load_firmware(str(axf))

    assert manager.gdb.commands == [], "nothing may reach GDB, let alone the flash"
