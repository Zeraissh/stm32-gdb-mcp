"""Render a FrameworkPlan (Pillar D) into a HAL C init skeleton.

Deterministic string templating only -- no inference lives here; every fact comes
from the plan produced by ``framework_solver``. Values the solver could not resolve
(an unknown alternate function, a peripheral with no design config) are emitted as
explicit ``TODO`` comments, never guessed. The result is a ``bsp_init.c`` /
``bsp_init.h`` pair the agent completes, flashes, and verifies via the acceptance
loop (Pillar C).
"""

from .clock_solver import render_system_clock_config


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


def _dma_handles(plan: dict) -> list[str]:
    """Names of every renderable DMA handle (resolved, non-conflicting) in the plan."""
    names: list[str] = []
    for block in plan.get("peripherals", []):
        dma = block.get("dma")
        if not dma:
            continue
        for stream in dma.get("streams", []):
            if not stream.get("conflict"):
                names.append(stream["handle"])
    return names


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
    dma_handles = _dma_handles(plan)
    if dma_handles:
        lines.append("/* DMA handles */")
        for name in dma_handles:
            lines.append(f"extern DMA_HandleTypeDef {name};")
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
    dma_handles = _dma_handles(plan)
    if dma_handles:
        lines.append("/* DMA handles */")
        for name in dma_handles:
            lines.append(f"DMA_HandleTypeDef {name};")
        lines.append("")

    lines += _render_bsp_init(plan)
    lines += _render_system_clock(plan)
    lines += _render_gpio_init(plan)
    for block in plan.get("peripherals", []):
        lines += _render_peripheral_init(block)
    lines += _render_isrs(plan)
    return "\n".join(lines)


def _render_isrs(plan: dict) -> list[str]:
    """One interrupt service routine per resolved vector, dispatching to the HAL handler.

    Covers both the peripheral vectors (``HAL_UART_IRQHandler`` etc.) and the DMA
    stream vectors (``HAL_DMA_IRQHandler(&hdma_...)``). Shared vectors (e.g.
    ``TIM6_DAC_IRQn``) that serve several enabled peripherals emit a single ISR that
    calls every attached handle, so no duplicate symbol is generated.
    """
    by_isr: dict[str, list[str]] = {}
    order: list[str] = []

    def _add(isr: str, call: str) -> None:
        if isr not in by_isr:
            by_isr[isr] = []
            order.append(isr)
        if call not in by_isr[isr]:
            by_isr[isr].append(call)

    for block in plan.get("peripherals", []):
        nvic = block.get("nvic")
        if nvic and nvic.get("resolved"):
            for vector in nvic["vectors"]:
                _add(vector["isr"], f"{vector['handler']}(&{block['handle']});")
        dma = block.get("dma")
        if dma:
            for stream in dma.get("streams", []):
                if stream.get("conflict"):
                    continue
                vector = stream["nvic"]
                _add(vector["isr"], f"{vector['handler']}(&{stream['handle']});")

    if not order:
        return []
    lines = ["/* Interrupt service routines -- dispatch into the HAL handlers. */"]
    for isr in order:
        lines += [f"void {isr}(void)", "{"]
        lines += [f"    {call}" for call in by_isr[isr]]
        lines += ["}", ""]
    return lines


def _render_bsp_init(plan: dict) -> list[str]:
    lines = ["void BSP_Init(void)", "{"]
    for fn in plan.get("init_order", []):
        lines.append(f"    {fn}();")
    lines += ["}", ""]
    return lines


def _render_system_clock(plan: dict) -> list[str]:
    clock_config = plan.get("clock_config")
    if clock_config:
        # The clock-tree solver resolved a concrete configuration -> emit real code.
        return render_system_clock_config(clock_config)
    return [
        "void SystemClock_Config(void)",
        "{",
        "    /* TODO: configure the clock tree (HSE/HSI, PLL, bus prescalers) for your",
        "     * board. Run solve_clock_tree to synthesize this automatically, or set it",
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
    for field in block.get("config_fields", []):
        source = field.get("source")
        if source == "default":
            comment = "  /* default */"
        elif source == "derived":
            comment = f"  /* derived: {field['note']} */" if field.get("note") else "  /* derived */"
        else:
            comment = ""
        lines.append(f"    {handle}.Init.{field['field']} = {field['rendered']};{comment}")
    for extra in block.get("unmapped_config", []):
        lines.append(f"    /* design.{extra['key']} = {extra['rendered']} "
                     "(no HAL .Init field mapping; set the matching member manually) */")
    for todo in block.get("param_todos", []):
        lines.append(f"    /* TODO: set {handle}.Init.{todo['field']} -- {todo['hint']} */")
    if not block.get("config_fields") and not block.get("param_todos"):
        lines.append(f"    /* TODO: no design config supplied for {block['name']}; "
                     "set its .Init fields (see the peripheral's HAL .Init members). */")
    lines += [f"    if ({block['hal_init_call']}(&{handle}) != HAL_OK)",
              "    {",
              "        Error_Handler();",
              "    }"]
    lines += _render_dma(block)
    lines += _render_nvic_calls(block)
    lines += ["}", ""]
    return lines


def _render_dma(block: dict) -> list[str]:
    """DMA handle init + __HAL_LINKDMA + DMA-stream NVIC enable, or an honest TODO.

    Reuses the NVIC backbone for the transfer interrupt: every DMA stream gets its
    own ``HAL_NVIC_SetPriority`` / ``HAL_NVIC_EnableIRQ`` and (via _render_isrs) an
    ISR dispatching to ``HAL_DMA_IRQHandler``.
    """
    dma = block.get("dma")
    if not dma or not dma.get("requested"):
        return []

    lines: list[str] = []
    for miss in dma.get("unresolved", []):
        lines.append(f"    /* TODO: {block['name']} {miss['direction']} DMA requested but stream "
                     f"unknown -- {miss['reason']} */")

    streams = [s for s in dma.get("streams", []) if not s.get("conflict")]
    for conflicted in (s for s in dma.get("streams", []) if s.get("conflict")):
        lines.append(f"    /* TODO: {block['name']} {conflicted['direction']} DMA {conflicted['instance']} "
                     "collides with another peripheral; resolve before enabling. */")
    if not streams:
        return lines

    lines.append("    /* DMA */")
    seen_clocks: set = set()
    for stream in streams:
        if stream["clock_macro"] not in seen_clocks:
            lines.append(f"    {stream['clock_macro']}();")
            seen_clocks.add(stream["clock_macro"])

    note = "  /* default priority -- review preemption for your app/RTOS */"
    for stream in streams:
        hdma = stream["handle"]
        lines += [
            f"    {hdma}.Instance = {stream['instance']};",
            f"    {hdma}.Init.{stream['select_field']} = {stream['select_value']};",
            f"    {hdma}.Init.Direction = {stream['direction_macro']};",
            f"    {hdma}.Init.PeriphInc = DMA_PINC_DISABLE;",
            f"    {hdma}.Init.MemInc = DMA_MINC_ENABLE;",
            f"    {hdma}.Init.PeriphDataAlignment = {stream['periph_align']};",
            f"    {hdma}.Init.MemDataAlignment = {stream['mem_align']};",
            f"    {hdma}.Init.Mode = DMA_NORMAL;",
            f"    {hdma}.Init.Priority = {stream['priority_macro']};",
            f"    if (HAL_DMA_Init(&{hdma}) != HAL_OK)",
            "    {",
            "        Error_Handler();",
            "    }",
            f"    __HAL_LINKDMA(&{block['handle']}, {stream['link_field']}, {hdma});",
        ]
        vector = stream["nvic"]
        lines.append(f"    HAL_NVIC_SetPriority({vector['irqn']}, {vector['preempt']}, {vector['sub']});{note}")
        lines.append(f"    HAL_NVIC_EnableIRQ({vector['irqn']});")
        note = ""  # annotate only the first stream
    return lines


def _render_nvic_calls(block: dict) -> list[str]:
    """HAL_NVIC_SetPriority + HAL_NVIC_EnableIRQ for each resolved vector, or a TODO."""
    nvic = block.get("nvic")
    if not nvic or not nvic.get("requested"):
        return []
    if not nvic.get("resolved"):
        return [f"    /* TODO: enable {block['name']} interrupt -- {nvic['unresolved_reason']} */"]
    lines = ["    /* NVIC */"]
    note = ("  /* default priority -- review preemption for your app/RTOS */"
            if nvic.get("priority_source") == "default" else "")
    for vector in nvic["vectors"]:
        lines.append(f"    HAL_NVIC_SetPriority({vector['irqn']}, {nvic['preempt']}, {nvic['sub']});{note}")
        lines.append(f"    HAL_NVIC_EnableIRQ({vector['irqn']});")
        note = ""  # annotate only the first vector
    return lines
