"""Render a FrameworkPlan (Pillar D) into a HAL C init skeleton.

Deterministic string templating only — no inference lives here; every fact comes
from the plan produced by ``framework_solver``. Values the solver could not resolve
(an unknown alternate function, a peripheral with no design config) are emitted as
explicit ``TODO`` comments, never guessed. The result is a ``bsp_init.c`` /
``bsp_init.h`` pair the agent completes, flashes, and verifies via the acceptance
loop (Pillar C).
"""


def render_framework(plan: dict, style: str = "hal") -> dict:
    """Render a FrameworkPlan to source files.

    Returns ``{"style", "files": [{"path", "language", "content"}], "warnings",
    "todo_count"}``. Only the HAL style is implemented today.
    """
    warnings = []
    if style != "hal":
        warnings.append(f"Unsupported style {style!r}; rendering HAL.")
        style = "hal"

    header = _render_header(plan)
    source = _render_source(plan)
    todo_count = header.count("TODO") + source.count("TODO")
    return {
        "style": style,
        "files": [
            {"path": "bsp_init.h", "language": "c", "content": header},
            {"path": "bsp_init.c", "language": "c", "content": source},
        ],
        "warnings": warnings,
        "todo_count": todo_count,
    }


def _handle_peripherals(plan: dict) -> list[dict]:
    """Peripheral blocks that own a real HAL handle (skip untemplated 'other')."""
    return [b for b in plan.get("peripherals", []) if b.get("hal_init_call")]


def _render_header(plan: dict) -> str:
    lines = [
        "#ifndef BSP_INIT_H",
        "#define BSP_INIT_H",
        "",
        '#include "main.h"  /* HAL headers + Error_Handler() + handle typedefs */',
        "",
        "#ifdef __cplusplus",
        'extern "C" {',
        "#endif",
        "",
    ]
    handles = _handle_peripherals(plan)
    if handles:
        lines.append("/* Peripheral handles */")
        for block in handles:
            lines.append(f"extern {block['hal_type']} {block['handle']};")
        lines.append("")
    lines.append("void BSP_Init(void);")
    for fn in plan.get("init_order", []):
        lines.append(f"void {fn}(void);")
    lines += [
        "",
        "#ifdef __cplusplus",
        "}",
        "#endif",
        "",
        "#endif /* BSP_INIT_H */",
        "",
    ]
    return "\n".join(lines)


def _render_source(plan: dict) -> str:
    lines = [
        "/* Auto-generated BSP init skeleton -- design synthesis (Pillar D).",
        " * Derived deterministically from the netlist board model + design config.",
        " * TODO markers denote values that need target data or a design decision;",
        " * they are intentionally NOT guessed. Fill them in, flash, and let the",
        " * acceptance loop (Pillar C) verify the result. */",
        '#include "bsp_init.h"',
        "",
    ]

    handles = _handle_peripherals(plan)
    if handles:
        lines.append("/* Peripheral handles */")
        for block in handles:
            lines.append(f"{block['hal_type']} {block['handle']};")
        lines.append("")

    lines += _render_bsp_init(plan)
    lines += _render_system_clock()
    lines += _render_gpio_init(plan)
    for block in plan.get("peripherals", []):
        lines += _render_peripheral_init(block)
    return "\n".join(lines)


def _render_bsp_init(plan: dict) -> list[str]:
    lines = ["void BSP_Init(void)", "{"]
    for fn in plan.get("init_order", []):
        lines.append(f"    {fn}();")
    lines += ["}", ""]
    return lines


def _render_system_clock() -> list[str]:
    return [
        "void SystemClock_Config(void)",
        "{",
        "    /* TODO: configure the clock tree (HSE/HSI, PLL, bus prescalers) for your",
        "     * board. A clock-tree solver is out of scope for the generator; set this",
        "     * up (e.g. in CubeMX) so the baud/timing values below resolve correctly. */",
        "}",
        "",
    ]


def _render_gpio_init(plan: dict) -> list[str]:
    lines = ["void MX_GPIO_Init(void)", "{", "    GPIO_InitTypeDef GPIO_InitStruct = {0};", ""]

    port_macros = [c["hal_macro"] for c in plan.get("clocks", []) if c.get("kind") == "gpio_port"]
    if port_macros:
        lines.append("    /* GPIO port clocks */")
        for macro in port_macros:
            lines.append(f"    {macro}();")
        lines.append("")

    for pin in plan.get("gpio", []):
        lines += _render_gpio_pin(pin)
    lines += ["}", ""]
    return lines


def _render_gpio_pin(pin: dict) -> list[str]:
    label = f"{pin['peripheral']}_{pin['signal']}"
    lines = [f"    /* {pin['port_pin']}  {label} */",
             f"    GPIO_InitStruct.Pin = GPIO_PIN_{pin['pin']};",
             f"    GPIO_InitStruct.Mode = {pin['hal_mode']};",
             f"    GPIO_InitStruct.Pull = {pin['pull']};"]
    if pin.get("speed"):
        lines.append(f"    GPIO_InitStruct.Speed = {pin['speed']};")
    if pin.get("hal_alternate"):
        lines.append(f"    GPIO_InitStruct.Alternate = {pin['hal_alternate']};")
    elif pin.get("role") in ("af_pp", "af_od"):
        lines.append(f"    /* TODO: GPIO_InitStruct.Alternate for {label} "
                     "(alternate-function number from the datasheet) */")
    lines.append(f"    HAL_GPIO_Init(GPIO{pin['port']}, &GPIO_InitStruct);")
    lines.append("")
    return lines


def _render_peripheral_init(block: dict) -> list[str]:
    lines = [f"void {block['init_fn']}(void)", "{", f"    {block['clock_macro']}();"]

    if not block.get("hal_init_call"):
        pins = ", ".join(f"{p['port_pin']}={p['signal']}" for p in block.get("pins", []))
        lines += [f"    /* TODO: initialize {block['name']} (no HAL driver template). Pins: {pins} */",
                  "}", ""]
        return lines

    handle = block["handle"]
    lines.append(f"    {handle}.Instance = {block['instance']};")
    if block.get("has_config"):
        for field in block.get("config_fields", []):
            if field.get("mapped"):
                lines.append(f"    {handle}.Init.{field['field']} = {field['rendered']};")
            else:
                lines.append(f"    /* design.{field['source_key']} = {field['rendered']} "
                             "(no HAL field mapping; set the matching .Init member manually) */")
    else:
        lines.append(f"    /* TODO: no design config supplied for {block['name']}; "
                     "set its .Init fields (see the peripheral's HAL .Init members). */")
    lines += [f"    if ({block['hal_init_call']}(&{handle}) != HAL_OK)",
              "    {",
              "        Error_Handler();",
              "    }",
              "}",
              ""]
    return lines
