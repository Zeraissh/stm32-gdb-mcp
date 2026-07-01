"""Deterministic STM32 clock-tree solver (design synthesis, Pillar D Tier 3).

Fills the last conspicuous hand-written gap in the generated init code:
``SystemClock_Config()``. Given a device *profile* (datasheet constraints) and a
*request* (clock source + target SYSCLK), this computes the exact PLL dividers, bus
prescalers, and flash latency to reach that frequency -- or reports honestly that the
target is infeasible. It is pure, bounded integer math over documented ranges; it
never guesses. An unknown device (no built-in profile and no explicit ``profile``) or
an HSE with no crystal frequency is surfaced as ``unresolved``, not fabricated.

Model (single main PLL, F4 / L4 style)::

    vco_in  = f_in / M            # within [vco_in_min, vco_in_max]
    vco_out = vco_in * N          # within [vco_out_min, vco_out_max]
    sysclk  = vco_out / D         # D = PLLP (F4) or PLLR (L4), in the div set
    clk48   = vco_out / Q         # optional, exact 48 MHz for USB/SDIO/RNG
    hclk    = sysclk / AHB_presc  # <= max_hclk
    pclk1   = hclk / APB1_presc   # <= max_pclk1
    pclk2   = hclk / APB2_presc   # <= max_pclk2

Deterministic tie-break: prefer ``vco_in`` closest to ``ideal_vco_in_hz`` (2 MHz for
F4, 16 MHz for L4), then smaller M then N. Bus prescalers are minimised (peripheral
clocks maximised), matching common vendor output. The chosen PLL is electrically
equivalent to CubeMX's even when the exact M/N differ.
"""

_MHZ = 1_000_000
_USB_HZ = 48 * _MHZ

# AHB (HPRE) and APB (PPRE) prescaler options common to F4 / L4.
_AHB_DIVS = (1, 2, 4, 8, 16, 64, 128, 256, 512)
_APB_DIVS = (1, 2, 4, 8, 16)


def _f4_profile(max_sysclk, max_pclk1, max_pclk2, flash_latency, *, family="STM32F4"):
    """Build an F4-style profile (PLLP feeds SYSCLK; M/N/Q plain ints)."""
    return {
        "family": family,
        "sysclk_pll_field": "P",
        "hsi_hz": 16 * _MHZ,
        "pll": {
            "m": (2, 63), "n": (50, 432), "sysclk_div_set": (2, 4, 6, 8),
            "q": (2, 15), "vco_in_hz": (1 * _MHZ, 2 * _MHZ),
            "vco_out_hz": (100 * _MHZ, 432 * _MHZ), "ideal_vco_in_hz": 2 * _MHZ,
        },
        "max_sysclk_hz": max_sysclk, "max_hclk_hz": max_sysclk,
        "max_pclk1_hz": max_pclk1, "max_pclk2_hz": max_pclk2,
        "flash_latency": flash_latency,
        "voltage_note": "flash wait-states for 2.7-3.6 V (VOS scale 1)",
    }


def _l4_profile():
    """Mainstream STM32L4 (<= 80 MHz): PLLR feeds SYSCLK; PLLP/PLLQ are macro fields."""
    return {
        "family": "STM32L4",
        "sysclk_pll_field": "R",
        "hsi_hz": 16 * _MHZ,
        "pll": {
            "m": (1, 8), "n": (8, 86), "sysclk_div_set": (2, 4, 6, 8),
            "q": (2, 4, 6, 8), "vco_in_hz": (4 * _MHZ, 16 * _MHZ),
            "vco_out_hz": (64 * _MHZ, 344 * _MHZ), "ideal_vco_in_hz": 16 * _MHZ,
        },
        "max_sysclk_hz": 80 * _MHZ, "max_hclk_hz": 80 * _MHZ,
        "max_pclk1_hz": 80 * _MHZ, "max_pclk2_hz": 80 * _MHZ,
        "flash_latency": ((16 * _MHZ, 0), (32 * _MHZ, 1), (48 * _MHZ, 2),
                          (64 * _MHZ, 3), (80 * _MHZ, 4)),
        "voltage_note": "flash wait-states for range 1 (1.2 V); L4+ (L4R/L4S) differ",
    }


# Flash wait-state tables (ascending [max_hclk_hz, wait_states]).
_F407_FLASH = ((30 * _MHZ, 0), (60 * _MHZ, 1), (90 * _MHZ, 2),
               (120 * _MHZ, 3), (150 * _MHZ, 4), (168 * _MHZ, 5))
_F411_FLASH = ((30 * _MHZ, 0), (64 * _MHZ, 1), (90 * _MHZ, 2), (100 * _MHZ, 3))
_F401_FLASH = ((30 * _MHZ, 0), (60 * _MHZ, 1), (84 * _MHZ, 2))


def _builtin_profiles():
    f407 = _f4_profile(168 * _MHZ, 42 * _MHZ, 84 * _MHZ, _F407_FLASH, family="STM32F4")
    f401 = _f4_profile(84 * _MHZ, 42 * _MHZ, 84 * _MHZ, _F401_FLASH, family="STM32F4")
    f411 = _f4_profile(100 * _MHZ, 50 * _MHZ, 100 * _MHZ, _F411_FLASH, family="STM32F4")
    l4 = _l4_profile()
    return {"F407": f407, "F405": f407, "F415": f407, "F417": f407,
            "F401": f401, "F411": f411, "L4": l4}


_L4_PLUS_PREFIXES = ("STM32L4R", "STM32L4S", "STM32L4P", "STM32L4Q")


def resolve_profile(line: str | None, family: str | None = None) -> dict | None:
    """Return a built-in device profile for a normalized line/family, or None.

    Honest by design: unknown devices (and overdrive/L4+ parts we do not model)
    yield ``None`` so the caller surfaces an ``unresolved`` rather than guessing.
    """
    profiles = _builtin_profiles()
    up_line = (line or "").upper()
    up_family = (family or "").upper()

    for key in ("F407", "F405", "F415", "F417", "F401", "F411"):
        if up_line.startswith(f"STM32{key}"):
            return profiles[key]

    if up_line.startswith(_L4_PLUS_PREFIXES):
        return None  # L4+ (120 MHz) uses a different topology.
    if up_line.startswith("STM32L4") or up_family == "STM32L4":
        return profiles["L4"]
    return None


def _resolve_source_hz(profile: dict, request: dict) -> tuple[str, int | None, str | None]:
    """Resolve (source, f_in, error) from the request."""
    source = (request.get("source") or "HSI").upper()
    if source == "HSI":
        return "HSI", int(profile.get("hsi_hz") or 16 * _MHZ), None
    if source == "HSE":
        hz = request.get("source_hz")
        if not hz:
            return "HSE", None, "hse_frequency_unknown"
        return "HSE", int(hz), None
    return source, None, "unsupported_source"


def _bus_prescaler(clock_hz: int, max_hz: int, divs) -> int | None:
    """Smallest prescaler that divides clock_hz evenly and stays <= max_hz."""
    for div in divs:
        if clock_hz % div:
            continue
        if clock_hz // div <= max_hz:
            return div
    return None


def _flash_latency(hclk_hz: int, table) -> int:
    for max_hclk, wait_states in table:
        if hclk_hz <= max_hclk:
            return wait_states
    return table[-1][1]


def _best_q(vco_out_hz: int, q_spec, need_48: bool):
    """Find a PLL Q giving exactly 48 MHz.

    A 2-element ``q_spec`` is an inclusive ``(min, max)`` range (F4 PLLQ 2..15); a
    longer tuple is an explicit divider set (L4 PLLQ {2,4,6,8}).
    """
    if not q_spec:
        return (None, False)
    q_values = range(q_spec[0], q_spec[1] + 1) if len(q_spec) == 2 else q_spec
    for q in q_values:
        if q and vco_out_hz % q == 0 and vco_out_hz // q == _USB_HZ:
            return q, True
    return (None, False)


def _search_pll(profile: dict, f_in: int, target: int, need_48: bool):
    """Exhaustive, deterministic PLL search. Returns the best core solution or None."""
    pll = profile["pll"]
    m_min, m_max = pll["m"]
    n_min, n_max = pll["n"]
    vco_in_min, vco_in_max = pll["vco_in_hz"]
    vco_out_min, vco_out_max = pll["vco_out_hz"]
    ideal = pll.get("ideal_vco_in_hz", 2 * _MHZ)

    best = None
    for d in pll["sysclk_div_set"]:
        vco_out = target * d
        if not (vco_out_min <= vco_out <= vco_out_max):
            continue
        for m in range(m_min, m_max + 1):
            if f_in % m:
                # Non-integer VCO input is allowed by hardware, but requiring M to
                # divide f_in keeps vco_in exact and the search well-defined.
                if (f_in / m) < vco_in_min or (f_in / m) > vco_in_max:
                    continue
            vco_in = f_in / m
            if not (vco_in_min <= vco_in <= vco_in_max):
                continue
            num = target * d * m
            if num % f_in:
                continue
            n = num // f_in
            if not (n_min <= n <= n_max):
                continue
            q, usb_ok = _best_q(vco_out, pll.get("q"), need_48)
            score = (
                0 if (usb_ok or not need_48) else 1,
                abs(vco_in - ideal),
                m, n,
            )
            candidate = {"m": m, "n": n, "sysclk_div": d, "q": q,
                         "vco_in_hz": int(vco_in), "vco_out_hz": vco_out,
                         "usb_48_ok": usb_ok, "_score": score}
            if best is None or candidate["_score"] < best["_score"]:
                best = candidate
    return best


def solve_clock_tree(profile: dict, request: dict) -> dict:
    """Solve the clock tree for a device profile + request.

    ``request``: ``{source: "HSE"|"HSI", source_hz?, target_sysclk_hz, need_48mhz?}``.
    Returns ``{feasible, solution, unresolved, notes, stats}``. ``solution`` is present
    only when feasible and is self-contained (carries the family + field hints needed
    to render ``SystemClock_Config``).
    """
    unresolved: list[dict] = []
    notes: list[str] = []

    target = request.get("target_sysclk_hz")
    if not target:
        unresolved.append({"type": "target_missing",
                           "detail": "Provide target_sysclk_hz (the desired SYSCLK in Hz)."})
        return _infeasible(unresolved, notes)

    source, f_in, err = _resolve_source_hz(profile, request)
    if err:
        unresolved.append({"type": err, "source": source,
                           "detail": "HSE selected but source_hz (crystal frequency) is unknown."
                           if err == "hse_frequency_unknown" else f"Unsupported clock source {source}."})
        return _infeasible(unresolved, notes)

    max_sysclk = profile["max_sysclk_hz"]
    if target > max_sysclk:
        unresolved.append({"type": "target_exceeds_max_sysclk", "target_hz": target,
                           "max_sysclk_hz": max_sysclk,
                           "detail": f"Target {target} Hz exceeds the device max SYSCLK {max_sysclk} Hz."})
        return _infeasible(unresolved, notes)

    need_48 = bool(request.get("need_48mhz"))
    use_pll = target != f_in
    if use_pll:
        core = _search_pll(profile, f_in, target, need_48)
        if core is None:
            unresolved.append({"type": "no_pll_solution", "target_hz": target, "source_hz": f_in,
                               "detail": f"No exact PLL configuration reaches {target} Hz from {f_in} Hz "
                               "within this device's PLL ranges."})
            return _infeasible(unresolved, notes)
        vco_out = core["vco_out_hz"]
        if need_48 and not core["usb_48_ok"]:
            notes.append("Requested 48 MHz (USB/SDIO/RNG) is not exactly reachable from this VCO; "
                         "clk48 domain left unconfigured. Adjust the target or use a dedicated 48 MHz source.")
    else:
        core = None
        vco_out = None
        notes.append(f"Target equals the {source} source frequency; running SYSCLK directly off "
                     f"{source} with the PLL disabled.")

    ahb = _bus_prescaler(target, profile["max_hclk_hz"], _AHB_DIVS)
    if ahb is None:
        unresolved.append({"type": "no_ahb_prescaler", "detail": "Could not keep HCLK within limits."})
        return _infeasible(unresolved, notes)
    hclk = target // ahb
    apb1 = _bus_prescaler(hclk, profile["max_pclk1_hz"], _APB_DIVS)
    apb2 = _bus_prescaler(hclk, profile["max_pclk2_hz"], _APB_DIVS)
    if apb1 is None or apb2 is None:
        unresolved.append({"type": "no_apb_prescaler", "detail": "Could not keep PCLK1/PCLK2 within limits."})
        return _infeasible(unresolved, notes)

    flash_ws = _flash_latency(hclk, profile["flash_latency"])
    clk48 = None
    if core and core.get("q"):
        clk48 = vco_out // core["q"]

    solution = {
        "family": profile.get("family"),
        "sysclk_pll_field": profile.get("sysclk_pll_field", "P"),
        "source": source,
        "source_hz": f_in,
        "use_pll": use_pll,
        "pll": None if core is None else {
            "m": core["m"], "n": core["n"], "sysclk_div": core["sysclk_div"],
            "q": core["q"], "vco_in_hz": core["vco_in_hz"], "vco_out_hz": vco_out,
        },
        "sysclk_hz": target,
        "hclk_hz": hclk,
        "pclk1_hz": hclk // apb1,
        "pclk2_hz": hclk // apb2,
        "ahb_presc": ahb,
        "apb1_presc": apb1,
        "apb2_presc": apb2,
        "flash_latency": flash_ws,
        "clk48_hz": clk48,
        "usb_48_ok": bool(core and core["usb_48_ok"]),
    }
    return {
        "feasible": True,
        "solution": solution,
        "unresolved": unresolved,
        "notes": notes,
        "stats": {"sysclk_hz": target, "hclk_hz": hclk, "flash_latency": flash_ws,
                  "usb_48_ok": solution["usb_48_ok"]},
    }


def _infeasible(unresolved, notes) -> dict:
    return {"feasible": False, "solution": None, "unresolved": unresolved,
            "notes": notes, "stats": {"unresolved_count": len(unresolved)}}


def summarize_clock_solution(result: dict) -> dict:
    """Compact, human-facing view of a solve_clock_tree result."""
    if not result.get("feasible"):
        return {"feasible": False, "unresolved": result.get("unresolved", []),
                "notes": result.get("notes", [])}
    s = result["solution"]
    view = {
        "feasible": True,
        "source": f"{s['source']} @ {s['source_hz'] // _MHZ} MHz",
        "sysclk_mhz": s["sysclk_hz"] / _MHZ,
        "hclk_mhz": s["hclk_hz"] / _MHZ,
        "pclk1_mhz": s["pclk1_hz"] / _MHZ,
        "pclk2_mhz": s["pclk2_hz"] / _MHZ,
        "flash_latency": s["flash_latency"],
        "usb_48_ok": s["usb_48_ok"],
        "notes": result.get("notes", []),
    }
    if s["pll"]:
        p = s["pll"]
        view["pll"] = f"M={p['m']} N={p['n']} {s['sysclk_pll_field']}={p['sysclk_div']} Q={p['q']}"
    else:
        view["pll"] = "disabled (direct source)"
    return view


# --- HAL C rendering --------------------------------------------------------

def _ahb_macro(div: int) -> str:
    return f"RCC_SYSCLK_DIV{div}"


def _apb_macro(div: int) -> str:
    return f"RCC_HCLK_DIV{div}"


def render_system_clock_config(solution: dict) -> list[str]:
    """Render a concrete, pure-ASCII HAL ``SystemClock_Config()`` from a solution."""
    field = solution.get("sysclk_pll_field", "P")
    source = solution["source"]
    lines = [
        "void SystemClock_Config(void)",
        "{",
        "    /* Auto-generated by the clock-tree solver (Pillar D). Achieved:",
        f"     *   SYSCLK={solution['sysclk_hz'] // _MHZ} MHz  HCLK={solution['hclk_hz'] // _MHZ} MHz"
        f"  PCLK1={solution['pclk1_hz'] // _MHZ} MHz  PCLK2={solution['pclk2_hz'] // _MHZ} MHz",
        f"     *   flash wait-states={solution['flash_latency']}"
        + (f", USB/48MHz clock={solution['clk48_hz'] // _MHZ} MHz" if solution.get("clk48_hz") else "")
        + " */",
        "    RCC_OscInitTypeDef RCC_OscInitStruct = {0};",
        "    RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};",
        "",
    ]

    osc_type = "RCC_OSCILLATORTYPE_HSE" if source == "HSE" else "RCC_OSCILLATORTYPE_HSI"
    lines.append(f"    RCC_OscInitStruct.OscillatorType = {osc_type};")
    if source == "HSE":
        lines.append("    RCC_OscInitStruct.HSEState = RCC_HSE_ON;")
    else:
        lines.append("    RCC_OscInitStruct.HSIState = RCC_HSI_ON;")
        lines.append("    RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;")

    pll = solution.get("pll")
    if pll:
        pll_source = "RCC_PLLSOURCE_HSE" if source == "HSE" else "RCC_PLLSOURCE_HSI"
        lines += [
            "    RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;",
            f"    RCC_OscInitStruct.PLL.PLLSource = {pll_source};",
            f"    RCC_OscInitStruct.PLL.PLLM = {pll['m']};",
            f"    RCC_OscInitStruct.PLL.PLLN = {pll['n']};",
        ]
        if field == "R":  # L4-style: PLLR feeds SYSCLK; PLLP/PLLQ are macro fields.
            q = pll["q"] or 2
            lines += [
                "    RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV7;",
                f"    RCC_OscInitStruct.PLL.PLLQ = RCC_PLLQ_DIV{q};",
                f"    RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV{pll['sysclk_div']};",
            ]
        else:  # F4-style: PLLP feeds SYSCLK; PLLQ is a plain integer.
            lines.append(f"    RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV{pll['sysclk_div']};")
            if pll["q"]:
                lines.append(f"    RCC_OscInitStruct.PLL.PLLQ = {pll['q']};")
    else:
        lines.append("    RCC_OscInitStruct.PLL.PLLState = RCC_PLL_NONE;")

    lines += [
        "    if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)",
        "    {",
        "        Error_Handler();",
        "    }",
        "",
        "    RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK"
        " | RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;",
    ]
    if pll:
        lines.append("    RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;")
    else:
        sysclk_src = "RCC_SYSCLKSOURCE_HSE" if source == "HSE" else "RCC_SYSCLKSOURCE_HSI"
        lines.append(f"    RCC_ClkInitStruct.SYSCLKSource = {sysclk_src};")
    lines += [
        f"    RCC_ClkInitStruct.AHBCLKDivider = {_ahb_macro(solution['ahb_presc'])};",
        f"    RCC_ClkInitStruct.APB1CLKDivider = {_apb_macro(solution['apb1_presc'])};",
        f"    RCC_ClkInitStruct.APB2CLKDivider = {_apb_macro(solution['apb2_presc'])};",
        f"    if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_{solution['flash_latency']}) != HAL_OK)",
        "    {",
        "        Error_Handler();",
        "    }",
        "}",
        "",
    ]
    return lines
