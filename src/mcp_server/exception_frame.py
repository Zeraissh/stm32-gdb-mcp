"""Reconstruct the Cortex-M auto-stacked exception frame.

When a HardFault (or any exception) is taken, hardware stacks eight words
``R0, R1, R2, R3, R12, LR, PC, xPSR`` onto the stack that was active before the
exception. The *real* faulting instruction address is the stacked ``PC`` — not
``$pc``, which points into the fault handler. Recovering it (and resolving it to
``file:line``) is the single most valuable step in diagnosing an embedded crash,
so this turns ``EXC_RETURN`` + SP into the decoded frame.

``EXC_RETURN`` (the value left in LR on exception entry) selects the stack and
frame type:
- bit 2 == 1  -> the Process Stack (PSP) was active; else the Main Stack (MSP)
- bit 4 == 0  -> an extended (FPU) frame was stacked; else the basic 8-word frame

``xPSR`` bit 9 indicates 4 bytes of alignment padding were inserted below the
stacked frame.
"""

STACKED_REGISTERS = ("R0", "R1", "R2", "R3", "R12", "LR", "PC", "xPSR")

EXC_RETURN_USE_PSP = 1 << 2
EXC_RETURN_BASIC_FRAME = 1 << 4
XPSR_STACK_ALIGN = 1 << 9


def _hex32(value):
    return f"0x{value & 0xFFFFFFFF:08x}"


def build_fault_context(gdb_client) -> dict:
    """Assemble a full crash-site report from a connected GDB client.

    Combines decoded fault registers, the reconstructed stacked exception frame,
    and the source location of the faulting (stacked) PC into one structured
    result. ``gdb_client`` must provide ``read_fault_registers``,
    ``read_register_value``, ``read_word``, and ``resolve_address``.
    """
    from .fault_analysis import diagnose_fault_registers

    fault_registers = gdb_client.read_fault_registers()
    diagnosis = diagnose_fault_registers(fault_registers)

    lr = gdb_client.read_register_value("$lr")
    msp = gdb_client.read_register_value("$msp")
    psp = gdb_client.read_register_value("$psp")
    frame = reconstruct_exception_frame(lr, msp, psp, gdb_client.read_word)

    faulting_location = gdb_client.resolve_address(frame["stacked_pc"])

    return {
        "diagnosis": diagnosis,
        "exception_frame": frame,
        "faulting_pc": frame["stacked_pc"],
        "faulting_location": faulting_location,
    }


def reconstruct_exception_frame(lr, msp, psp, read_word) -> dict:
    """Decode the stacked exception frame.

    ``lr`` is the EXC_RETURN value, ``msp``/``psp`` the two stack pointers, and
    ``read_word`` a callable ``(address) -> int`` returning the 32-bit word.
    """
    use_psp = bool(lr & EXC_RETURN_USE_PSP)
    fpu_extended = not (lr & EXC_RETURN_BASIC_FRAME)
    frame_sp = psp if use_psp else msp

    registers = {}
    for index, name in enumerate(STACKED_REGISTERS):
        registers[name] = read_word(frame_sp + index * 4)

    xpsr = registers["xPSR"]

    return {
        "active_stack": "PSP" if use_psp else "MSP",
        "frame_sp": frame_sp,
        "fpu_extended": fpu_extended,
        "stack_alignment_padding": bool(xpsr & XPSR_STACK_ALIGN),
        "registers": registers,
        "registers_hex": {name: _hex32(value) for name, value in registers.items()},
        "stacked_pc": _hex32(registers["PC"]),
        "stacked_lr": _hex32(registers["LR"]),
        "exc_return": _hex32(lr),
    }
