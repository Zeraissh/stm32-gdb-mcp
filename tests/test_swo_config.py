from mcp_server import swo_config


def test_swo_prescaler_exact_divisor():
    # 80 MHz / 2 MHz = 40 -> prescaler 39, exact
    prescaler, achieved, exact = swo_config.swo_prescaler(80_000_000, 2_000_000)
    assert prescaler == 39
    assert achieved == 2_000_000
    assert exact is True


def test_swo_prescaler_rounds_and_reports_inexact():
    prescaler, achieved, exact = swo_config.swo_prescaler(80_000_000, 3_000_000)
    # nearest divisor is 27 -> ~2.96 MHz, not exact
    assert prescaler == 26
    assert exact is False
    assert achieved == 80_000_000 // 27


def test_itm_tpiu_setup_writes_enables_trace_and_stimulus_port():
    writes = dict(swo_config.itm_tpiu_setup_writes(80_000_000, 2_000_000, port=0))
    assert writes[swo_config.DEMCR] == swo_config.DEMCR_TRCENA          # trace on
    assert writes[swo_config.TPIU_SPPR] == swo_config.TPIU_SPPR_SWO_NRZ  # SWO UART mode
    assert writes[swo_config.TPIU_ACPR] == 39                            # prescaler
    assert writes[swo_config.ITM_LAR] == swo_config.ITM_LAR_UNLOCK       # unlock
    assert writes[swo_config.ITM_TER] == 0b1                             # port 0 enabled


def test_itm_setup_enables_the_requested_port():
    writes = dict(swo_config.itm_tpiu_setup_writes(80_000_000, 2_000_000, port=3))
    assert writes[swo_config.ITM_TER] == (1 << 3)


def test_openocd_commands_carry_clock_baud_and_output():
    cmds = swo_config.openocd_swo_capture_commands(80_000_000, 2_000_000, "swo.log", port=0)
    joined = " ".join(cmds)
    assert "traceclk 80000000" in joined
    assert "pin-freq 2000000" in joined
    assert "-output swo.log" in joined
    assert "itm port 0 on" in joined


def test_retarget_snippet_mentions_itm_sendchar():
    assert "ITM_SendChar" in swo_config.printf_retarget_snippet()
