"""Unit tests for the deterministic product-spec reducer (spec_model)."""

from mcp_server.framework_solver import _KIND_META, _TIMER_TARGET_KEYS, DMA_KEYS, NVIC_KEYS
from mcp_server.spec_model import build_design


def _board(*peripherals):
    """Minimal board whose peripherals_in_use == the given names."""
    return {"mcu": {"pins": [{"function": {"peripheral": p}} for p in peripherals]}}


# --- UART framing / direction / flow ---------------------------------------


def test_uart_framing_8n1_expands_to_three_hal_macros():
    out = build_design({"USART1": {"framing": "8N1"}})
    assert out["design"]["USART1"] == {
        "word_length": "UART_WORDLENGTH_8B",
        "parity": "UART_PARITY_NONE",
        "stop_bits": "UART_STOPBITS_1",
    }
    assert out["unresolved"] == []


def test_uart_framing_with_parity_grows_word_length_and_notes_it():
    out = build_design({"USART1": {"framing": "8E1"}})
    assert out["design"]["USART1"]["word_length"] == "UART_WORDLENGTH_9B"
    assert out["design"]["USART1"]["parity"] == "UART_PARITY_EVEN"
    assert any("parity bit" in n for n in out["notes"])


def test_uart_framing_bad_code_is_unresolved_not_guessed():
    out = build_design({"USART1": {"framing": "8X1"}})
    assert out["design"]["USART1"] == {}
    assert out["unresolved"][0]["key"] == "framing"


def test_uart_framing_overwide_word_is_unresolved():
    out = build_design({"USART1": {"framing": "9E1"}})  # 9 data + parity = 10 bits
    assert out["design"]["USART1"] == {}
    assert "9/" not in out["unresolved"][0]["reason"]  # sanity: reason mentions 7/8/9
    assert "7/8/9" in out["unresolved"][0]["reason"]


def test_uart_baud_and_direction():
    out = build_design({"USART1": {"baud": 115200, "direction": "txrx"}})
    assert out["design"]["USART1"] == {"baud": 115200, "mode": "UART_MODE_TX_RX"}


def test_uart_direction_bad_value_unresolved():
    out = build_design({"USART1": {"direction": "half"}})
    assert out["unresolved"][0]["key"] == "direction"


def test_uart_flow_control_rtscts():
    out = build_design({"USART1": {"flow_control": "rtscts"}})
    assert out["design"]["USART1"]["flow_control"] == "UART_HWCONTROL_RTS_CTS"


# --- SPI --------------------------------------------------------------------


def test_spi_role_and_mode3():
    out = build_design({"SPI1": {"role": "master", "spi_mode": 3}})
    assert out["design"]["SPI1"] == {
        "mode": "SPI_MODE_MASTER",
        "clk_polarity": "SPI_POLARITY_HIGH",
        "clk_phase": "SPI_PHASE_2EDGE",
    }


def test_spi_mode_out_of_range_unresolved():
    out = build_design({"SPI1": {"spi_mode": 5}})
    assert out["unresolved"][0]["key"] == "spi_mode"
    assert out["design"]["SPI1"] == {}


def test_spi_datasize_and_bitorder():
    out = build_design({"SPI1": {"data_size": 16, "bit_order": "lsb"}})
    assert out["design"]["SPI1"] == {"data_size": "SPI_DATASIZE_16BIT", "first_bit": "SPI_FIRSTBIT_LSB"}


# --- I2C --------------------------------------------------------------------


def test_i2c_speed_is_recorded_unresolved_not_a_guessed_register():
    out = build_design({"I2C1": {"speed": "fast", "addressing": "10bit"}})
    assert "clock_speed" not in out["design"]["I2C1"]  # never renders a wrong register
    assert out["design"]["I2C1"]["addressing_mode"] == "I2C_ADDRESSINGMODE_10BIT"
    speed = next(u for u in out["unresolved"] if u["key"] == "speed")
    assert "400000" in speed["reason"]  # target recorded, register left honest


def test_i2c_speed_numeric_recorded_addressing_and_own_address_map():
    out = build_design({"I2C1": {"speed": 100000, "own_address": 0x33, "addressing": "7bit"}})
    assert out["design"]["I2C1"] == {"own_address": 0x33, "addressing_mode": "I2C_ADDRESSINGMODE_7BIT"}
    assert any(u["key"] == "speed" and "100000" in u["reason"] for u in out["unresolved"])


def test_i2c_speed_bad_value_unresolved():
    out = build_design({"I2C1": {"speed": "ludicrous"}})
    speed = next(u for u in out["unresolved"] if u["key"] == "speed")
    assert "expected one of" in speed["reason"]


# --- ADC --------------------------------------------------------------------


def test_adc_resolution_and_continuous():
    out = build_design({"ADC1": {"resolution": 12, "conversion": "continuous"}})
    assert out["design"]["ADC1"] == {"resolution": "ADC_RESOLUTION_12B", "continuous": "ENABLE"}


def test_adc_conversion_bad_value_unresolved():
    out = build_design({"ADC1": {"conversion": "burst"}})
    assert out["unresolved"][0]["key"] == "conversion"


# --- timer + common intent keys --------------------------------------------


def test_timer_update_hz_passes_through():
    out = build_design({"TIM3": {"update_hz": 1000}})
    assert out["design"]["TIM3"] == {"update_hz": 1000}


def test_common_interrupt_priority_dma_map_to_existing_opt_ins():
    out = build_design({"USART1": {"baud": 9600, "interrupt": True, "priority": 5, "dma": "rx"}})
    d = out["design"]["USART1"]
    assert d["nvic"] is True
    assert d["nvic_priority"] == 5
    assert d["dma"] == "rx"


def test_common_interrupt_false_does_not_emit_nvic():
    out = build_design({"USART1": {"interrupt": False}})
    assert "nvic" not in out["design"]["USART1"]


def test_unknown_intent_key_is_unresolved():
    out = build_design({"USART1": {"nonsense": 7}})
    assert out["unresolved"][0]["key"] == "nonsense"


def test_untranslated_kind_surfaces_specific_keys():
    out = build_design({"DAC1": {"buffer": True}})
    assert out["unresolved"][0]["peripheral"] == "DAC1"
    assert "no spec translator" in out["unresolved"][0]["reason"]


# --- board cross-check ------------------------------------------------------


def test_peripheral_absent_from_netlist_is_a_conflict():
    out = build_design({"USART2": {"baud": 115200}}, board=_board("USART1"))
    assert out["conflicts"][0]["peripheral"] == "USART2"
    assert "USART2" not in out["design"]  # no code for hardware that is not wired


def test_peripheral_present_in_netlist_is_designed():
    out = build_design({"USART1": {"baud": 115200}}, board=_board("USART1", "I2C1"))
    assert out["design"]["USART1"] == {"baud": 115200}
    assert out["conflicts"] == []


def test_no_board_skips_cross_check():
    out = build_design({"USART9": {"baud": 115200}})  # nonsense instance, but no board to check
    assert out["design"]["USART9"] == {"baud": 115200}
    assert out["conflicts"] == []


def test_non_object_spec_is_a_conflict():
    out = build_design({"USART1": "115200"})
    assert out["conflicts"][0]["peripheral"] == "USART1"


def test_stats_count_resolved_and_problems():
    out = build_design(
        {"USART1": {"baud": 115200, "framing": "8N1"}, "SPI1": {"spi_mode": 9}},
        board=_board("USART1", "SPI1"),
    )
    assert out["stats"]["peripherals"] == 2
    assert out["stats"]["unresolved"] == 1
    assert out["stats"]["resolved_keys"] == 4  # baud + 3 framing macros


# --- anti-drift contract with framework_solver ------------------------------


def test_translated_keys_are_all_understood_by_framework_solver():
    """Every design key spec_model can emit must be a key design_framework maps
    (a peripheral .Init field, a timer target, or an NVIC/DMA opt-in) -- otherwise
    the translated value would silently become an unmapped pass-through."""
    spec = {
        "USART1": {"baud": 115200, "framing": "8E1", "direction": "txrx",
                   "flow_control": "rtscts", "interrupt": True, "priority": 5, "dma": True},
        "SPI1": {"role": "master", "spi_mode": 2, "data_size": 8, "bit_order": "msb"},
        "I2C1": {"speed": "fast", "addressing": "7bit", "own_address": 16},
        "ADC1": {"resolution": 10, "conversion": "single", "dma": True},
        "TIM3": {"update_hz": 1000},
    }
    out = build_design(spec)
    kinds = {"USART1": "uart", "SPI1": "spi", "I2C1": "i2c", "ADC1": "adc", "TIM3": "timer"}
    passthrough = set(NVIC_KEYS) | set(DMA_KEYS) | set(_TIMER_TARGET_KEYS)
    for periph, cfg in out["design"].items():
        allowed = set(_KIND_META.get(kinds[periph], {}).get("fields", {})) | passthrough
        assert set(cfg) <= allowed, f"{periph} emitted keys outside framework_solver: {set(cfg) - allowed}"
