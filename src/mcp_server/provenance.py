"""Map an acceptance check back to the exact rendered-source construct that should satisfy it
(design synthesis, Pillar E -- failure -> source provenance).

When a derived check fails, the agent needs the *cause*, not just the symptom: which init
function, which line, which construct was supposed to set that bit. This module supplies it
**deterministically**, never by inference. Every derived check already comes from one concrete
plan element, and ``framework_render`` renders that element into one construct on one line, so

    check  ->  plan element  ->  rendered construct (file, init_fn, line)

is a pure join of facts the machine already produced -- analogous to a compiler emitting debug
line info.

* :func:`build_source_map` scans a rendered init file into an index of init functions (line
  spans) and tagged constructs ``{tag, key, line, text, init_fn}`` -- a text scan of exactly
  what was emitted, no guessing.
* :func:`annotate_spec_sources` joins each check's ``provenance`` join key (RCC macro / IRQ name
  / port-pin, attached by ``acceptance_synth``) to a construct and fills ``provenance.source``.

Honest misses are surfaced, never faked. If a construct was **not emitted** (the clock / NVIC /
GPIO was unresolved, so the renderer wrote a ``TODO`` instead of the enable line), the join finds
nothing and yields ``located = false`` with a reason that says *make the code emit it* -- not
*the value is wrong*. ``no_fault`` is a whole-init invariant, so it points at the ``BSP_Init``
span with a note rather than a fabricated line. A key that matches nothing (e.g. the plan changed
since the spec was synthesized) is ``located = false`` too -- never mapped to the wrong line.
"""

import re

# A function definition line in the rendered init (``void MX_..._Init(void)``). Declarations in
# the header end with ``;`` and deliberately do not match, so only real definitions are indexed.
_FUNC_RE = re.compile(r"^void (\w+)\(void\)\s*$")
# A clock-enable macro call: the key is the macro itself (``__HAL_RCC_USART1_CLK_ENABLE``), which
# is exactly the ``hal_macro`` the plan carried, so the join is a verbatim string match.
_CLOCK_RE = re.compile(r"^\s*(__HAL_RCC_\w+_CLK_ENABLE)\(\);")
# An NVIC set-enable call: the key is the IRQ name (``USART1_IRQn``).
_NVIC_RE = re.compile(r"^\s*HAL_NVIC_EnableIRQ\((\w+)\);")
# A per-pin block header comment (``/* PA9  USART1_TX */``): the key is the port-pin.
_PIN_HEADER_RE = re.compile(r"^\s*/\*\s*(P[A-K]\d+)\b")
# The mode-setting call that closes a pin block; attributed to the port-pin from its header.
_GPIO_INIT_RE = re.compile(r"^\s*HAL_GPIO_Init\(")

# origin (on the check's provenance) -> (source-map construct tag, provenance field holding the key)
_ORIGIN_JOIN = {
    "clock_enable": ("clock_enable", "macro"),
    "nvic_enable": ("nvic_enable", "irq"),
    "gpio_mode": ("gpio_mode", "port_pin"),
}


def build_source_map(content: str, path: str) -> dict:
    """Scan rendered init *content* into an index of functions + tagged constructs.

    Returns ``{"path", "functions": [{"name", "start_line", "end_line"}],
    "constructs": [{"tag", "key", "line", "text", "init_fn"}]}`` with 1-based line numbers.
    Pure text scan -- it reports only what the renderer emitted.
    """
    functions: list[dict] = []
    constructs: list[dict] = []
    current_fn: str | None = None
    current_start = 0
    current_pin: str | None = None

    for index, raw in enumerate(content.split("\n"), start=1):
        func = _FUNC_RE.match(raw)
        if func:
            current_fn, current_start, current_pin = func.group(1), index, None
            continue
        # A lone column-0 ``}`` closes the current function; inner braces are indented.
        if raw == "}" and current_fn is not None:
            functions.append({"name": current_fn, "start_line": current_start, "end_line": index})
            current_fn = current_pin = None
            continue

        clock = _CLOCK_RE.match(raw)
        if clock:
            constructs.append({"tag": "clock_enable", "key": clock.group(1),
                               "line": index, "text": raw.strip(), "init_fn": current_fn})
            continue
        nvic = _NVIC_RE.match(raw)
        if nvic:
            constructs.append({"tag": "nvic_enable", "key": nvic.group(1),
                               "line": index, "text": raw.strip(), "init_fn": current_fn})
            continue
        header = _PIN_HEADER_RE.match(raw)
        if header:
            current_pin = header.group(1)
            continue
        if current_pin is not None and _GPIO_INIT_RE.match(raw):
            constructs.append({"tag": "gpio_mode", "key": current_pin,
                               "line": index, "text": raw.strip(), "init_fn": current_fn})
            current_pin = None

    return {"path": path, "functions": functions, "constructs": constructs}


def _index(source_maps) -> tuple[dict, dict]:
    """Flatten source maps into ``{(tag, key): construct}`` and ``{fn_name: location}``.

    First occurrence wins; a construct key is unique per rendered init, so collisions do not
    normally arise.
    """
    by_key: dict = {}
    functions: dict = {}
    for smap in source_maps or []:
        path = smap.get("path")
        for fn in smap.get("functions", []):
            functions.setdefault(fn["name"], {"path": path, "line": fn["start_line"],
                                              "end_line": fn["end_line"]})
        for construct in smap.get("constructs", []):
            by_key.setdefault((construct["tag"], construct["key"]),
                              {"path": path, **construct})
    return by_key, functions


def resolve_source(provenance: dict, by_key: dict, functions: dict) -> dict:
    """Resolve one check's *provenance* to a source location, or an honest ``located=false`` miss."""
    origin = (provenance or {}).get("origin")

    if origin in ("no_fault", "stopped_at"):
        fn_name = provenance.get("init_fn") if origin == "no_fault" else provenance.get("symbol")
        location = functions.get(fn_name)
        if location:
            note = ("whole-init invariant; verified after all of BSP_Init(), not at one line"
                    if origin == "no_fault" else "execution should reach this entry symbol")
            return {"located": True, "file": location["path"], "init_fn": fn_name,
                    "line": location["line"], "text": f"void {fn_name}(void)", "note": note}
        return {"located": False,
                "reason": f"no init function {fn_name!r} in the rendered source"}

    join = _ORIGIN_JOIN.get(origin) if origin is not None else None
    if join is None:
        return {"located": False, "reason": f"unknown provenance origin {origin!r}"}
    tag, key_field = join
    key = provenance.get(key_field)
    hit = by_key.get((tag, key))
    if hit:
        return {"located": True, "file": hit["path"], "init_fn": hit["init_fn"],
                "line": hit["line"], "text": hit["text"]}
    return {"located": False,
            "reason": f"no {origin} construct for {key!r} in the rendered init (it may be an "
                      "un-emitted TODO/unresolved, or the plan changed since the spec was synthesized)"}


def annotate_spec_sources(spec: dict, source_maps) -> dict:
    """Fill each check's ``provenance.source`` in place from *source_maps*; return located counts.

    Checks without a ``provenance`` (e.g. a hand-authored spec) are left untouched.
    """
    by_key, functions = _index(source_maps)
    located = unlocated = 0
    for check in spec.get("checks", []):
        provenance = check.get("provenance")
        if not provenance:
            continue
        source = resolve_source(provenance, by_key, functions)
        provenance["source"] = source
        if source.get("located"):
            located += 1
        else:
            unlocated += 1
    return {"located": located, "unlocated": unlocated}
