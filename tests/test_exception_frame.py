from mcp_server.exception_frame import reconstruct_exception_frame


def _fake_memory(base, words):
    """Return a read_word(addr) backed by a list of 32-bit words at base."""
    mem = {base + i * 4: w for i, w in enumerate(words)}

    def read_word(address):
        return mem[address]

    return read_word


STACKED = [
    0x00000001,  # R0
    0x00000002,  # R1
    0x00000003,  # R2
    0x00000004,  # R3
    0x0000000C,  # R12
    0x08000123,  # LR
    0x08001234,  # PC (faulting return address)
    0x61000200,  # xPSR (bit 9 set -> stack alignment padding)
]


def test_basic_frame_on_msp_decodes_registers_and_pc():
    read_word = _fake_memory(0x20001000, STACKED)

    frame = reconstruct_exception_frame(
        lr=0xFFFFFFF9,  # EXC_RETURN: bit2=0 -> MSP, bit4=1 -> basic frame
        msp=0x20001000,
        psp=0x20002000,
        read_word=read_word,
    )

    assert frame["active_stack"] == "MSP"
    assert frame["frame_sp"] == 0x20001000
    assert frame["fpu_extended"] is False
    assert frame["registers"]["R0"] == 0x00000001
    assert frame["registers"]["PC"] == 0x08001234
    assert frame["registers"]["LR"] == 0x08000123
    assert frame["stacked_pc"] == "0x08001234"


def test_frame_on_psp_when_exc_return_bit2_set():
    read_word = _fake_memory(0x20002000, STACKED)

    frame = reconstruct_exception_frame(
        lr=0xFFFFFFFD,  # bit2=1 -> PSP
        msp=0x20001000,
        psp=0x20002000,
        read_word=read_word,
    )

    assert frame["active_stack"] == "PSP"
    assert frame["frame_sp"] == 0x20002000


def test_extended_frame_flag_from_exc_return_bit4():
    read_word = _fake_memory(0x20001000, STACKED)

    frame = reconstruct_exception_frame(
        lr=0xFFFFFFE9,  # bit4=0 -> extended (FPU) frame
        msp=0x20001000,
        psp=0x20002000,
        read_word=read_word,
    )

    assert frame["fpu_extended"] is True


def test_alignment_padding_detected_from_xpsr_bit9():
    read_word = _fake_memory(0x20001000, STACKED)  # xPSR 0x61000200 has bit9 set

    frame = reconstruct_exception_frame(
        lr=0xFFFFFFF9, msp=0x20001000, psp=0x20002000, read_word=read_word
    )

    assert frame["stack_alignment_padding"] is True


def test_build_fault_context_combines_diagnosis_frame_and_location():
    from mcp_server.exception_frame import build_fault_context

    read_word = _fake_memory(0x20001000, STACKED)

    class FakeClient:
        def read_fault_registers(self):
            # PRECISERR + BFARVALID -> a BusFault with a valid address
            return {"CFSR": (1 << 9) | (1 << 15), "HFSR": 0, "BFAR": 0x40001000, "MMFAR": 0}

        def read_register_value(self, expr):
            return {"$lr": 0xFFFFFFF9, "$msp": 0x20001000, "$psp": 0x20002000}[expr]

        def read_word(self, address):
            addr = int(address, 0) if isinstance(address, str) else address
            return read_word(addr)

        def resolve_address(self, expr):
            return [{"payload": f"Line 73 of main.c starts at address {expr}"}]

    context = build_fault_context(FakeClient())

    assert "BusFault" in context["diagnosis"]["fault_classes"]
    assert context["faulting_pc"] == "0x08001234"
    assert context["exception_frame"]["active_stack"] == "MSP"
    assert context["faulting_location"][0]["payload"].startswith("Line 73")
