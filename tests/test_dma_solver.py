"""Unit tests for the deterministic DMA association resolver (Pillar D Tier 3)."""

from mcp_server.dma_solver import DMA_KEYS, build_dma


def _stream(block, direction):
    return next(s for s in block["streams"] if s["direction"] == direction)


def test_dma_keys_are_the_design_directives():
    assert DMA_KEYS == ("dma", "dma_priority")


def test_no_request_returns_none():
    assert build_dma("USART1", "uart", "STM32L4", dma=None, dma_priority=None) is None


def test_l4_usart1_rx_tx_streams():
    block = build_dma("USART1", "uart", "STM32L4", dma=True)
    assert block["resolved"] is True
    assert block["unresolved"] == []
    rx = _stream(block, "rx")
    assert rx["instance"] == "DMA1_Channel5"
    assert rx["select_field"] == "Request"
    assert rx["select_value"] == "DMA_REQUEST_2"
    assert rx["direction_macro"] == "DMA_PERIPH_TO_MEMORY"
    assert rx["link_field"] == "hdmarx"
    assert rx["handle"] == "hdma_usart1_rx"
    assert rx["clock_macro"] == "__HAL_RCC_DMA1_CLK_ENABLE"
    assert rx["nvic"]["irqn"] == "DMA1_Channel5_IRQn"
    assert rx["nvic"]["isr"] == "DMA1_Channel5_IRQHandler"
    assert rx["nvic"]["handler"] == "HAL_DMA_IRQHandler"
    tx = _stream(block, "tx")
    assert tx["instance"] == "DMA1_Channel4"
    assert tx["select_value"] == "DMA_REQUEST_2"
    assert tx["direction_macro"] == "DMA_MEMORY_TO_PERIPH"
    assert tx["link_field"] == "hdmatx"


def test_l4_spi1_and_i2c1_request_numbers():
    spi = build_dma("SPI1", "spi", "STM32L4", dma=True)
    assert _stream(spi, "rx")["instance"] == "DMA1_Channel2"
    assert _stream(spi, "rx")["select_value"] == "DMA_REQUEST_1"
    assert _stream(spi, "tx")["instance"] == "DMA1_Channel3"
    i2c = build_dma("I2C1", "i2c", "STM32L4", dma=True)
    assert _stream(i2c, "rx")["instance"] == "DMA1_Channel7"
    assert _stream(i2c, "rx")["select_value"] == "DMA_REQUEST_3"
    assert _stream(i2c, "tx")["instance"] == "DMA1_Channel6"


def test_l4_adc1_is_single_receive_stream_with_dma_handle_link():
    block = build_dma("ADC1", "adc", "STM32L4", dma=True)
    assert len(block["streams"]) == 1
    rx = block["streams"][0]
    assert rx["direction"] == "rx"
    assert rx["handle"] == "hdma_adc1"  # no direction suffix
    assert rx["link_field"] == "DMA_Handle"
    assert rx["instance"] == "DMA1_Channel1"
    assert rx["select_value"] == "DMA_REQUEST_0"
    assert rx["periph_align"] == "DMA_PDATAALIGN_HALFWORD"
    assert rx["mem_align"] == "DMA_MDATAALIGN_HALFWORD"


def test_f4_uses_stream_and_channel_selector():
    block = build_dma("USART1", "uart", "STM32F4", dma=True)
    rx = _stream(block, "rx")
    assert rx["instance"] == "DMA2_Stream2"
    assert rx["select_field"] == "Channel"
    assert rx["select_value"] == "DMA_CHANNEL_4"
    assert rx["nvic"]["irqn"] == "DMA2_Stream2_IRQn"
    tx = _stream(block, "tx")
    assert tx["instance"] == "DMA2_Stream7"


def test_f4_spi1_i2c1_adc1_streams():
    spi = build_dma("SPI1", "spi", "STM32F4", dma=True)
    assert _stream(spi, "rx")["instance"] == "DMA2_Stream0"
    assert _stream(spi, "rx")["select_value"] == "DMA_CHANNEL_3"
    assert _stream(spi, "tx")["instance"] == "DMA2_Stream3"
    i2c = build_dma("I2C1", "i2c", "STM32F4", dma=True)
    assert _stream(i2c, "rx")["instance"] == "DMA1_Stream0"
    assert _stream(i2c, "tx")["instance"] == "DMA1_Stream6"
    adc = build_dma("ADC1", "adc", "STM32F4", dma=True)
    assert adc["streams"][0]["instance"] == "DMA2_Stream4"  # S4 dodges SPI1_RX on S0
    assert adc["streams"][0]["select_value"] == "DMA_CHANNEL_0"


def test_direction_filter_rx_only():
    block = build_dma("USART1", "uart", "STM32L4", dma="rx")
    assert [s["direction"] for s in block["streams"]] == ["rx"]


def test_direction_filter_list():
    block = build_dma("USART1", "uart", "STM32L4", dma=["tx"])
    assert [s["direction"] for s in block["streams"]] == ["tx"]


def test_priority_default_and_explicit():
    default = build_dma("USART1", "uart", "STM32L4", dma=True)
    assert default["priority_source"] == "default"
    assert _stream(default, "rx")["priority_macro"] == "DMA_PRIORITY_LOW"
    high = build_dma("USART1", "uart", "STM32L4", dma=True, dma_priority="high")
    assert high["priority_source"] == "explicit"
    assert _stream(high, "rx")["priority_macro"] == "DMA_PRIORITY_HIGH"


def test_priority_alone_still_enables_dma():
    block = build_dma("USART1", "uart", "STM32L4", dma=None, dma_priority="medium")
    assert block is not None
    assert block["resolved"] is True
    assert {s["direction"] for s in block["streams"]} == {"rx", "tx"}


def test_unknown_family_is_surfaced_not_guessed():
    block = build_dma("USART1", "uart", "STM32G0", dma=True)
    assert block["resolved"] is False
    assert block["streams"] == []
    assert "not in the built-in" in block["unresolved_reason"]


def test_unknown_peripheral_is_surfaced_not_guessed():
    block = build_dma("USART2", "uart", "STM32L4", dma=True)
    assert block["resolved"] is False
    assert "No DMA request mapping known for USART2" in block["unresolved_reason"]


def test_adc_transmit_request_is_receive_only_unresolved():
    block = build_dma("ADC1", "adc", "STM32L4", dma="tx")
    assert block["resolved"] is False
    assert block["unresolved"][0]["direction"] == "tx"


def test_unsupported_kind_is_surfaced():
    block = build_dma("TIM2", "timer", "STM32L4", dma=True)
    assert block["resolved"] is False
    assert "not supported for timer" in block["unresolved_reason"]
