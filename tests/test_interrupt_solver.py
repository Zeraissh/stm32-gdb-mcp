"""Unit tests for the deterministic NVIC interrupt resolver (Pillar D Tier 3)."""

from mcp_server.interrupt_solver import build_nvic, resolve_vectors

# --- Vector resolution ------------------------------------------------------


def test_uart_and_spi_use_regular_single_vector():
    (v,) = resolve_vectors("USART1", "uart", "STM32F4")
    assert v["irqn"] == "USART1_IRQn"
    assert v["handler"] == "HAL_UART_IRQHandler"
    assert v["isr"] == "USART1_IRQHandler"
    assert v["source"] == "regular"
    (s,) = resolve_vectors("SPI2", "spi", "STM32L4")
    assert s["irqn"] == "SPI2_IRQn" and s["handler"] == "HAL_SPI_IRQHandler"


def test_i2c_resolves_event_and_error_pair():
    vectors = resolve_vectors("I2C1", "i2c", "STM32F4")
    assert [v["irqn"] for v in vectors] == ["I2C1_EV_IRQn", "I2C1_ER_IRQn"]
    assert [v["handler"] for v in vectors] == ["HAL_I2C_EV_IRQHandler", "HAL_I2C_ER_IRQHandler"]
    assert [v["role"] for v in vectors] == ["event", "error"]


def test_i2c_unknown_family_is_unresolved():
    assert resolve_vectors("I2C1", "i2c", "STM32G0") == []


def test_timer_and_dac_use_family_table():
    (t3,) = resolve_vectors("TIM3", "timer", "STM32F4")
    assert t3["irqn"] == "TIM3_IRQn" and t3["source"] == "table"
    # TIM6 shares its vector with the DAC on F4/L4.
    (t6,) = resolve_vectors("TIM6", "timer", "STM32F4")
    assert t6["irqn"] == "TIM6_DAC_IRQn"
    (dac,) = resolve_vectors("DAC", "dac", "STM32L4")
    assert dac["irqn"] == "TIM6_DAC_IRQn"


def test_adc_vector_varies_by_family():
    assert resolve_vectors("ADC1", "adc", "STM32F4")[0]["irqn"] == "ADC_IRQn"
    assert resolve_vectors("ADC1", "adc", "STM32L4")[0]["irqn"] == "ADC1_2_IRQn"


def test_unknown_timer_vector_is_unresolved():
    # Advanced-timer combined vectors are not in the table -> honest empty.
    assert resolve_vectors("TIM1", "timer", "STM32F4") == []


def test_irqn_override_wins_and_infers_i2c_role():
    vectors = resolve_vectors("TIM1", "timer", "STM32F4", irqn_override="TIM1_UP_TIM10_IRQn")
    assert vectors[0]["irqn"] == "TIM1_UP_TIM10_IRQn"
    assert vectors[0]["isr"] == "TIM1_UP_TIM10_IRQHandler"
    assert vectors[0]["source"] == "override"
    pair = resolve_vectors("I2C4", "i2c", "STM32G0", irqn_override=["I2C4_EV_IRQn", "I2C4_ER_IRQn"])
    assert [v["role"] for v in pair] == ["event", "error"]


# --- build_nvic (priority + request handling) -------------------------------


def test_no_request_returns_none():
    assert build_nvic("USART1", "uart", "STM32F4") is None


def test_default_priority_when_enabled_without_number():
    nvic = build_nvic("USART1", "uart", "STM32F4", nvic=True)
    assert nvic["requested"] and nvic["resolved"]
    assert nvic["preempt"] == 5 and nvic["sub"] == 0
    assert nvic["priority_source"] == "default"


def test_explicit_priority_int_and_pair_and_dict():
    assert build_nvic("SPI1", "spi", "STM32F4", nvic_priority=3)["preempt"] == 3
    pair = build_nvic("SPI1", "spi", "STM32F4", nvic_priority=[2, 1])
    assert (pair["preempt"], pair["sub"], pair["priority_source"]) == (2, 1, "explicit")
    d = build_nvic("SPI1", "spi", "STM32F4", nvic={"preempt": 7, "sub": 3})
    assert (d["preempt"], d["sub"]) == (7, 3)


def test_irqn_alone_implies_enable():
    nvic = build_nvic("TIM1", "timer", "STM32F4", irqn="TIM1_UP_TIM10_IRQn")
    assert nvic["requested"] and nvic["resolved"]
    assert nvic["vectors"][0]["irqn"] == "TIM1_UP_TIM10_IRQn"


def test_requested_but_unresolved_is_honest():
    nvic = build_nvic("TIM1", "timer", "STM32F4", nvic=True)
    assert nvic["requested"] and not nvic["resolved"]
    assert nvic["vectors"] == []
    assert "device-specific" in nvic["unresolved_reason"]
