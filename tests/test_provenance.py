from mcp_server.framework_render import render_framework
from mcp_server.framework_solver import build_framework_plan
from mcp_server.provenance import (
    annotate_spec_sources,
    build_source_map,
    resolve_source,
)

# A minimal but structurally faithful rendered init: two-space realities of the real renderer
# (column-0 function braces, indented inner braces, pin-block header comments, macro calls).
_CONTENT = "\n".join([
    "void BSP_Init(void)",
    "{",
    "    SystemClock_Config();",
    "    MX_GPIO_Init();",
    "}",
    "",
    "void MX_GPIO_Init(void)",
    "{",
    "    GPIO_InitTypeDef GPIO_InitStruct = {0};",
    "",
    "    __HAL_RCC_GPIOA_CLK_ENABLE();",
    "",
    "    /* PA9  USART1_TX */",
    "    GPIO_InitStruct.Pin = GPIO_PIN_9;",
    "    GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;",
    "    HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);",
    "",
    "}",
    "",
    "void MX_USART1_UART_Init(void)",
    "{",
    "    __HAL_RCC_USART1_CLK_ENABLE();",
    "    huart1.Instance = USART1;",
    "    if (HAL_UART_Init(&huart1) != HAL_OK)",
    "    {",
    "        Error_Handler();",
    "    }",
    "    HAL_NVIC_SetPriority(USART1_IRQn, 0, 0);",
    "    HAL_NVIC_EnableIRQ(USART1_IRQn);",
    "}",
    "",
])


def _construct(smap, tag, key):
    for c in smap["constructs"]:
        if c["tag"] == tag and c["key"] == key:
            return c
    return None


def _line_text(content, line):
    return content.split("\n")[line - 1]


# --- build_source_map: functions ------------------------------------------------


def test_source_map_indexes_functions_with_line_spans():
    smap = build_source_map(_CONTENT, "bsp_init.c")
    names = {f["name"]: f for f in smap["functions"]}

    assert set(names) == {"BSP_Init", "MX_GPIO_Init", "MX_USART1_UART_Init"}
    for fn in names.values():
        assert fn["start_line"] < fn["end_line"]
        assert _line_text(_CONTENT, fn["start_line"]).startswith("void ")
        assert _line_text(_CONTENT, fn["end_line"]) == "}"  # column-0 close


def test_inner_braces_do_not_close_a_function():
    # MX_USART1_UART_Init contains an `if (...) {  } ` block; its close must not end the function.
    smap = build_source_map(_CONTENT, "bsp_init.c")
    usart = next(f for f in smap["functions"] if f["name"] == "MX_USART1_UART_Init")
    # The nvic construct lives *after* the inner brace block, so it must still be attributed
    # to MX_USART1_UART_Init (not leaked out by the indented `    }`).
    nvic = _construct(smap, "nvic_enable", "USART1_IRQn")
    assert nvic["init_fn"] == "MX_USART1_UART_Init"
    assert usart["start_line"] < nvic["line"] < usart["end_line"]


# --- build_source_map: constructs ----------------------------------------------


def test_clock_enable_construct_keyed_by_macro():
    smap = build_source_map(_CONTENT, "bsp_init.c")
    gpioa = _construct(smap, "clock_enable", "__HAL_RCC_GPIOA_CLK_ENABLE")
    usart = _construct(smap, "clock_enable", "__HAL_RCC_USART1_CLK_ENABLE")

    assert gpioa["init_fn"] == "MX_GPIO_Init"
    assert "__HAL_RCC_GPIOA_CLK_ENABLE();" in _line_text(_CONTENT, gpioa["line"])
    assert usart["init_fn"] == "MX_USART1_UART_Init"


def test_nvic_enable_construct_keyed_by_irq_name():
    smap = build_source_map(_CONTENT, "bsp_init.c")
    nvic = _construct(smap, "nvic_enable", "USART1_IRQn")

    assert nvic is not None
    # HAL_NVIC_SetPriority also names USART1_IRQn but is NOT the enable construct.
    assert "HAL_NVIC_EnableIRQ(USART1_IRQn);" in _line_text(_CONTENT, nvic["line"])


def test_gpio_mode_construct_keyed_by_port_pin_from_header():
    smap = build_source_map(_CONTENT, "bsp_init.c")
    pin = _construct(smap, "gpio_mode", "PA9")

    assert pin["init_fn"] == "MX_GPIO_Init"
    assert "HAL_GPIO_Init(GPIOA" in _line_text(_CONTENT, pin["line"])


def test_header_file_yields_no_constructs():
    # Declarations (`void BSP_Init(void);`) end with ';' and must not be indexed as definitions.
    header = "\n".join(["#ifndef BSP_INIT_H", "void BSP_Init(void);", "#ifdef __cplusplus", "}", "#endif"])
    smap = build_source_map(header, "bsp_init.h")

    assert smap["functions"] == []
    assert smap["constructs"] == []


# --- annotate_spec_sources: located --------------------------------------------


def _spec_with_provenance():
    return {"checks": [
        {"id": "nf", "kind": "no_fault",
         "provenance": {"origin": "no_fault", "init_fn": "BSP_Init"}},
        {"id": "clk", "kind": "memory_u32",
         "provenance": {"origin": "clock_enable", "macro": "__HAL_RCC_USART1_CLK_ENABLE"}},
        {"id": "irq", "kind": "memory_u32",
         "provenance": {"origin": "nvic_enable", "irq": "USART1_IRQn"}},
        {"id": "pin", "kind": "memory_u32",
         "provenance": {"origin": "gpio_mode", "port_pin": "PA9"}},
    ]}


def test_annotate_locates_every_origin():
    spec = _spec_with_provenance()
    smap = build_source_map(_CONTENT, "bsp_init.c")
    stats = annotate_spec_sources(spec, [smap])

    assert stats == {"located": 4, "unlocated": 0}
    by_id = {c["id"]: c["provenance"]["source"] for c in spec["checks"]}
    assert by_id["nf"]["located"] and by_id["nf"]["init_fn"] == "BSP_Init"
    assert "invariant" in by_id["nf"]["note"]
    assert by_id["clk"]["init_fn"] == "MX_USART1_UART_Init"
    assert by_id["irq"]["init_fn"] == "MX_USART1_UART_Init"
    assert by_id["pin"]["init_fn"] == "MX_GPIO_Init"
    assert by_id["pin"]["file"] == "bsp_init.c"


def test_annotate_leaves_provenance_free_checks_untouched():
    spec = {"checks": [{"id": "x", "kind": "no_fault"}]}
    stats = annotate_spec_sources(spec, [build_source_map(_CONTENT, "bsp_init.c")])

    assert stats == {"located": 0, "unlocated": 0}
    assert "provenance" not in spec["checks"][0]


# --- annotate_spec_sources: honest misses --------------------------------------


def test_stopped_at_non_init_symbol_is_unlocated_not_faked():
    spec = {"checks": [{"id": "s", "kind": "stopped_at",
                        "provenance": {"origin": "stopped_at", "symbol": "main"}}]}
    stats = annotate_spec_sources(spec, [build_source_map(_CONTENT, "bsp_init.c")])

    source = spec["checks"][0]["provenance"]["source"]
    assert stats == {"located": 0, "unlocated": 1}
    assert source["located"] is False
    assert "main" in source["reason"]


def test_unemitted_or_drifted_construct_is_unlocated_with_reason():
    spec = {"checks": [{"id": "irq", "kind": "memory_u32",
                        "provenance": {"origin": "nvic_enable", "irq": "TIM2_IRQn"}}]}
    annotate_spec_sources(spec, [build_source_map(_CONTENT, "bsp_init.c")])

    source = spec["checks"][0]["provenance"]["source"]
    assert source["located"] is False
    assert "TIM2_IRQn" in source["reason"]
    # tells the agent it may be un-emitted / plan drift, not a wrong value
    assert "un-emitted" in source["reason"] or "plan changed" in source["reason"]


def test_empty_source_map_locates_nothing_but_never_raises():
    spec = _spec_with_provenance()
    stats = annotate_spec_sources(spec, [])

    assert stats == {"located": 0, "unlocated": 4}
    assert all(c["provenance"]["source"]["located"] is False for c in spec["checks"])


def test_resolve_source_rejects_unknown_origin():
    result = resolve_source({"origin": "wat"}, {}, {})
    assert result["located"] is False
    assert "wat" in result["reason"]


# --- integration: the real renderer feeds a joinable source map -----------------


def _board(pins):
    return {"mcu": {"part_normalized": "STM32L431CBT6", "family": "STM32L4",
                    "line": "STM32L431", "pins": pins}}


def test_real_render_source_map_joins_derived_provenance():
    pins = [
        {"package_pin": "1", "port_pin": "PA9", "net": "/USART1_TX",
         "function": {"peripheral": "USART1", "signal": "TX"}},
        {"package_pin": "2", "port_pin": "PB6", "net": "/I2C1_SCL",
         "function": {"peripheral": "I2C1", "signal": "SCL"}},
    ]
    plan = build_framework_plan(_board(pins), design={"USART1": {"nvic": True}})
    source_maps = render_framework(plan)["source_map"]
    keys = {(c["tag"], c["key"]) for smap in source_maps for c in smap["constructs"]}

    assert ("clock_enable", "__HAL_RCC_USART1_CLK_ENABLE") in keys
    assert ("nvic_enable", "USART1_IRQn") in keys
    assert ("gpio_mode", "PA9") in keys
