"""Design tools (Pillar D): spec translation, framework synthesis/rendering, and solvers."""

import copy
import os

from mcp.types import TextContent, Tool

from ..acceptance_model import summarize_acceptance, validate_acceptance_spec
from ..acceptance_synth import (
    derive_acceptance_spec,
    dict_clock_resolver,
    dict_gpio_resolver,
    dict_irq_resolver,
    svd_clock_resolver,
    svd_gpio_resolver,
    svd_irq_resolver,
)
from ..board_validation import load_capability_db
from ..clock_solver import resolve_profile, summarize_clock_solution
from ..clock_solver import solve_clock_tree as solve_clock_tree_impl
from ..framework_render import render_framework as render_framework_impl
from ..framework_solver import build_framework_plan, framework_view, merge_af_maps, summarize_framework
from ..provenance import annotate_spec_sources
from ..spec_model import build_design
from ..timer_solver import solve_timers_in_plan
from ..tool_response import content_error, content_success
from .context import ToolContext
from .registry import register


@register(Tool(
    name="import_spec",
    description="Translate a controlled-vocabulary product spec (human/product terms) into the "
                "per-peripheral design params design_framework consumes, and cross-check it against the "
                "imported netlist. This is the upstream guard the pipeline lacked: instead of hand-writing "
                "HAL macros, supply intent -- UART framing '8N1', direction 'txrx', flow_control 'rtscts'; "
                "SPI role 'master', spi_mode 0..3, data_size, bit_order; I2C speed 'fast', addressing "
                "'7bit'; ADC resolution 12, conversion 'continuous'; a timer update_hz; plus dma / "
                "interrupt / priority opt-ins -- and the machine expands it deterministically (8E1 -> "
                "UART_WORDLENGTH_9B + UART_PARITY_EVEN, honoring HAL's parity-bit-in-word-length rule). A "
                "peripheral named in the spec but absent from the netlist is a conflict; an intent key or "
                "value the machine does not model is surfaced as unresolved -- never guessed. Then "
                "design_framework(from_spec=true) builds the plan from the translated params. Import a "
                "netlist first so the spec is cross-checked.",
    inputSchema={
        "type": "object",
        "properties": {
            "spec": {"type": "object", "description": "Per-peripheral intent, e.g. {'USART1': {'baud': 115200, 'framing': '8N1', 'direction': 'txrx'}, 'ADC1': {'resolution': 12, 'conversion': 'continuous', 'dma': true}}."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def import_spec(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    spec = arguments.get("spec")
    if not isinstance(spec, dict) or not spec:
        return [content_error(
            "import_spec needs a non-empty 'spec' object mapping peripheral -> intent config.",
            code="missing_argument",
            suggested_next_actions=["import_spec(spec={'USART1': {'baud': 115200, 'framing': '8N1'}})"])]
    board = ctx.board.get("current")
    result = build_design(spec, board=board)
    ctx.spec["current"] = result
    payload = dict(result)
    payload["cross_checked"] = board is not None
    if result["conflicts"]:
        actions = ["describe_board (what=peripherals)", "fix the spec, then import_spec again"]
    elif result["unresolved"]:
        actions = ["review unresolved, then design_framework(from_spec=true)"]
    else:
        actions = ["design_framework(from_spec=true)"]
    return [content_success(payload, suggested_next_actions=actions)]


@register(Tool(
    name="design_framework",
    description="Synthesize a deterministic FrameworkPlan (Pillar D) from the session's imported "
                "board: which clocks to enable, how each pin must be muxed, and which peripheral "
                "init blocks to emit, in dependency order. Supply per-peripheral HAL .Init "
                "parameters via design={'USART1': {'baud': 115200, ...}} and optional AF numbers "
                "via af_map. Mandatory .Init members are auto-filled with HAL-standard defaults "
                "(a complete, valid init struct, not a half-initialized one) and a few are derived "
                "from the netlist (UART flow control from RTS/CTS pins, SPI NSS from an NSS pin); "
                "each field is tagged explicit/derived/default. Alternate-function numbers are also "
                "auto-derived from a pin-capability DB (db_path or the STM32_GDB_MCP_PIN_DB env) "
                "when its entries carry an 'af' field; an explicit af_map overrides the DB per pin. "
                "Anything that needs a human decision (baud, timer period, I2C timing) is surfaced "
                "as unresolved, never guessed. Import a netlist first (import_netlist).",
    inputSchema={
        "type": "object",
        "properties": {
            "design": {"type": "object", "description": "Per-peripheral config, e.g. {'USART1': {'baud': 115200, 'word_length': 'UART_WORDLENGTH_8B'}}."},
            "from_spec": {"type": "boolean", "description": "Build from the session's imported product spec (import_spec) instead of, or merged under, an explicit design (explicit keys win)."},
            "af_map": {"type": "object", "description": "Optional alternate-function numbers: {line_or_family: {port_pin: {'USART1_TX': 7}}}. Overrides db_path per pin."},
            "db_path": {"type": "string", "description": "Optional JSON pin-capability DB (CubeMX-derived); entries with an 'af' field auto-fill alternate-function numbers."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def design_framework(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    board = ctx.board.get("current")
    if not board:
        return [content_error(
            "No board imported for this session. Run import_netlist first.", code="no_board",
            suggested_next_actions=["import_netlist(path='board.net')"])]
    design = arguments.get("design")
    if design is not None and not isinstance(design, dict):
        return [content_error(
            "design must be an object mapping peripheral name -> config.", code="invalid_argument",
            suggested_next_actions=["design_framework(design={'USART1': {'baud': 115200}})"])]
    if arguments.get("from_spec"):
        stored = ctx.spec.get("current")
        if not stored:
            return [content_error(
                "from_spec set but no spec imported for this session. Run import_spec first.",
                code="no_spec",
                suggested_next_actions=["import_spec(spec={'USART1': {'baud': 115200}})"])]
        spec_design = stored.get("design") or {}
        if design:
            merged = {p: dict(cfg) for p, cfg in spec_design.items()}
            for p, cfg in design.items():
                merged[p] = {**merged.get(p, {}), **(cfg or {})}
            design = merged
        else:
            design = {p: dict(cfg) for p, cfg in spec_design.items()}
    af_map = arguments.get("af_map")
    if af_map is not None and not isinstance(af_map, dict):
        return [content_error(
            "af_map must be an object {line_or_family: {port_pin: {'PERIPH_SIG': af}}}.",
            code="invalid_argument", suggested_next_actions=["design_framework"])]
    db_path = arguments.get("db_path") or os.environ.get("STM32_GDB_MCP_PIN_DB")
    if db_path:
        try:
            af_map = merge_af_maps(load_capability_db(db_path).af_map(), af_map)
        except (OSError, ValueError) as exc:
            return [content_error(
                f"Could not load pin-capability DB '{db_path}': {exc}", code="invalid_db",
                suggested_next_actions=["design_framework without db_path"])]
    plan = build_framework_plan(board, design=design, af_map=af_map)
    ctx.design["current"] = plan
    ctx.design["last_render"] = None
    return [content_success(
        summarize_framework(plan),
        suggested_next_actions=["describe_framework (what=unresolved)", "render_framework"])]


@register(Tool(
    name="describe_framework",
    description="Read the synthesized FrameworkPlan. what=summary (mcu + clocks + peripherals), "
                "clocks, gpio (per-pin config), peripherals (init blocks), init_order, or "
                "unresolved (the TODO holes that need target data or a design decision). "
                "Run design_framework first.",
    inputSchema={
        "type": "object",
        "properties": {
            "what": {"type": "string", "description": "summary|clocks|gpio|peripherals|init_order|unresolved (default summary)."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def describe_framework(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    plan = ctx.design.get("current")
    if not plan:
        return [content_error(
            "No framework plan for this session. Run design_framework first.", code="no_design",
            suggested_next_actions=["design_framework"])]
    what = arguments.get("what", "summary")
    view = framework_view(plan, what)
    if view is None:
        return [content_error(
            f"Unknown view '{what}'.", code="invalid_argument",
            suggested_next_actions=["describe_framework (what=summary|clocks|gpio|peripherals|init_order|unresolved)"])]
    return [content_success(view)]


@register(Tool(
    name="render_framework",
    description="Render the synthesized FrameworkPlan to a HAL C init skeleton (bsp_init.c + "
                "bsp_init.h). Every derived fact (clock enables, GPIO modes, mapped .Init fields) "
                "is concrete; every unresolved value is a clearly marked TODO — nothing is "
                "fabricated. Returns the files, their content, and a todo_count. Run "
                "design_framework first.",
    inputSchema={
        "type": "object",
        "properties": {
            "style": {"type": "string", "description": "Code style (default 'hal')."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def render_framework(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    plan = ctx.design.get("current")
    if not plan:
        return [content_error(
            "No framework plan for this session. Run design_framework first.", code="no_design",
            suggested_next_actions=["design_framework"])]
    rendered = render_framework_impl(plan, style=arguments.get("style", "hal"))
    ctx.design["last_render"] = rendered
    return [content_success(
        rendered,
        suggested_next_actions=["synthesize_acceptance", "build_firmware"])]


@register(Tool(
    name="synthesize_acceptance",
    description="Auto-derive a machine-checked AcceptanceSpec from the synthesized FrameworkPlan "
                "(Pillar D Tier 3) and load it as the session's acceptance judge — welding design "
                "synthesis to the acceptance loop. Always emits a no_fault check (init must not "
                "HardFault); adds a memory_u32 bits_set check per clock the plan enables (RCC "
                "enable bit), a memory_u32 bits_set check per interrupt the plan enables (arch-standard "
                "NVIC ISER bit, from the resolved IRQ number), and a masked memory_u32 eq check per "
                "configured pin (GPIO MODER = AF/analog; F1's CRL/CRH is skipped). Register/IRQ/port "
                "placements come from the session's loaded SVD or explicit register_map/irq_map/gpio_map; "
                "anything unresolvable is surfaced, never guessed. Each derived check also carries source "
                "provenance (the init function + line that should satisfy it), so a later failure points "
                "straight at the fix site. Run design_framework first; load an "
                "SVD (start_debug_session/set svd) for clock/NVIC/GPIO checks.",
    inputSchema={
        "type": "object",
        "properties": {
            "register_map": {"type": "object", "description": "Optional explicit RCC placements {line_or_family: {clock: {address, bit}}}; overrides the SVD."},
            "irq_map": {"type": "object", "description": "Optional explicit IRQ numbers {line_or_family: {irq_name: number}} (name with or without _IRQn); overrides the SVD."},
            "gpio_map": {"type": "object", "description": "Optional explicit GPIO port bases {line_or_family: {port_letter: MODER_address}}; overrides the SVD."},
            "stopped_at": {"type": "string", "description": "Optional symbol to also assert the PC reached after init (e.g. 'main')."},
            "include_no_fault": {"type": "boolean", "description": "Emit the no_fault check (default true)."},
            "include_nvic": {"type": "boolean", "description": "Emit NVIC ISER checks for enabled interrupts (default true)."},
            "include_gpio": {"type": "boolean", "description": "Emit GPIO MODER checks for configured pins (default true)."},
            "load": {"type": "boolean", "description": "Load the derived spec as the session acceptance judge (default true)."},
            "name": {"type": "string", "description": "Optional spec name."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def synthesize_acceptance(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    plan = ctx.design.get("current")
    if not plan:
        return [content_error(
            "No framework plan for this session. Run design_framework first.", code="no_design",
            suggested_next_actions=["design_framework"])]
    register_map = arguments.get("register_map")
    if register_map is not None and not isinstance(register_map, dict):
        return [content_error(
            "register_map must be an object {line_or_family: {clock: {address, bit}}}.",
            code="invalid_argument", suggested_next_actions=["synthesize_acceptance"])]
    irq_map = arguments.get("irq_map")
    if irq_map is not None and not isinstance(irq_map, dict):
        return [content_error(
            "irq_map must be an object {line_or_family: {irq_name: number}}.",
            code="invalid_argument", suggested_next_actions=["synthesize_acceptance"])]
    gpio_map = arguments.get("gpio_map")
    if gpio_map is not None and not isinstance(gpio_map, dict):
        return [content_error(
            "gpio_map must be an object {line_or_family: {port_letter: base_address}}.",
            code="invalid_argument", suggested_next_actions=["synthesize_acceptance"])]
    mcu = plan.get("mcu") or {}
    line, family = mcu.get("line"), mcu.get("family")
    svd_loaded = getattr(ctx.svd_parser, "svd_root", None) is not None
    if register_map:
        clock_resolver, clock_source = dict_clock_resolver(register_map, line, family), "register_map"
    elif svd_loaded:
        clock_resolver, clock_source = svd_clock_resolver(ctx.svd_parser), "svd"
    else:
        clock_resolver, clock_source = None, "none"
    if irq_map:
        irq_resolver, irq_source = dict_irq_resolver(irq_map, line, family), "irq_map"
    elif svd_loaded:
        irq_resolver, irq_source = svd_irq_resolver(ctx.svd_parser), "svd"
    else:
        irq_resolver, irq_source = None, "none"
    if gpio_map:
        gpio_resolver, gpio_source = dict_gpio_resolver(gpio_map, line, family), "gpio_map"
    elif svd_loaded:
        gpio_resolver, gpio_source = svd_gpio_resolver(ctx.svd_parser), "svd"
    else:
        gpio_resolver, gpio_source = None, "none"
    options = {
        "include_no_fault": arguments.get("include_no_fault", True),
        "include_nvic": arguments.get("include_nvic", True),
        "include_gpio": arguments.get("include_gpio", True),
        "stopped_at": arguments.get("stopped_at"),
        "name": arguments.get("name"),
    }
    derived = derive_acceptance_spec(plan, clock_resolver=clock_resolver, options=options,
                                     irq_resolver=irq_resolver, gpio_resolver=gpio_resolver)
    try:
        validated = validate_acceptance_spec(derived["spec"])
    except ValueError as exc:
        return [content_error(
            f"Derived acceptance spec is invalid: {exc}", code="invalid_spec",
            suggested_next_actions=["synthesize_acceptance(include_no_fault=true)"])]
    # Provenance -> source (Pillar E): render the same plan, build its per-file source map,
    # and resolve each check's provenance to the exact init function + line it verifies. A
    # construct that was not emitted (TODO/unresolved) resolves to located=false, never a
    # fabricated line. The stored spec is self-contained -- run_acceptance and every loop
    # verdict then carry result.provenance.source with no further plan lookup.
    provenance_stats = annotate_spec_sources(validated, render_framework_impl(plan).get("source_map"))
    loaded = arguments.get("load", True)
    if loaded:
        ctx.acceptance["current"] = validated
        ctx.acceptance["last_result"] = None
    next_actions = (["start_acceptance_loop", "describe_acceptance (what=checks)"] if loaded
                    else ["load_acceptance", "describe_acceptance"])
    return [content_success({
        "spec": summarize_acceptance(validated),
        "checks": validated["checks"],
        "unresolved": derived["unresolved"],
        "notes": derived["notes"],
        "stats": derived["stats"],
        "provenance": provenance_stats,
        "placement_source": clock_source,
        "resolver_sources": {"clock": clock_source, "nvic": irq_source, "gpio": gpio_source},
        "loaded": loaded,
    }, suggested_next_actions=next_actions)]


@register(Tool(
    name="solve_clock_tree",
    description="Synthesize a concrete SystemClock_Config() for the session's FrameworkPlan "
                "(Pillar D Tier 3) - the last hand-written gap in generated init code. Given a "
                "clock source (HSE + crystal Hz, or HSI) and a target SYSCLK, it computes the exact "
                "PLL dividers (M/N/P or R, Q for 48 MHz USB), AHB/APB bus prescalers, and flash "
                "wait-states via pure datasheet math, then stores the result so the next "
                "render_framework emits real clock code instead of a TODO stub. Deterministic and "
                "honest: an unmodelled device or an infeasible target is surfaced, never guessed. "
                "Built-in profiles: STM32F401/F407/F411 and mainstream L4 (<=80 MHz); pass an "
                "explicit profile for others. Run design_framework first.",
    inputSchema={
        "type": "object",
        "properties": {
            "sysclk_hz": {"type": "integer", "description": "Target SYSCLK in Hz (e.g. 168000000)."},
            "source": {"type": "string", "description": "Clock source: 'HSE' or 'HSI' (default 'HSI')."},
            "source_hz": {"type": "integer", "description": "HSE crystal frequency in Hz (required when source=HSE)."},
            "need_48mhz": {"type": "boolean", "description": "Require an exact 48 MHz PLL output for USB/SDIO/RNG (default false)."},
            "profile": {"type": "object", "description": "Optional explicit device profile; overrides the built-in table."},
            "load": {"type": "boolean", "description": "Store the solution into the plan so render_framework uses it (default true)."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def solve_clock_tree(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    plan = ctx.design.get("current")
    if not plan:
        return [content_error(
            "No framework plan for this session. Run design_framework first.", code="no_design",
            suggested_next_actions=["design_framework"])]
    profile_arg = arguments.get("profile")
    if profile_arg is not None and not isinstance(profile_arg, dict):
        return [content_error(
            "profile must be an object (a device clock profile).", code="invalid_argument",
            suggested_next_actions=["solve_clock_tree(sysclk_hz=...)"])]
    target = arguments.get("sysclk_hz") or arguments.get("target_sysclk_hz")
    if not target:
        return [content_error(
            "Provide sysclk_hz (target SYSCLK in Hz).", code="missing_argument",
            suggested_next_actions=["solve_clock_tree(sysclk_hz=80000000)"])]
    mcu = plan.get("mcu") or {}
    profile = profile_arg or resolve_profile(mcu.get("line"), mcu.get("family"))
    if not profile:
        return [content_success({
            "feasible": False,
            "unresolved": [{"type": "device_unmodelled", "line": mcu.get("line"),
                            "family": mcu.get("family"),
                            "detail": "No built-in clock profile for this device; pass an explicit "
                                      "profile with the datasheet PLL/bus limits."}],
            "notes": [],
        }, suggested_next_actions=["solve_clock_tree(profile={...})"])]
    request = {
        "source": arguments.get("source"),
        "source_hz": arguments.get("source_hz"),
        "target_sysclk_hz": int(target),
        "need_48mhz": bool(arguments.get("need_48mhz")),
    }
    result = solve_clock_tree_impl(profile, request)
    if not result["feasible"]:
        return [content_success({
            "feasible": False,
            "unresolved": result["unresolved"],
            "notes": result["notes"],
        }, suggested_next_actions=["solve_clock_tree (adjust sysclk_hz / provide source_hz)"])]
    loaded = arguments.get("load", True)
    if loaded:
        plan["clock_config"] = result["solution"]  # persisted in the session plan
    return [content_success({
        "feasible": True,
        "clock": summarize_clock_solution(result),
        "solution": result["solution"],
        "notes": result["notes"],
        "loaded": loaded,
    }, suggested_next_actions=["render_framework", "synthesize_acceptance"])]


@register(Tool(
    name="solve_timer",
    description="Solve a timer's Prescaler/Period (PSC/ARR) for a target update frequency "
                "(Pillar D Tier 3). Turns intent ('TIM3 at 1 kHz') into concrete register values "
                "using the timer input clock (TIMxCLK) derived from the solved clock tree. Record a "
                "target via design_framework(design={'TIM3': {'update_hz': 1000}}) then run "
                "solve_clock_tree first, or pass timer_clock_hz directly for a what-if. Deterministic "
                "and honest: an exact target yields zero-error PSC/ARR, an inexact one yields the "
                "closest pair plus the achieved frequency and ppm error, and an unrepresentable target "
                "or an unknown timer bus is surfaced, never guessed. Injects the result into the plan "
                "so render_framework emits concrete values instead of a TODO.",
    inputSchema={
        "type": "object",
        "properties": {
            "timer": {"type": "string", "description": "Timer to solve, e.g. 'TIM3'. Omit to solve every timer that has a recorded target."},
            "target_hz": {"type": "number", "description": "Target update frequency in Hz; overrides the recorded design target (requires timer)."},
            "timer_clock_hz": {"type": "integer", "description": "Explicit TIMxCLK in Hz; bypasses the clock-solution + bus derivation (pure what-if)."},
            "bus": {"type": "string", "description": "Override the timer's APB bus: 'apb1' or 'apb2'."},
            "arr_bits": {"type": "integer", "description": "ARR width override: 16 or 32 (for 32-bit timers TIM2/TIM5)."},
            "load": {"type": "boolean", "description": "Persist the solved PSC/ARR into the plan (default true)."},
            "session": {"type": "string", "description": "Target session id (default 'default')."}
        }
    }
))
def solve_timer(ctx: ToolContext, arguments: dict) -> list[TextContent]:
    plan = ctx.design.get("current")
    if not plan:
        return [content_error(
            "No framework plan for this session. Run design_framework first.", code="no_design",
            suggested_next_actions=["design_framework"])]
    timer = arguments.get("timer")
    target_hz = arguments.get("target_hz")
    if target_hz is not None and not timer:
        return [content_error(
            "target_hz requires a specific timer=; omit target_hz to use recorded design targets.",
            code="invalid_argument", suggested_next_actions=["solve_timer(timer='TIM3', target_hz=1000)"])]
    bus = arguments.get("bus")
    if bus is not None and bus not in ("apb1", "apb2"):
        return [content_error(
            "bus must be 'apb1' or 'apb2'.", code="invalid_argument",
            suggested_next_actions=["solve_timer(timer='TIM3', bus='apb1')"])]
    arr_bits = arguments.get("arr_bits")
    if arr_bits is not None and arr_bits not in (16, 32):
        return [content_error(
            "arr_bits must be 16 or 32.", code="invalid_argument",
            suggested_next_actions=["solve_timer(arr_bits=32)"])]
    loaded = arguments.get("load", True)
    target_plan = plan if loaded else copy.deepcopy(plan)
    overrides = {timer: target_hz} if (timer and target_hz is not None) else None
    report = solve_timers_in_plan(
        target_plan, only=timer, target_overrides=overrides,
        timer_clock_hz=arguments.get("timer_clock_hz"), bus_override=bus, arr_bits_override=arr_bits)
    if not report["results"]:
        detail = (f"{timer} has no recorded target; pass target_hz=." if timer
                  else "No timer had a recorded target (design update_hz) to solve.")
        return [content_success({
            "solved_count": 0, "timer_count": report["timer_count"], "results": [], "detail": detail,
        }, suggested_next_actions=["design_framework(design={'TIM3': {'update_hz': 1000}})"])]
    if loaded:
        ctx.design["current"] = target_plan
        ctx.design["last_render"] = None
    return [content_success({
        "solved_count": report["solved_count"],
        "timer_count": report["timer_count"],
        "results": report["results"],
        "loaded": loaded,
    }, suggested_next_actions=["render_framework", "describe_framework (what=unresolved)"])]
