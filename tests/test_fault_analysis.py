from mcp_server.fault_analysis import diagnose_fault_registers


def test_diagnose_fault_registers_decodes_bus_fault_with_bfar():
    registers = {
        "CFSR": 0x00008200,
        "HFSR": 0x40000000,
        "BFAR": 0x2000FFF0,
        "MMFAR": 0,
    }

    diagnosis = diagnose_fault_registers(registers)

    assert diagnosis["fault_classes"] == ["BusFault"]
    assert "PRECISERR" in diagnosis["active_flags"]
    assert "BFARVALID" in diagnosis["active_flags"]
    assert diagnosis["fault_addresses"]["BFAR"] == "0x2000fff0"
    assert "forced escalation" in diagnosis["summary"].lower()


def test_diagnose_fault_registers_decodes_usage_fault_causes():
    registers = {
        "CFSR": 0x03000000,
        "HFSR": 0,
        "BFAR": 0,
        "MMFAR": 0,
    }

    diagnosis = diagnose_fault_registers(registers)

    assert diagnosis["fault_classes"] == ["UsageFault"]
    assert "UNALIGNED" in diagnosis["active_flags"]
    assert "DIVBYZERO" in diagnosis["active_flags"]
    assert diagnosis["fault_addresses"] == {}
