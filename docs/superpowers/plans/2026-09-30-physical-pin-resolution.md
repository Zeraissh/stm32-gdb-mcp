# Physical-First Pin Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace name-only pin-function guessing with layered, evidence-based resolution: package-pin → port-pin from ST's official pin data (physical truth), AF-candidate corroboration (strong evidence), net-name heuristics as hint/fallback (weak evidence) — so boards like WR350 (nets `U2TX`, `SW_CLK`, `SW_DIO`) resolve correctly and `no_debug_pins` / `no_reset_pin` warnings become physically grounded.

**Architecture:** Four layers, each honest about its evidence. (1) `pin_data_import.py` extracts a **position map** (`Position` → port pin, ALL pin types incl. Reset/Power/Boot) from each MCU XML and emits it per RefName under a reserved `_positions` key in the capability DB. (2) `PinCapabilityDB` gains `position_map()` (RefName wildcard-match against the board's part number) and `candidates()`. (3) A new pure module `pin_resolver.py` enriches a BoardDescription: fills `port_pin` physically, corroborates or infers `function` against AF candidates (net name is only a hint, the DB is the arbiter), and tags every filled field with its evidence source. (4) `validate_board` / `import_netlist` run the resolver when a DB is available; missing-critical checks (debug/reset) use physical candidates instead of name-matched functions.

**Tech Stack:** Python 3.10+, stdlib only, pytest, ruff. Real-data verification against ST's `STM32_open_pin_data` (STM32L151) and a real 143-component Altium export (WR350).

**Evidence layers (the contract every task honors):**

| Layer | Source | Fills | Source tag |
|---|---|---|---|
| Physical | position map (ST data) | `port_pin` | `"package_position"` |
| Strong | name-inference AND DB candidate agree | `function` | `"name+db"` |
| Medium | net-token hint matches exactly ONE DB candidate | `function` | `"db_hint"` |
| Weak | name-inference only (no DB / not in DB) | `function` | `"name_only"` |
| Fixed function | NRST / BOOT0 position pins | `function` | `"package_position"` |

Never fabricated: no position map → `port_pin` stays `None`; zero or multiple candidate matches → `function` stays `None`; disagreeing sources produce a warning, never a silent override.

## Global Constraints

- Line length 120, ruff rules E/F/I/UP/B, E501 ignored (from `pyproject.toml`).
- Target Python 3.10 (`target-version = "py310"`); stdlib only.
- Plain-dict models only (JSON-native house style) — no dataclasses.
- `pin_data_import.py` stays a PURE module: no imports from the rest of the package.
- Tests run with `python -m pytest` from the repo root (`pythonpath = ["src"]`).
- Honesty rule (house-wide): absent data → `None` + warning; never a guess.
- Keep `python -m ruff check .`, `python -m mypy`, and `python -m pytest` green after every task.
- Work on branch `feat/pin-resolution`.

---

### Task 1: Importer extracts the position map

**Files:**
- Modify: `src/mcp_server/pin_data_import.py`
- Test: `tests/test_pin_data_import.py` (append)

**Interfaces:**
- Consumes: MCU XML `<Pin Name=".." Position=".." Type="..">` elements (verified against the real `STM32L151CCUx.xml`: every Pin has `Position`; `Type` is `I/O` / `Reset` / `Power` / `Boot`).
- Produces:
  - `parse_mcu_xml(text)` return dict gains `"positions": {position_str: port_pin}` covering **all** pin Types (I/O names normalized via `normalize_port_pin` — `"PC14-OSC32_IN"` → `"PC14"`; non-I/O names pass through unchanged — `"NRST"` → `"NRST"`).
  - `build_db(...)` emits, inside each line scope, `"_positions": {"<RefName>": {position_str: port_pin}}` (per-RefName, so different packages of one line never merge).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pin_data_import.py`:

```python
MCU_XML_POSITIONS = """\
<Mcu Family="STM32L1" Line="STM32L151/152" Package="UFQFPN48" RefName="STM32L151CCUx"
     DBVersion="V3.0" xmlns="http://dummy.com">
  <IP Name="GPIO" Version="STM32L152xC_gpio_v1_0"/>
  <Pin Name="NRST" Position="7" Type="Reset"/>
  <Pin Name="VSSA" Position="8" Type="Power"/>
  <Pin Name="PA2" Position="12" Type="I/O">
    <Signal Name="ADC_IN2"/>
    <Signal Name="USART2_TX"/>
  </Pin>
  <Pin Name="PC14-OSC32_IN" Position="3" Type="I/O">
    <Signal Name="RCC_OSC32_IN"/>
  </Pin>
  <Pin Name="BOOT0" Position="44" Type="Boot"/>
</Mcu>
"""


def test_parse_mcu_xml_extracts_positions_for_all_pin_types():
    mcu = parse_mcu_xml(MCU_XML_POSITIONS)

    assert mcu["positions"] == {
        "3": "PC14",   # I/O name normalized
        "7": "NRST",   # Reset pin kept
        "8": "VSSA",   # Power pin kept
        "12": "PA2",
        "44": "BOOT0",  # Boot pin kept
    }


def test_build_db_emits_positions_per_ref_name():
    mcu = parse_mcu_xml(MCU_XML_POSITIONS)
    db = build_db([mcu], {}, source="test")

    scope = db["STM32L151"]
    assert scope["_positions"] == {
        "STM32L151CCUx": {"3": "PC14", "7": "NRST", "8": "VSSA", "12": "PA2", "44": "BOOT0"}
    }
    # The pins table is untouched: only I/O pins with peripheral signals.
    assert {(e["peripheral"], e["signal"]) for e in scope["PA2"]} == {("ADC", "IN2"), ("USART2", "TX")}
    assert "NRST" not in scope  # non-I/O pins never enter the AF table
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_data_import.py -k positions -v`
Expected: FAIL — `KeyError: 'positions'`.

- [ ] **Step 3: Write the implementation**

In `src/mcp_server/pin_data_import.py`, extend `parse_mcu_xml`. Replace the pin loop and return dict:

```python
    pins: dict[str, list[dict]] = {}
    positions: dict[str, str] = {}
    for pin in root.findall(f"{ns}Pin"):
        name = pin.get("Name", "")
        port_pin = normalize_port_pin(name)
        position = pin.get("Position")
        if port_pin and position:
            positions[position] = port_pin
        if pin.get("Type") != "I/O":
            continue
        if not port_pin:
            continue
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for sig in pin.findall(f"{ns}Signal"):
            pair = split_signal(sig.get("Name", ""))
            if pair is None or pair in seen:
                continue
            seen.add(pair)
            entries.append({"peripheral": pair[0], "signal": pair[1]})
        if entries:
            pins[port_pin] = entries
    ref_name = root.get("RefName")
    key_line_match = _REF_LINE_RE.match(ref_name) if ref_name else None
    return {
        "ref_name": ref_name,
        "family": root.get("Family"),
        "line": root.get("Line"),
        "key_line": key_line_match.group(0).upper() if key_line_match else None,
        "package": root.get("Package"),
        "db_version": root.get("DBVersion"),
        "gpio_version": gpio_version,
        "pins": pins,
        "positions": positions,
    }
```

Then in `build_db`, after `table = db.setdefault(scope, {})`, add the per-RefName position emission:

```python
        if mcu["ref_name"] and mcu.get("positions"):
            table.setdefault("_positions", {})[mcu["ref_name"]] = mcu["positions"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: all tests pass (24 pre-existing + 2 new). Watch for regressions in `test_generated_db_round_trips_through_capability_db` — the `_positions` key must flow through `PinCapabilityDB` harmlessly (it ignores non-list entries).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/pin_data_import.py tests/test_pin_data_import.py
git commit -m "feat: extract package-pin position map into capability DB (_positions per RefName)"
```

---

### Task 2: `PinCapabilityDB` position lookup + AF candidates

**Files:**
- Modify: `src/mcp_server/board_validation.py`
- Test: `tests/test_board_validation.py` (append)

**Interfaces:**
- Consumes: the `_positions` DB shape from Task 1.
- Produces (used by Task 3's resolver):
  - `PinCapabilityDB.position_map(line, family, part_normalized) -> dict | None` — finds the scope's `_positions`, wildcard-matches RefNames against `part_normalized` (`x` and `(...)` groups each match one alphanumeric run, e.g. RefName `STM32L151CCUx` matches part `STM32L151CCU6`). Exactly one match → its map; zero → `None`; multiple matches with IDENTICAL maps → the map; multiple DIFFERING maps → `None` (cannot arbitrate honestly).
  - `PinCapabilityDB.candidates(line, family, port_pin) -> list[dict] | None` — the AF entries for a port pin; `None` when scope or pin unknown.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_board_validation.py`:

```python
POSITION_DB = {
    "_meta": {"source": "test", "generated_by": "test", "db_version": None,
              "ref_names": ["STM32L151CCUx"], "warnings": []},
    "STM32L151": {
        "PA2": [{"peripheral": "ADC", "signal": "IN2"},
                {"peripheral": "USART2", "signal": "TX", "af": 4}],
        "PA13": [{"peripheral": "SYS", "signal": "JTMS-SWDIO"}],
        "PA14": [{"peripheral": "SYS", "signal": "JTCK-SWCLK"}],
        "_positions": {"STM32L151CCUx": {"7": "NRST", "12": "PA2", "34": "PA13", "37": "PA14"}},
    },
}


def test_position_map_matches_part_against_ref_name_wildcards():
    db = PinCapabilityDB(POSITION_DB)

    pmap = db.position_map("STM32L151", "STM32L1", "STM32L151CCU6")
    assert pmap == {"7": "NRST", "12": "PA2", "34": "PA13", "37": "PA14"}


def test_position_map_returns_none_without_match():
    db = PinCapabilityDB(POSITION_DB)

    assert db.position_map("STM32L151", "STM32L1", "STM32L431CBT6") is None
    assert db.position_map(None, None, "STM32L151CCU6") is None
    assert PinCapabilityDB({}).position_map("STM32L151", "STM32L1", "STM32L151CCU6") is None


def test_candidates_returns_af_entries_or_none():
    db = PinCapabilityDB(POSITION_DB)

    assert db.candidates("STM32L151", "STM32L1", "PA13") == [{"peripheral": "SYS", "signal": "JTMS-SWDIO"}]
    assert db.candidates("STM32L151", "STM32L1", "PA99") is None
    assert db.candidates("STM32L151", "STM32L1", None) is None
    # The reserved _positions key is not a port pin and must not leak through:
    assert db.candidates("STM32L151", "STM32L1", "_positions") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_board_validation.py -k "position_map or candidates" -v`
Expected: FAIL — `AttributeError: 'PinCapabilityDB' object has no attribute 'position_map'`.

- [ ] **Step 3: Write the implementation**

In `src/mcp_server/board_validation.py`, add a module-level helper after the imports, and two methods to `PinCapabilityDB`:

```python
import re

def _ref_name_matches(ref_name: str, part_normalized: str) -> bool:
    """Wildcard-match an ST RefName (``STM32L151CCUx``) against a concrete part
    (``STM32L151CCU6``). ``x`` and ``(...)`` groups each match one or more
    alphanumerics; everything else is literal."""
    pattern = re.escape(ref_name)
    pattern = re.sub(r"\\\(.*?\\\)", r"[A-Z0-9]+", pattern)  # escaped (B-C) groups
    pattern = pattern.replace("x", "[A-Z0-9]")
    return re.fullmatch(pattern, part_normalized, flags=re.IGNORECASE) is not None
```

```python
    def position_map(self, line, family, part_normalized) -> dict | None:
        """Return the package-pin → port-pin map for a concrete part number.

        Wildcard-matches the scope's per-RefName position tables against
        ``part_normalized``. Multiple matches only resolve when every matched
        table is identical; differing tables return ``None`` — never arbitrated.
        """
        pins = self._pins_for(line, family)
        if not pins or not part_normalized:
            return None
        tables = pins.get("_positions")
        if not isinstance(tables, dict):
            return None
        matches = [table for ref, table in tables.items()
                   if _ref_name_matches(ref, part_normalized)]
        if not matches:
            return None
        first = matches[0]
        if all(table == first for table in matches[1:]):
            return dict(first)
        return None

    def candidates(self, line, family, port_pin) -> list[dict] | None:
        """Return the AF candidate entries for a port pin, or ``None`` when unknown."""
        pins = self._pins_for(line, family)
        if pins is None or not port_pin or port_pin.startswith("_"):
            return None
        entries = pins.get(port_pin)
        return entries if isinstance(entries, list) else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_board_validation.py -v`
Expected: all tests pass (pre-existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/board_validation.py tests/test_board_validation.py
git commit -m "feat: PinCapabilityDB position_map (RefName wildcard) and AF candidates lookup"
```

---

### Task 3: `pin_resolver.py` — layered evidence resolution

**Files:**
- Create: `src/mcp_server/pin_resolver.py`
- Test: `tests/test_pin_resolver.py`

**Interfaces:**
- Consumes: `PinCapabilityDB.position_map` / `.candidates` (Task 2); a BoardDescription whose `mcu.pins` carry `package_pin`, `port_pin`, `net`, `function` (board_model schema).
- Produces:
  - `resolve_board(board: dict, db) -> dict` — returns an enriched COPY (input untouched). Per MCU pin, may set `port_pin` (+ `"port_pin_source"`), `function` (+ `"function_source"`), per the evidence-layer table at the top. Adds `board["resolution_notes"]` (list of human-readable findings, e.g. netlist/physical port_pin disagreements). Idempotent.

Resolution algorithm per pin (in order; first hit wins):
1. **Physical port_pin:** if `port_pin` is None and position map has `str(package_pin)` → set it, `port_pin_source="package_position"`. If both exist and DIFFER → keep the netlist value, append a `resolution_notes` warning.
2. **Fixed functions:** `port_pin` == `NRST` → `function={"peripheral": "SYS", "signal": "NRST"}`; == `BOOT0` → `{"peripheral": "SYS", "signal": "BOOT0"}`; `function_source="package_position"` (only when `function` was None).
3. **Corroboration:** name-inferred `function` present AND in DB candidates → `function_source="name+db"`; present but NOT in candidates (or no DB) → `"name_only"`.
4. **DB hint:** `function` is None, `port_pin` and candidates known → filter candidates whose last `-`/`_` token is a suffix of the normalized net label (non-alnum stripped, uppercased; e.g. net `U2TX` → `U2TX`, candidate `USART2_TX` token `TX` matches, `ADC_IN2` token `IN2` does not). Exactly one match → set it, `function_source="db_hint"`; zero or 2+ → leave None.

Debug-signal normalization (SYS candidates): `JTMS-SWDIO` → `("SWD", "SWDIO")`, `JTCK-SWCLK` → `("SWD", "SWCLK")`, `JTDO-TRACESWO` → `("SWD", "SWO")`, `JTDI` → `("JTAG", "JTDI")`, `JTRST`/`NJTRST` → `("JTAG", "NJTRST")` — applied when a db_hint match lands on a `SYS` debug candidate, so `validate_board`'s `{"SWD", "JTAG"}` check sees them.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pin_resolver.py`:

```python
"""pin_resolver: layered physical-first pin resolution."""

from mcp_server.board_validation import PinCapabilityDB
from mcp_server.pin_resolver import resolve_board

DB = PinCapabilityDB({
    "_meta": {"source": "t", "generated_by": "t", "db_version": None,
              "ref_names": ["STM32L151CCUx"], "warnings": []},
    "STM32L151": {
        "PA2": [{"peripheral": "ADC", "signal": "IN2"},
                {"peripheral": "USART2", "signal": "TX", "af": 4}],
        "PA13": [{"peripheral": "SYS", "signal": "JTMS-SWDIO"}],
        "PA14": [{"peripheral": "SYS", "signal": "JTCK-SWCLK"}],
        "PA9": [{"peripheral": "USART1", "signal": "TX", "af": 7},
                {"peripheral": "TIM1", "signal": "CH2", "af": 1}],
        "_positions": {"STM32L151CCUx": {
            "7": "NRST", "12": "PA2", "30": "PA9", "34": "PA13", "37": "PA14"}},
    },
})


def _board(pins):
    return {
        "source": "test", "format": "altium",
        "mcu": {"ref": "U6", "part": "STM32L151CCU6", "part_normalized": "STM32L151CCU6",
                "family": "STM32L1", "line": "STM32L151", "pins": pins},
        "components": [], "nets": [], "power_nets": {"power": [], "ground": ["GND"]},
        "warnings": [], "stats": {},
    }


def test_db_hint_resolves_u2tx_via_physical_candidate():
    board = _board([{"package_pin": "12", "port_pin": None, "net": "U2TX", "function": None}])
    out = resolve_board(board, DB)

    pin = out["mcu"]["pins"][0]
    assert pin["port_pin"] == "PA2"
    assert pin["port_pin_source"] == "package_position"
    assert pin["function"] == {"peripheral": "USART2", "signal": "TX"}
    assert pin["function_source"] == "db_hint"
    assert board["mcu"]["pins"][0]["port_pin"] is None  # input untouched


def test_swd_pins_resolve_via_db_hint():
    board = _board([
        {"package_pin": "34", "port_pin": None, "net": "SW_DIO", "function": None},
        {"package_pin": "37", "port_pin": None, "net": "SW_CLK", "function": None},
    ])
    out = resolve_board(board, DB)
    dio, clk = out["mcu"]["pins"]

    assert dio["port_pin"] == "PA13"
    assert dio["function"] == {"peripheral": "SWD", "signal": "SWDIO"}
    assert clk["function"] == {"peripheral": "SWD", "signal": "SWCLK"}
    assert clk["function_source"] == "db_hint"


def test_fixed_function_reset_pin():
    board = _board([{"package_pin": "7", "port_pin": None, "net": "NRST", "function": None}])
    pin = resolve_board(board, DB)["mcu"]["pins"][0]

    assert pin["port_pin"] == "NRST"
    assert pin["function"] == {"peripheral": "SYS", "signal": "NRST"}
    assert pin["function_source"] == "package_position"


def test_name_inference_corroborated_by_db():
    board = _board([{"package_pin": "30", "port_pin": None, "net": "USART1_TX",
                     "function": {"peripheral": "USART1", "signal": "TX"}}])
    pin = resolve_board(board, DB)["mcu"]["pins"][0]

    assert pin["port_pin"] == "PA9"
    assert pin["function_source"] == "name+db"


def test_ambiguous_hint_leaves_function_none():
    # PA9 candidates: TX (matches) and CH2 (no); make the net hint hit BOTH by
    # using a candidate set where two signals share the hint token.
    db = PinCapabilityDB({
        "STM32L0": {
            "PA1": [{"peripheral": "USART2", "signal": "TX"},
                    {"peripheral": "TIM2", "signal": "TX"}],
            "_positions": {"STM32L071CBTx": {"5": "PA1"}},
        },
    })
    board = _board([{"package_pin": "5", "port_pin": None, "net": "LINK_TX", "function": None}])
    board["mcu"].update(part="STM32L071CBT6", part_normalized="STM32L071CBT6",
                        family="STM32L0", line="STM32L0")
    pin = resolve_board(board, db)["mcu"]["pins"][0]

    assert pin["port_pin"] == "PA1"      # physical layer still resolves
    assert pin["function"] is None       # ambiguous hint: honest None


def test_no_position_map_keeps_everything_as_is():
    db = PinCapabilityDB({"STM32F4": {"PA1": []}})
    board = _board([{"package_pin": "12", "port_pin": None, "net": "U2TX", "function": None}])
    pin = resolve_board(board, db)["mcu"]["pins"][0]

    assert pin["port_pin"] is None
    assert pin["function"] is None


def test_netlist_port_pin_disagreement_is_a_note_not_an_override():
    board = _board([{"package_pin": "12", "port_pin": "PB7", "net": "U2TX", "function": None}])
    out = resolve_board(board, DB)
    pin = out["mcu"]["pins"][0]

    assert pin["port_pin"] == "PB7"  # netlist value kept
    assert any("PB7" in n and "PA2" in n for n in out["resolution_notes"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server.pin_resolver'`.

- [ ] **Step 3: Write the implementation**

Create `src/mcp_server/pin_resolver.py`:

```python
"""Layered, evidence-first MCU pin resolution.

Given a BoardDescription and a PinCapabilityDB, enrich the MCU pin list using
physical evidence before name heuristics:

1. ``port_pin`` from the package-pin position map (ST pin data) — physical
   truth, independent of net naming. Tagged ``port_pin_source="package_position"``.
2. Fixed-function pins (NRST/BOOT0) get their ``function`` from position alone.
3. A name-inferred ``function`` corroborated by the DB's AF candidates is tagged
   ``"name+db"``; without corroboration it stays ``"name_only"``.
4. When no function is known, the net label is used as a HINT against the pin's
   legal candidates: exactly one candidate whose signal token suffix-matches the
   label wins (``"db_hint"``); zero or several matches leave ``function=None``.

SYS debug candidates (JTMS-SWDIO etc.) are normalized to SWD/JTAG functions so
``validate_board``'s debug-pin check sees them. Everything absent stays
``None`` — resolution never fabricates.
"""

import copy
import re

# SYS candidate signal -> normalized debug function.
_DEBUG_SIGNALS = {
    "JTMS-SWDIO": ("SWD", "SWDIO"),
    "JTCK-SWCLK": ("SWD", "SWCLK"),
    "JTDO-TRACESWO": ("SWD", "SWO"),
    "JTDI": ("JTAG", "JTDI"),
    "JTRST": ("JTAG", "NJTRST"),
    "NJTRST": ("JTAG", "NJTRST"),
}

# Fixed-function position pins (Type != I/O in ST's data, so no AF candidates).
_FIXED_FUNCTIONS = {
    "NRST": ("SYS", "NRST"),
    "BOOT0": ("SYS", "BOOT0"),
}


def _normalize_label(net_name: str) -> str:
    """Uppercase alnum-only label: ``SW_DIO`` -> ``SWDIO``."""
    return re.sub(r"[^A-Za-z0-9]", "", net_name or "").upper()


def _signal_token(signal: str) -> str:
    """Last ``-``/``_`` token of a candidate signal: ``JTCK-SWCLK`` -> ``SWCLK``."""
    return re.split(r"[-_]", signal)[-1].upper()


def _hint_matches(candidates: list[dict], net_name: str) -> list[dict]:
    """Candidates whose signal token suffix-matches the normalized net label."""
    label = _normalize_label(net_name)
    if not label:
        return []
    return [c for c in candidates
            if c.get("signal") and label.endswith(_signal_token(c["signal"]))]


def _debug_function(signal: str) -> dict | None:
    mapped = _DEBUG_SIGNALS.get(signal)
    return {"peripheral": mapped[0], "signal": mapped[1]} if mapped else None


def _resolve_pin(pin: dict, position_map: dict | None, candidates_of, notes: list) -> None:
    package_pin = pin.get("package_pin")
    physical = position_map.get(str(package_pin)) if position_map else None
    if physical:
        if pin.get("port_pin") is None:
            pin["port_pin"] = physical
            pin["port_pin_source"] = "package_position"
        elif pin["port_pin"] != physical:
            notes.append(
                f"pin {package_pin}: netlist says port_pin {pin['port_pin']} but the package "
                f"position maps to {physical}; keeping the netlist value.")

    port_pin = pin.get("port_pin")
    function = pin.get("function")

    if function is None and port_pin in _FIXED_FUNCTIONS:
        peripheral, signal = _FIXED_FUNCTIONS[port_pin]
        pin["function"] = {"peripheral": peripheral, "signal": signal}
        pin["function_source"] = "package_position"
        return

    candidates = candidates_of(port_pin) if port_pin else None
    if function is not None:
        # An already-tagged function (db_hint/package_position from an earlier
        # pass) keeps its provenance — resolve_board must be idempotent.
        if "function_source" in pin:
            return
        pin["function_source"] = "name_only"
        if candidates:
            pair = (function.get("peripheral"), function.get("signal"))
            if any((c.get("peripheral"), c.get("signal")) == pair for c in candidates):
                pin["function_source"] = "name+db"
        return

    if not candidates:
        return
    matches = _hint_matches(candidates, pin.get("net") or "")
    if len(matches) != 1:
        return
    match = matches[0]
    debug = _debug_function(match["signal"]) if match.get("peripheral") == "SYS" else None
    pin["function"] = debug or {"peripheral": match["peripheral"], "signal": match["signal"]}
    pin["function_source"] = "db_hint"


def resolve_board(board: dict, db) -> dict:
    """Return an enriched copy of *board* with physically-resolved MCU pins."""
    out = copy.deepcopy(board)
    notes: list[str] = []
    mcu = out.get("mcu")
    if mcu and db is not None:
        position_map = db.position_map(mcu.get("line"), mcu.get("family"),
                                       mcu.get("part_normalized"))
        candidates_of = lambda port_pin: db.candidates(  # noqa: E731
            mcu.get("line"), mcu.get("family"), port_pin)
        for pin in mcu.get("pins", []):
            _resolve_pin(pin, position_map, candidates_of, notes)
    out["resolution_notes"] = notes
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_resolver.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/pin_resolver.py tests/test_pin_resolver.py
git commit -m "feat: layered physical-first pin resolver (position map, corroboration, db hints)"
```

---

### Task 4: Wire resolution into `import_netlist` / `validate_board`

**Files:**
- Modify: `src/mcp_server/tools/board_tools.py` (import_netlist gains optional `db_path`; both tools resolve when a DB loads)
- Modify: `src/mcp_server/board_validation.py` (`validate_board` runs `resolve_board` internally when a DB is supplied; `_detect_missing_critical` gains physical debug/reset detection)
- Test: `tests/test_board_validation.py` (append), `tests/test_pin_resolver.py` (append)

**Interfaces:**
- Consumes: `resolve_board` (Task 3); the existing `db_path` arg / `STM32_GDB_MCP_PIN_DB` env resolution in the `validate_board` tool.
- Produces:
  - `validate_board(board, capability_db)` resolves first when `capability_db` is not None, then runs all checks on the RESOLVED board. `_detect_missing_critical(board, pins, db)` treats debug as present when any MCU pin's `function` is SWD/JTAG **or** its port_pin has a SYS SWD/JTAG candidate in the DB; reset as present when any pin's `port_pin` == `NRST` **or** `function` is SYS/NRST.
  - `import_netlist` schema gains optional `"db_path"`; when a DB loads, the stashed board is the resolved one and the summary adds `resolution` counts.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_board_validation.py`:

```python
def test_validate_board_detects_debug_and_reset_physically():
    db = PinCapabilityDB(POSITION_DB)
    board = {
        "source": "t", "format": "altium",
        "mcu": {"ref": "U6", "part": "STM32L151CCU6", "part_normalized": "STM32L151CCU6",
                "family": "STM32L1", "line": "STM32L151",
                "pins": [
                    {"package_pin": "7", "port_pin": None, "net": "NRST", "function": None},
                    {"package_pin": "34", "port_pin": None, "net": "SW_DIO", "function": None},
                    {"package_pin": "37", "port_pin": None, "net": "SW_CLK", "function": None},
                ]},
        "components": [], "nets": [], "power_nets": {"power": ["VDD"], "ground": ["GND"]},
        "warnings": [], "stats": {},
    }
    report = validate_board(board, db)

    warning_types = {w["type"] for w in report["warnings"]}
    assert "no_debug_pins" not in warning_types
    assert "no_reset_pin" not in warning_types
```

(Note: `POSITION_DB` was defined in Task 2's tests, same file.)

Append to `tests/test_pin_resolver.py`:

```python
def test_resolve_board_is_idempotent():
    board = _board([{"package_pin": "12", "port_pin": None, "net": "U2TX", "function": None}])
    once = resolve_board(board, DB)
    twice = resolve_board(once, DB)

    assert twice["mcu"]["pins"][0] == once["mcu"]["pins"][0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_board_validation.py -k physically -v`
Expected: FAIL — `no_debug_pins` still in warning types (name-based check can't see SW_DIO).

- [ ] **Step 3: Write the implementation**

In `src/mcp_server/board_validation.py`, add the import at the top (below `import json`):

```python
from mcp_server.pin_resolver import resolve_board
```

Replace `_detect_missing_critical` with:

```python
def _detect_missing_critical(board: dict, pins: list[dict], db: PinCapabilityDB | None = None,
                             line=None, family=None) -> list[dict]:
    warnings = []
    power = board.get("power_nets") or {}
    if not power.get("power"):
        warnings.append({"type": "no_power_net", "detail": "No power net detected (VDD/VCC/+3V3/...)."})
    if not power.get("ground"):
        warnings.append({"type": "no_ground_net", "detail": "No ground net detected (GND/VSS)."})

    functions = {(p["function"]["peripheral"], p["function"]["signal"]) for p in pins if p.get("function")}
    peripherals = {peripheral for peripheral, _ in functions}

    debug_found = bool({"SWD", "JTAG"} & peripherals)
    reset_found = ("SYS", "NRST") in functions
    if db is not None:
        for pin in pins:
            port_pin = pin.get("port_pin")
            if not port_pin:
                continue
            if port_pin == "NRST":
                reset_found = True
            for candidate in db.candidates(line, family, port_pin) or []:
                signal = candidate.get("signal") or ""
                if candidate.get("peripheral") == "SYS" and ("SWD" in signal or signal.startswith("JT")):
                    debug_found = True
    if not debug_found:
        warnings.append({"type": "no_debug_pins", "detail": "No SWD/JTAG debug pins found; on-chip debug may be unavailable."})
    if not reset_found:
        warnings.append({"type": "no_reset_pin", "detail": "No NRST reset net found."})
    return warnings
```

In `validate_board`, resolve first and pass db context through:

```python
def validate_board(board: dict, capability_db: PinCapabilityDB | None = None) -> dict:
    """Validate a BoardDescription; return a structured conflict/warning report."""
    if capability_db is not None:
        board = resolve_board(board, capability_db)
    mcu = board.get("mcu")
    pins = _mcu_pins(board)
    # ... unchanged ...
    warnings = _detect_missing_critical(
        board, pins, capability_db,
        mcu.get("line") if mcu else None, mcu.get("family") if mcu else None)
```

(Everything else in `validate_board` stays as-is; `unassigned_pins` now reflects resolved functions automatically.)

In `src/mcp_server/tools/board_tools.py`, add to the `import_netlist` schema properties (after `"format"`):

```python
            "db_path": {"type": "string", "description":
                        "Optional JSON pin-capability DB; when given (or STM32_GDB_MCP_PIN_DB is set), "
                        "pins are resolved physically (package position -> port pin, AF corroboration)."},
```

and in `import_netlist`, after `ctx.board["current"] = parsed`, insert resolution:

```python
    resolved_note = None
    db_path = arguments.get("db_path") or os.environ.get("STM32_GDB_MCP_PIN_DB")
    if db_path:
        try:
            from ..pin_resolver import resolve_board
            capability_db = load_capability_db(db_path)
            before = sum(1 for p in (parsed.get("mcu") or {}).get("pins", []) if p.get("function"))
            parsed = resolve_board(parsed, capability_db)
            after = sum(1 for p in (parsed.get("mcu") or {}).get("pins", []) if p.get("function"))
            resolved_note = {"db_path": db_path, "functions_before": before, "functions_after": after,
                             "notes": parsed.get("resolution_notes", [])}
        except (OSError, ValueError) as e:
            return [content_error(
                f"Failed to load pin-capability DB: {e}", code="db_load_error",
                suggested_next_actions=["import_netlist without db_path"])]
    ctx.board["current"] = parsed
```

Then pass `resolved_note` into the summary data: change the `content_success(summarize_board(parsed), ...)` call to merge it:

```python
    summary = summarize_board(parsed)
    if resolved_note:
        summary["resolution"] = resolved_note
    return [content_success(
        summary,
        suggested_next_actions=["describe_board (what=pins)", "describe_board (what=peripherals)"])]
```

(Move the `ctx.board["current"] = parsed` line so it stores the RESOLVED board — the snippet above already reassigns before stashing; delete the original stash line.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_board_validation.py tests/test_pin_resolver.py -v`
Expected: all pass, including the two new ones.

- [ ] **Step 5: Commit**

```bash
git add src/mcp_server/board_validation.py src/mcp_server/tools/board_tools.py \
        tests/test_board_validation.py tests/test_pin_resolver.py
git commit -m "feat: physical debug/reset detection and DB-backed resolution in board tools"
```

---

### Task 5: Real-data smoke + verification gate + docs

**Files:**
- No new source files. Modify: `CHANGELOG.md` (new Unreleased entry).

**Interfaces:**
- Consumes: everything above.
- Produces: real-evidence verification on the WR350 board + a clean repo-wide gate.

- [ ] **Step 1: Generate the STM32L151 capability DB from ST's official data**

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/STMicroelectronics/STM32_open_pin_data .tmp_open_pin_data
cd .tmp_open_pin_data && git sparse-checkout set --no-cone '/mcu/IP/GPIO-STM32L1*' '/mcu/STM32L15*' && cd ..
python scripts/import_pin_data.py --source .tmp_open_pin_data --mcu 'STM32L151CCUx' -o .tmp_caps_l151.json
```

Expected: `wrote .tmp_caps_l151.json: 1 line(s) ['STM32L151'], <N> entries, 0 warning(s)`.

- [ ] **Step 2: Resolve the real WR350 board and verify the three canonical cases**

Copy the real netlist into the workspace first (it lives outside the repo), then:

```bash
cp "<WR350 .NET path>" .tmp_wr350.net
python - << 'EOF'
import json
from mcp_server.board_validation import load_capability_db, validate_board
from mcp_server.netlist_parser import load_netlist_file

db = load_capability_db(".tmp_caps_l151.json")
board = load_netlist_file(".tmp_wr350.net")
report = validate_board(board, db)

pins = {p["package_pin"]: p for p in __import__("mcp_server.pin_resolver", fromlist=["resolve_board"])
        .resolve_board(board, db)["mcu"]["pins"]}
print("U2TX pin 12 ->", pins["12"]["port_pin"], pins["12"]["function"], pins["12"]["function_source"])
print("SW_DIO    ->", [p for p in pins.values() if p["net"] == "SW_DIO"][0]["function"])
print("SW_CLK    ->", [p for p in pins.values() if p["net"] == "SW_CLK"][0]["function"])
print("warning types:", sorted({w["type"] for w in report["warnings"]}))
print("conflicts:", report["stats"]["conflict_count"])
EOF
```

Expected: pin 12 → `PA2` / `USART2_TX` / `db_hint`; SW_DIO → `SWD/SWDIO`; SW_CLK → `SWD/SWCLK`;
`no_debug_pins` and `no_reset_pin` ABSENT from warnings; conflict count 0.
If any expectation fails, debug and fix — do not weaken the assertions; the physical
facts were verified by hand against `STM32L151CCUx.xml` (Position 12 = PA2 = USART2_TX).

- [ ] **Step 3: Clean up temp artifacts**

```bash
rm -rf .tmp_open_pin_data .tmp_caps_l151.json .tmp_wr350.net
```

- [ ] **Step 4: CHANGELOG entry**

Insert under `## [Unreleased]` in `CHANGELOG.md`:

```markdown
### Physical-first pin resolution / 物理事实优先的引脚解析

- The capability DB now carries a **package-pin position map** (per RefName, from
  ST's official pin data), and a new resolver layers evidence honestly: physical
  position fills `port_pin`, AF candidates corroborate or hint `function`
  (`name+db` / `db_hint` / `name_only` / `package_position` source tags), and
  `validate_board` detects debug/reset pins physically — nets named `U2TX` or
  `SW_CLK` resolve correctly without touching the name table. Verified against a
  real STM32L151 board (143 components): `no_debug_pins` false positives gone. /
  能力库新增封装管脚位置映射，解析按证据强度分层，调试/复位检测改走物理真值；
  已在真实 STM32L151 板卡上验证。
```

- [ ] **Step 5: Full verification gate**

```bash
python -m ruff check .
python -m mypy
python -m pytest
python -m compileall src tests scripts
```

Expected: ruff clean; mypy clean; full suite green (1309 pre-existing + ~13 new);
compileall exits 0.

- [ ] **Step 6: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog entry for physical-first pin resolution"
```

---

## Self-Review Notes

- **Spec coverage:** the four-layer contract from the design discussion maps to Task 1 (physical data), Task 2 (lookup), Task 3 (resolution layers + source tags), Task 4 (validate_board physical debug/reset + tool wiring), Task 5 (real-data proof on the actual board that motivated this). The paired-corroboration idea (layer 4 in the discussion) is deliberately folded into `function_source` tags rather than a separate confidence field — YAGNI.
- **Placeholder scan:** none; every code step is complete. Test expectations were verified against the REAL `STM32L151CCUx.xml` fetched from ST's repo: Position 12 = PA2 (signals include `USART2_TX`), Position 34 = PA13 (`SYS_JTMS-SWDIO`), Position 37 = PA14 (`SYS_JTCK-SWCLK`), NRST at Position 7 (Type=Reset), BOOT0 at 44 (Type=Boot).
- **Type consistency:** `parse_mcu_xml` gains `positions` (consumed only by `build_db`); DB shape `{line: {port_pin: [...], "_positions": {ref: {pos: pin}}}}` — `af_map()` skips non-dict entries and `candidates()` rejects `_`-prefixed keys, so the reserved key cannot leak into AF logic; `position_map`/`candidates` signatures in Task 2 match their Task 3-4 call sites exactly; `resolve_board(board, db) -> dict` is the single entry point used by both `board_validation` and `board_tools`.
- **Backward compatibility:** old DBs without `_positions` → `position_map` returns None → resolver is a no-op except source tagging; `validate_board` without a DB keeps the old name-based checks (db=None path unchanged).
- **Deliberate scope cuts:** pincount-letter/package-letter tables (rejected — RefName wildcard matching needs no letter tables); loose substring hints beyond suffix-token matching; OrCAD part-value join (still out; OrCAD boards get resolution only when the MCU is identified another way).
