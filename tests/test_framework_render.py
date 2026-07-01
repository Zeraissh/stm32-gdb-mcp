from mcp_server.framework_render import render_framework
from mcp_server.framework_solver import build_framework_plan


def _fn(peripheral, signal):
    return {"peripheral": peripheral, "signal": signal}


def _pin(package_pin, port_pin, net, function=None):
    return {"package_pin": package_pin, "port_pin": port_pin, "net": net, "function": function}


def _board(pins, line="STM32L431", family="STM32L4"):
    return {
        "source": "<test>", "format": "kicad",
        "mcu": {"ref": "U1", "part": "STM32L431CBT6", "part_normalized": "STM32L431CBT6",
                "family": family, "line": line, "pins": pins},
        "power_nets": {"power": ["+3V3"], "ground": ["GND"]}, "nets": [],
    }


def _pins():
    return [
        _pin("42", "PA9", "/USART1_TX", _fn("USART1", "TX")),
        _pin("43", "PA10", "/USART1_RX", _fn("USART1", "RX")),
        _pin("22", "PB6", "/I2C1_SCL", _fn("I2C1", "SCL")),
        _pin("23", "PB7", "/I2C1_SDA", _fn("I2C1", "SDA")),
        _pin("15", "PA0", "/ADC1_IN5", _fn("ADC1", "IN5")),
        _pin("46", "PA13", "/SWDIO", _fn("SWD", "SWDIO")),
    ]


def _render(design=None, af_map=None):
    plan = build_framework_plan(_board(_pins()), design=design, af_map=af_map)
    result = render_framework(plan)
    source = next(f["content"] for f in result["files"] if f["path"] == "bsp_init.c")
    header = next(f["content"] for f in result["files"] if f["path"] == "bsp_init.h")
    return result, source, header


def test_render_emits_two_c_files():
    result, _, _ = _render()
    assert {f["path"] for f in result["files"]} == {"bsp_init.c", "bsp_init.h"}
    assert all(f["language"] == "c" for f in result["files"])


def test_source_enables_clocks_and_configures_pins():
    _, source, _ = _render()

    assert "__HAL_RCC_GPIOA_CLK_ENABLE();" in source
    assert "__HAL_RCC_GPIOB_CLK_ENABLE();" in source
    assert "__HAL_RCC_USART1_CLK_ENABLE();" in source
    assert "GPIO_InitStruct.Pin = GPIO_PIN_9;" in source
    assert "GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;" in source
    assert "HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);" in source


def test_i2c_pin_is_open_drain_and_adc_pin_is_analog():
    _, source, _ = _render()

    assert "GPIO_InitStruct.Mode = GPIO_MODE_AF_OD;" in source   # I2C SCL/SDA
    assert "GPIO_InitStruct.Mode = GPIO_MODE_ANALOG;" in source  # ADC IN5


def test_unknown_af_becomes_todo_not_a_number():
    _, source, _ = _render()

    assert "TODO: GPIO_InitStruct.Alternate for USART1_TX" in source
    assert "GPIO_InitStruct.Alternate = GPIO_AF" not in source  # nothing fabricated


def test_af_map_emits_concrete_alternate():
    af_map = {"STM32L431": {"PA9": {"USART1_TX": 7}, "PA10": {"USART1_RX": 7}}}
    _, source, _ = _render(af_map=af_map)

    assert "GPIO_InitStruct.Alternate = GPIO_AF7_USART1;" in source
    assert "TODO: GPIO_InitStruct.Alternate for USART1_TX" not in source


def test_design_config_renders_hal_init_fields():
    design = {"USART1": {"baud": 115200, "word_length": "UART_WORDLENGTH_8B"}}
    _, source, _ = _render(design=design)

    assert "huart1.Instance = USART1;" in source
    assert "huart1.Init.BaudRate = 115200;" in source
    assert "huart1.Init.WordLength = UART_WORDLENGTH_8B;" in source
    assert "if (HAL_UART_Init(&huart1) != HAL_OK)" in source


def test_missing_config_becomes_todo():
    _, source, _ = _render()  # no design supplied
    # Mandatory fields are still filled from HAL defaults (a complete, valid struct)...
    assert "huart1.Init.WordLength = UART_WORDLENGTH_8B;" in source
    # ...but the genuine design decision (baud) stays an honest TODO, never guessed.
    assert "TODO: set huart1.Init.BaudRate" in source


def test_header_declares_handles_and_prototypes():
    design = {"USART1": {"baud": 115200}}
    _, _, header = _render(design=design)

    assert "extern UART_HandleTypeDef huart1;" in header
    assert "void BSP_Init(void);" in header
    assert "void MX_USART1_UART_Init(void);" in header
    assert "void MX_GPIO_Init(void);" in header


def test_bsp_init_calls_in_dependency_order():
    _, source, _ = _render()
    body = source.split("void BSP_Init(void)", 1)[1].split("}", 1)[0]

    clk = body.index("SystemClock_Config();")
    gpio = body.index("MX_GPIO_Init();")
    usart = body.index("MX_USART1_UART_Init();")
    assert clk < gpio < usart


def test_todo_count_matches_markers():
    result, source, header = _render()
    assert result["todo_count"] == source.count("TODO") + header.count("TODO")
    assert result["todo_count"] > 0  # unresolved AF + missing config + clock stub


def test_debug_pins_not_configured():
    _, source, _ = _render()
    assert "SWDIO" not in source  # PA13 debug pin excluded


def test_generated_code_is_pure_ascii():
    result, _, _ = _render(design={"USART1": {"baud": 115200}})
    for f in result["files"]:
        f["content"].encode("ascii")  # raises if any non-ASCII leaked into generated C


def _render_with_clock(request):
    from mcp_server.clock_solver import resolve_profile, solve_clock_tree
    plan = build_framework_plan(_board(_pins()), design={"USART1": {"baud": 115200}})
    profile = resolve_profile(plan["mcu"]["line"], plan["mcu"]["family"])
    plan["clock_config"] = solve_clock_tree(profile, request)["solution"]
    result = render_framework(plan)
    source = next(f["content"] for f in result["files"] if f["path"] == "bsp_init.c")
    return result, source


def test_clock_config_renders_real_system_clock():
    result, source = _render_with_clock({"source": "HSI", "target_sysclk_hz": 80_000_000})

    # The honest TODO stub is gone; a concrete configuration is emitted instead.
    assert "TODO: configure the clock tree" not in source
    assert "RCC_OscInitStruct.PLL.PLLN = 10;" in source
    assert "RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;" in source
    assert "FLASH_LATENCY_4" in source
    assert "HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4)" in source


def test_clock_config_generated_code_is_pure_ascii():
    result, _ = _render_with_clock({"source": "HSI", "target_sysclk_hz": 80_000_000})
    for f in result["files"]:
        f["content"].encode("ascii")


def test_clock_stub_remains_without_clock_config():
    _, source, _ = _render()
    assert "TODO: configure the clock tree" in source
    assert "RCC_OscInitTypeDef" not in source


# --- NVIC interrupt backbone (Pillar D Tier 3) ------------------------------


def _render_pins(pins, design):
    plan = build_framework_plan(_board(pins), design=design)
    result = render_framework(plan)
    source = next(f["content"] for f in result["files"] if f["path"] == "bsp_init.c")
    return result, source


def test_nvic_renders_setpriority_enable_and_isr():
    _, source, _ = _render(design={"USART1": {"nvic_priority": 5}})
    assert "HAL_NVIC_SetPriority(USART1_IRQn, 5, 0);" in source
    assert "HAL_NVIC_EnableIRQ(USART1_IRQn);" in source
    # A dispatching ISR is generated for the enabled vector.
    assert "void USART1_IRQHandler(void)" in source
    assert "HAL_UART_IRQHandler(&huart1);" in source


def test_nvic_default_priority_carries_review_note():
    _, source, _ = _render(design={"USART1": {"nvic": True}})
    assert "HAL_NVIC_SetPriority(USART1_IRQn, 5, 0);" in source
    assert "default priority" in source


def test_nvic_i2c_emits_both_event_and_error_vectors():
    _, source, _ = _render(design={"I2C1": {"nvic": True}})
    assert "HAL_NVIC_EnableIRQ(I2C1_EV_IRQn);" in source
    assert "HAL_NVIC_EnableIRQ(I2C1_ER_IRQn);" in source
    assert "void I2C1_EV_IRQHandler(void)" in source
    assert "HAL_I2C_EV_IRQHandler(&hi2c1);" in source
    assert "void I2C1_ER_IRQHandler(void)" in source
    assert "HAL_I2C_ER_IRQHandler(&hi2c1);" in source


def test_nvic_unresolved_vector_renders_todo_not_a_guess():
    tim_pins = [_pin("10", "PA8", "/TIM1_CH1", _fn("TIM1", "CH1"))]
    _, source = _render_pins(tim_pins, {"TIM1": {"nvic": True}})
    assert "TODO: enable TIM1 interrupt" in source
    assert "HAL_NVIC_EnableIRQ" not in source  # nothing guessed


def test_nvic_generated_code_is_pure_ascii():
    result, _, _ = _render(design={"USART1": {"nvic": True}, "I2C1": {"nvic_priority": 3}})
    for f in result["files"]:
        f["content"].encode("ascii")
