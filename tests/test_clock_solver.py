from mcp_server.clock_solver import (
    render_system_clock_config,
    resolve_profile,
    solve_clock_tree,
    summarize_clock_solution,
)

MHZ = 1_000_000


def _assert_invariants(profile, solution):
    """Every returned solution must satisfy the device's documented constraints."""
    pll = profile["pll"]
    assert solution["sysclk_hz"] <= profile["max_sysclk_hz"]
    assert solution["hclk_hz"] <= profile["max_hclk_hz"]
    assert solution["pclk1_hz"] <= profile["max_pclk1_hz"]
    assert solution["pclk2_hz"] <= profile["max_pclk2_hz"]
    if solution["pll"]:
        p = solution["pll"]
        assert pll["m"][0] <= p["m"] <= pll["m"][1]
        assert pll["n"][0] <= p["n"] <= pll["n"][1]
        assert p["sysclk_div"] in pll["sysclk_div_set"]
        assert pll["vco_in_hz"][0] <= p["vco_in_hz"] <= pll["vco_in_hz"][1]
        assert pll["vco_out_hz"][0] <= p["vco_out_hz"] <= pll["vco_out_hz"][1]
        # The PLL actually produces the requested SYSCLK.
        assert p["vco_out_hz"] // p["sysclk_div"] == solution["sysclk_hz"]


# --- golden anchors (high-confidence vendor-equivalent configs) --------------


def test_f407_hse8_to_168_with_usb():
    profile = resolve_profile("STM32F407VG", "STM32F4")
    result = solve_clock_tree(profile, {
        "source": "HSE", "source_hz": 8 * MHZ, "target_sysclk_hz": 168 * MHZ, "need_48mhz": True})

    assert result["feasible"] is True
    s = result["solution"]
    _assert_invariants(profile, s)
    assert (s["pll"]["m"], s["pll"]["n"], s["pll"]["sysclk_div"], s["pll"]["q"]) == (4, 168, 2, 7)
    assert s["usb_48_ok"] is True
    assert s["clk48_hz"] == 48 * MHZ
    assert s["pclk1_hz"] == 42 * MHZ  # APB1 max 42 MHz
    assert s["pclk2_hz"] == 84 * MHZ  # APB2 max 84 MHz
    assert s["flash_latency"] == 5
    assert s["sysclk_pll_field"] == "P"


def test_l431_hsi16_to_80():
    profile = resolve_profile("STM32L431CBT6", "STM32L4")
    result = solve_clock_tree(profile, {"source": "HSI", "target_sysclk_hz": 80 * MHZ})

    assert result["feasible"] is True
    s = result["solution"]
    _assert_invariants(profile, s)
    # Textbook L4 80 MHz: HSI16 / 1 * 10 / 2.
    assert (s["pll"]["m"], s["pll"]["n"], s["pll"]["sysclk_div"]) == (1, 10, 2)
    assert s["sysclk_pll_field"] == "R"
    assert s["flash_latency"] == 4
    assert s["pclk1_hz"] == 80 * MHZ and s["pclk2_hz"] == 80 * MHZ


def test_f411_hsi16_to_100():
    profile = resolve_profile("STM32F411RE", "STM32F4")
    result = solve_clock_tree(profile, {"source": "HSI", "target_sysclk_hz": 100 * MHZ})

    assert result["feasible"] is True
    s = result["solution"]
    _assert_invariants(profile, s)
    assert s["sysclk_hz"] == 100 * MHZ
    assert s["flash_latency"] == 3
    assert s["pclk1_hz"] <= 50 * MHZ  # APB1 max 50 MHz on F411


def test_f401_hse8_to_84():
    profile = resolve_profile("STM32F401RE", "STM32F4")
    result = solve_clock_tree(profile, {
        "source": "HSE", "source_hz": 8 * MHZ, "target_sysclk_hz": 84 * MHZ})

    assert result["feasible"] is True
    _assert_invariants(profile, result["solution"])
    assert result["solution"]["flash_latency"] == 2


# --- honest failures / edge cases -------------------------------------------


def test_target_above_max_is_infeasible():
    profile = resolve_profile("STM32L431CB", "STM32L4")
    result = solve_clock_tree(profile, {"source": "HSI", "target_sysclk_hz": 200 * MHZ})

    assert result["feasible"] is False
    assert result["solution"] is None
    assert result["unresolved"][0]["type"] == "target_exceeds_max_sysclk"


def test_hse_without_frequency_is_unresolved():
    profile = resolve_profile("STM32F407", "STM32F4")
    result = solve_clock_tree(profile, {"source": "HSE", "target_sysclk_hz": 100 * MHZ})

    assert result["feasible"] is False
    assert result["unresolved"][0]["type"] == "hse_frequency_unknown"


def test_missing_target_is_unresolved():
    profile = resolve_profile("STM32F407", "STM32F4")
    result = solve_clock_tree(profile, {"source": "HSI"})

    assert result["feasible"] is False
    assert result["unresolved"][0]["type"] == "target_missing"


def test_usb_requested_but_unreachable_is_noted_not_faked():
    profile = resolve_profile("STM32F411", "STM32F4")
    result = solve_clock_tree(profile, {"source": "HSI", "target_sysclk_hz": 100 * MHZ, "need_48mhz": True})

    # A SYSCLK solution is still returned; USB just cannot be exact from this VCO.
    assert result["feasible"] is True
    assert result["solution"]["usb_48_ok"] is False
    assert result["solution"]["clk48_hz"] is None
    assert any("48 MHz" in n for n in result["notes"])


def test_direct_source_when_target_equals_source():
    profile = resolve_profile("STM32L431", "STM32L4")
    result = solve_clock_tree(profile, {"source": "HSI", "target_sysclk_hz": 16 * MHZ})

    assert result["feasible"] is True
    assert result["solution"]["use_pll"] is False
    assert result["solution"]["pll"] is None
    assert any("directly" in n for n in result["notes"])


# --- profile resolution is honest -------------------------------------------


def test_resolve_profile_known_and_unknown():
    assert resolve_profile("STM32F407VG", "STM32F4")["max_sysclk_hz"] == 168 * MHZ
    assert resolve_profile("STM32F401", "STM32F4")["max_sysclk_hz"] == 84 * MHZ
    assert resolve_profile("STM32L476", "STM32L4")["sysclk_pll_field"] == "R"
    # Unknown / unmodelled devices resolve to None (never guessed).
    assert resolve_profile("STM32H750", "STM32H7") is None
    assert resolve_profile("STM32L4R5", "STM32L4") is None  # L4+ (120 MHz) differs
    assert resolve_profile(None, None) is None


# --- rendering: concrete, correct, pure ASCII -------------------------------


def _render(part, family, request):
    profile = resolve_profile(part, family)
    solution = solve_clock_tree(profile, request)["solution"]
    return "\n".join(render_system_clock_config(solution))


def test_render_f4_is_concrete_and_ascii():
    code = _render("STM32F407VG", "STM32F4",
                   {"source": "HSE", "source_hz": 8 * MHZ, "target_sysclk_hz": 168 * MHZ, "need_48mhz": True})

    assert "void SystemClock_Config(void)" in code
    assert "RCC_OscInitStruct.PLL.PLLM = 4;" in code
    assert "RCC_OscInitStruct.PLL.PLLN = 168;" in code
    assert "RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;" in code
    assert "RCC_OscInitStruct.PLL.PLLQ = 7;" in code
    assert "RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;" in code
    assert "FLASH_LATENCY_5" in code
    assert code.isascii()


def test_render_l4_uses_pllr_and_is_ascii():
    code = _render("STM32L431CB", "STM32L4", {"source": "HSI", "target_sysclk_hz": 80 * MHZ})

    assert "RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;" in code
    assert "RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;" in code
    assert "FLASH_LATENCY_4" in code
    assert code.isascii()


def test_render_direct_source_disables_pll():
    code = _render("STM32L431", "STM32L4", {"source": "HSI", "target_sysclk_hz": 16 * MHZ})

    assert "RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;" in code
    assert "RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_HSI;" in code
    assert code.isascii()


def test_summarize_reports_frequencies():
    profile = resolve_profile("STM32F407", "STM32F4")
    result = solve_clock_tree(profile, {
        "source": "HSE", "source_hz": 8 * MHZ, "target_sysclk_hz": 168 * MHZ, "need_48mhz": True})
    view = summarize_clock_solution(result)

    assert view["feasible"] is True
    assert view["sysclk_mhz"] == 168.0
    assert view["usb_48_ok"] is True
    assert "M=4" in view["pll"]
