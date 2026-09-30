# Pin Data Importer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert ST's official pin data (STM32_open_pin_data checkout or CubeMX `db/` dir) into the capability DB JSON consumed by `board_validation.PinCapabilityDB`, offline and deterministically.

**Architecture:** A pure, stdlib-only parsing module `src/mcp_server/pin_data_import.py` (no imports from the rest of the package, plain dicts) plus a thin CLI wrapper `scripts/import_pin_data.py` following the existing `scripts/hil_rack.py` pattern. The MCP server is untouched; it keeps loading the generated JSON via `load_capability_db`.

**Tech Stack:** Python 3.10+, `xml.etree.ElementTree`, `argparse`, `json`, `re`, `difflib`; pytest for tests.

**Spec:** `docs/superpowers/specs/2026-09-03-pin-data-import-design.md`

## Global Constraints

- Line length 120, ruff rules E/F/I/UP/B, E501 ignored (from `pyproject.toml`).
- Target Python 3.10 (`target-version = "py310"`); no third-party imports in the new module.
- Tests run with `python -m pytest` from the repo root (`pythonpath = ["src"]`).
- XML files declare a default namespace `xmlns="http://dummy.com"` — every tag lookup MUST go through the namespace helper below; never search bare tag names.
- Capability DB output shape (consumed by `board_validation.PinCapabilityDB`):
  `{"<Line>": {"<port_pin>": [{"peripheral": str, "signal": str, "af": int?}, ...]}}` plus a top-level `"_meta"` dict.
- Honesty rule: never fabricate an `af`; entries without a numeric AF are emitted without the key.

## File Structure

- Create `src/mcp_server/pin_data_import.py` — all parsing/merge logic (pure).
- Create `scripts/import_pin_data.py` — CLI wrapper (argparse, discovery, JSON write).
- Create `tests/test_pin_data_import.py` — all tests with inline XML fixtures.

---

### Task 1: MCU XML parsing (pure module skeleton)

**Files:**
- Create: `src/mcp_server/pin_data_import.py`
- Test: `tests/test_pin_data_import.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces:
  - `normalize_port_pin(pin_name: str) -> str` — `"PA0-WKUP"` → `"PA0"`.
  - `split_signal(signal_name: str) -> tuple[str, str] | None` — `"ADC1_IN0"` → `("ADC1", "IN0")`; `None` for `"GPIO"`, `"EVENTOUT"`, or names without `_`.
  - `parse_mcu_xml(text: str) -> dict` — returns
    `{"ref_name": str|None, "family": str|None, "line": str|None, "package": str|None, "db_version": str|None, "gpio_version": str|None, "pins": {port_pin: [{"peripheral": str, "signal": str}]}}`.
    Raises `ValueError` on malformed XML or a root that is not `Mcu`.

- [x] **Step 1: Write the failing test**

Create `tests/test_pin_data_import.py`:

```python
"""pin_data_import: parse ST open_pin_data / CubeMX db XML into capability DB JSON."""

import pytest

from mcp_server.pin_data_import import (
    normalize_port_pin,
    parse_mcu_xml,
    split_signal,
)

MCU_XML_L4 = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<Mcu ClockTree="STM32L4" DBVersion="V3.0" Family="STM32L4" HasPowerPad="false" Line="STM32L431" Package="LQFP48" RefName="STM32L431C(B-C)Tx" xmlns="http://dummy.com">
    <IP InstanceName="USART1" Name="USART" Version="sci2_v2_1_Cube"/>
    <IP ConfigFile="GPIO-STM32L4xx" InstanceName="GPIO" Name="GPIO" Version="STM32L43x_gpio_v1_0"/>
    <Pin Name="VBAT" Position="1" Type="Power"/>
    <Pin Name="PA0-WKUP1" Position="6" Type="I/O">
        <Signal Name="ADC1_IN5"/>
        <Signal Name="USART2_CTS"/>
        <Signal IOModes="Input,Output,Analog,EVENTOUT,EXTI" Name="GPIO"/>
    </Pin>
    <Pin Name="PA9" Position="21" Type="I/O">
        <Signal Name="I2C1_SCL"/>
        <Signal Name="USART1_TX"/>
        <Signal IOModes="Input,Output,Analog,EVENTOUT,EXTI" Name="GPIO"/>
    </Pin>
    <Pin Name="NRST" Position="4" Type="Reset"/>
</Mcu>
"""


def test_normalize_port_pin_strips_suffix():
    assert normalize_port_pin("PA0-WKUP1") == "PA0"
    assert normalize_port_pin("PC14-OSC32_IN") == "PC14"
    assert normalize_port_pin("PA9") == "PA9"


def test_split_signal_first_underscore():
    assert split_signal("ADC1_IN5") == ("ADC1", "IN5")
    assert split_signal("RCC_OSC32_IN") == ("RCC", "OSC32_IN")
    assert split_signal("GPIO") is None
    assert split_signal("EVENTOUT") is None
    assert split_signal("VBAT") is None


def test_parse_mcu_xml_header_and_pins():
    parsed = parse_mcu_xml(MCU_XML_L4)

    assert parsed["ref_name"] == "STM32L431C(B-C)Tx"
    assert parsed["family"] == "STM32L4"
    assert parsed["line"] == "STM32L431"
    assert parsed["package"] == "LQFP48"
    assert parsed["db_version"] == "V3.0"
    assert parsed["gpio_version"] == "STM32L43x_gpio_v1_0"
    # Non-I/O pins excluded.
    assert set(parsed["pins"]) == {"PA0", "PA9"}
    assert parsed["pins"]["PA9"] == [
        {"peripheral": "I2C1", "signal": "SCL"},
        {"peripheral": "USART1", "signal": "TX"},
    ]
    # GPIO signal skipped, suffix normalized, order preserved.
    assert parsed["pins"]["PA0"] == [
        {"peripheral": "ADC1", "signal": "IN5"},
        {"peripheral": "USART2", "signal": "CTS"},
    ]


def test_parse_mcu_xml_rejects_non_mcu_root():
    with pytest.raises(ValueError, match="Mcu"):
        parse_mcu_xml('<Foo xmlns="http://dummy.com"/>')
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_server.pin_data_import'`

- [x] **Step 3: Write minimal implementation**

Create `src/mcp_server/pin_data_import.py`:

```python
"""Import ST's official pin data (STM32_open_pin_data checkout or CubeMX ``db/``)
into the capability DB JSON consumed by ``board_validation.PinCapabilityDB``.

Pure module: stdlib only, no imports from the rest of the package, everything
plain dicts. Generation is an offline, git-reviewable step; the MCP server only
ever loads the produced JSON. Never fabricates an ``af`` -- entries without a
numeric alternate function are emitted without the key.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

_AF_RE = re.compile(r"^GPIO_AF(\d+)_")
_SKIP_SIGNALS = frozenset({"GPIO", "EVENTOUT"})


def _ns(root: ET.Element) -> str:
    """Namespace prefix of the document, e.g. ``{http://dummy.com}`` ('' when absent)."""
    tag = root.tag
    return tag[: tag.index("}") + 1] if tag.startswith("{") else ""


def normalize_port_pin(pin_name: str) -> str:
    """``PA0-WKUP1`` -> ``PA0``; ``PC14-OSC32_IN`` -> ``PC14``."""
    return pin_name.split("-", 1)[0]


def split_signal(signal_name: str) -> tuple[str, str] | None:
    """Split ``PERIPHERAL_SIGNAL`` at the first underscore; None when not a peripheral signal."""
    if signal_name in _SKIP_SIGNALS or "_" not in signal_name:
        return None
    peripheral, signal = signal_name.split("_", 1)
    return (peripheral, signal)


def parse_mcu_xml(text: str) -> dict:
    """Parse one ``mcu/<RefName>.xml`` into header facts + per-port-pin signal pairs."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"malformed MCU XML: {e}") from e
    if not root.tag.endswith("Mcu"):
        raise ValueError(f"expected Mcu root element, got {root.tag!r}")
    ns = _ns(root)
    gpio_version = None
    for ip in root.findall(f"{ns}IP"):
        if ip.get("Name") == "GPIO":
            gpio_version = ip.get("Version")
    pins: dict[str, list[dict]] = {}
    for pin in root.findall(f"{ns}Pin"):
        if pin.get("Type") != "I/O":
            continue
        port_pin = normalize_port_pin(pin.get("Name", ""))
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
    return {
        "ref_name": root.get("RefName"),
        "family": root.get("Family"),
        "line": root.get("Line"),
        "package": root.get("Package"),
        "db_version": root.get("DBVersion"),
        "gpio_version": gpio_version,
        "pins": pins,
    }
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: 4 passed

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/pin_data_import.py tests/test_pin_data_import.py
git commit -m "feat: parse ST MCU XML into per-pin peripheral/signal pairs"
```

---

### Task 2: GPIO Modes XML parsing (numeric AF extraction)

**Files:**
- Modify: `src/mcp_server/pin_data_import.py` (append one function)
- Test: `tests/test_pin_data_import.py` (append tests)

**Interfaces:**
- Consumes: `normalize_port_pin`, `_ns` from Task 1.
- Produces: `parse_gpio_modes_xml(text: str) -> dict[tuple[str, str], int]` —
  maps `(port_pin, "PERIPHERAL_SIGNAL")` → numeric AF. AFIO-remap values
  (F1 family, `__HAL_AFIO_REMAP_*`) are simply absent from the table.

- [x] **Step 1: Write the failing test**

Append to `tests/test_pin_data_import.py`:

```python
from mcp_server.pin_data_import import parse_gpio_modes_xml

GPIO_MODES_L4 = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<IP DBVersion="V3.0" Name="GPIO" Version="STM32L43x_gpio_v1_0" xmlns="http://dummy.com">
    <GPIO_Pin PortName="PA" Name="PA9">
        <SpecificParameter Name="GPIO_Pin">
            <PossibleValue>GPIO_PIN_9</PossibleValue>
        </SpecificParameter>
        <PinSignal Name="I2C1_SCL">
            <SpecificParameter Name="GPIO_AF">
                <PossibleValue>GPIO_AF4_I2C1</PossibleValue>
            </SpecificParameter>
        </PinSignal>
        <PinSignal Name="USART1_TX">
            <SpecificParameter Name="GPIO_AF">
                <PossibleValue>GPIO_AF7_USART1</PossibleValue>
            </SpecificParameter>
        </PinSignal>
    </GPIO_Pin>
    <GPIO_Pin PortName="PA" Name="PA0-WKUP1">
        <PinSignal Name="ADC1_IN5"/>
    </GPIO_Pin>
</IP>
"""

GPIO_MODES_F1 = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<IP DBVersion="V3.0" Name="GPIO" Version="STM32F103x8_gpio_v1_0" xmlns="http://dummy.com">
    <GPIO_Pin PortName="PA" Name="PA0-WKUP">
        <PinSignal Name="TIM2_CH1">
            <RemapBlock Name="TIM2_REMAP0" DefaultRemap="true" />
            <RemapBlock Name="TIM2_REMAP2">
               <SpecificParameter Name="GPIO_AF">
                   <PossibleValue>__HAL_AFIO_REMAP_TIM2_PARTIAL_2</PossibleValue>
               </SpecificParameter>
            </RemapBlock>
        </PinSignal>
        <PinSignal Name="USART2_CTS">
            <RemapBlock Name="USART2_REMAP0" DefaultRemap="true" />
        </PinSignal>
    </GPIO_Pin>
</IP>
"""


def test_parse_gpio_modes_extracts_numeric_af():
    table = parse_gpio_modes_xml(GPIO_MODES_L4)

    assert table[("PA9", "I2C1_SCL")] == 4
    assert table[("PA9", "USART1_TX")] == 7
    # PinSignal without GPIO_AF parameter contributes nothing.
    assert ("PA0", "ADC1_IN5") not in table


def test_parse_gpio_modes_afio_remap_yields_no_numeric_af():
    # F1 uses the AFIO remap model; __HAL_AFIO_REMAP_* values are not numeric AFs.
    table = parse_gpio_modes_xml(GPIO_MODES_F1)

    assert table == {}
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_gpio_modes_xml'`

- [x] **Step 3: Write minimal implementation**

Append to `src/mcp_server/pin_data_import.py`:

```python
def parse_gpio_modes_xml(text: str) -> dict[tuple[str, str], int]:
    """Parse ``mcu/IP/GPIO-<version>_Modes.xml`` into ``{(port_pin, PERIPHERAL_SIGNAL): af}``.

    Only ``GPIO_AF<n>_*`` values count; AFIO remap values (F1 family) and signals
    without a ``GPIO_AF`` parameter contribute nothing -- an absent entry means
    "no numeric AF known", never a guess.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"malformed GPIO modes XML: {e}") from e
    ns = _ns(root)
    table: dict[tuple[str, str], int] = {}
    for gpio_pin in root.iter(f"{ns}GPIO_Pin"):
        port_pin = normalize_port_pin(gpio_pin.get("Name", ""))
        if not port_pin:
            continue
        for pin_signal in gpio_pin.findall(f"{ns}PinSignal"):
            signal_name = pin_signal.get("Name", "")
            for param in pin_signal.iter(f"{ns}SpecificParameter"):
                if param.get("Name") != "GPIO_AF":
                    continue
                for value in param.findall(f"{ns}PossibleValue"):
                    match = _AF_RE.match((value.text or "").strip())
                    if match:
                        table[(port_pin, signal_name)] = int(match.group(1))
    return table
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: 6 passed

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/pin_data_import.py tests/test_pin_data_import.py
git commit -m "feat: extract numeric alternate functions from GPIO modes XML"
```

---

### Task 3: DB assembly — merge, `_meta`, honest warnings

**Files:**
- Modify: `src/mcp_server/pin_data_import.py` (append one function)
- Test: `tests/test_pin_data_import.py` (append tests)

**Interfaces:**
- Consumes: `parse_mcu_xml` / `parse_gpio_modes_xml` outputs from Tasks 1-2.
- Produces: `build_db(mcus: list[dict], modes_by_version: dict[str, dict], source: str) -> dict` —
  the full capability DB JSON object: `{"_meta": {...}, "<Line>": {port_pin: [entries]}}`.
  `_meta` keys: `source` (str), `generated_by` (str), `db_version` (str|None),
  `ref_names` (list[str]), `warnings` (list[str]).
  Raises `ValueError` when an MCU has neither `line` nor `family`.

- [x] **Step 1: Write the failing test**

Append to `tests/test_pin_data_import.py`:

```python
from mcp_server.pin_data_import import build_db

MCU_XML_L4_TWIN = MCU_XML_L4.replace(
    'RefName="STM32L431C(B-C)Tx"', 'RefName="STM32L431CCUx"'
).replace(
    '<Pin Name="PA9" Position="21" Type="I/O">',
    '<Pin Name="PA10" Position="22" Type="I/O">',
).replace('Name="I2C1_SCL"', 'Name="I2C1_SDA"').replace('Name="USART1_TX"', 'Name="USART1_RX"')


def test_build_db_keys_by_line_and_attaches_af():
    mcu = parse_mcu_xml(MCU_XML_L4)
    modes = parse_gpio_modes_xml(GPIO_MODES_L4)

    db = build_db([mcu], {"STM32L43x_gpio_v1_0": modes}, source="/data/open_pin_data")

    assert set(db["STM32L431"]["PA9"]) == {
        frozenset({"peripheral": "I2C1", "signal": "SCL", "af": 4}.items()),
        frozenset({"peripheral": "USART1", "signal": "TX", "af": 7}.items()),
    }
    # ADC1_IN5 has no GPIO_AF parameter -> entry without "af".
    assert db["STM32L431"]["PA0"][0] == {"peripheral": "ADC1", "signal": "IN5"}
    assert db["_meta"]["source"] == "/data/open_pin_data"
    assert db["_meta"]["db_version"] == "V3.0"
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx"]
    assert db["_meta"]["warnings"] == []


def test_build_db_unions_ref_names_sharing_a_line():
    first = parse_mcu_xml(MCU_XML_L4)
    twin = parse_mcu_xml(MCU_XML_L4_TWIN)

    db = build_db([first, twin], {}, source="/data")

    assert sorted(db["_meta"]["ref_names"]) == ["STM32L431C(B-C)Tx", "STM32L431CCUx"]
    assert "PA9" in db["STM32L431"] and "PA10" in db["STM32L431"]


def test_build_db_missing_modes_file_warns_and_omits_af():
    mcu = parse_mcu_xml(MCU_XML_L4)

    db = build_db([mcu], {}, source="/data")

    assert db["_meta"]["warnings"] == [
        "no GPIO modes file for STM32L43x_gpio_v1_0 (STM32L431C(B-C)Tx)"
    ]
    assert all("af" not in e for pin in db["STM32L431"].values() for e in pin)


def test_build_db_requires_line_or_family():
    mcu = parse_mcu_xml(MCU_XML_L4)
    mcu["line"] = None
    mcu["family"] = None

    with pytest.raises(ValueError, match="line"):
        build_db([mcu], {}, source="/data")
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_db'`

- [x] **Step 3: Write minimal implementation**

Append to `src/mcp_server/pin_data_import.py`:

```python
GENERATED_BY = "stm32-gdb-mcp scripts/import_pin_data.py"


def build_db(mcus: list[dict], modes_by_version: dict[str, dict], source: str) -> dict:
    """Merge parsed MCUs into one capability DB keyed by Line, with provenance.

    ``modes_by_version`` maps a GPIO IP version string (from each MCU's
    ``gpio_version``) to the table returned by :func:`parse_gpio_modes_xml`.
    A missing table degrades honestly: entries keep no ``af`` and a warning is
    recorded in ``_meta``. Duplicate (peripheral, signal) entries across RefNames
    of one Line are merged; a later entry may contribute a missing ``af``.
    """
    db: dict = {
        "_meta": {
            "source": source,
            "generated_by": GENERATED_BY,
            "db_version": None,
            "ref_names": [],
            "warnings": [],
        }
    }
    for mcu in mcus:
        scope = mcu["line"] or mcu["family"]
        if not scope:
            raise ValueError(f"{mcu['ref_name']}: no line or family to key the DB by")
        if db["_meta"]["db_version"] is None:
            db["_meta"]["db_version"] = mcu["db_version"]
        if mcu["ref_name"]:
            db["_meta"]["ref_names"].append(mcu["ref_name"])
        modes = modes_by_version.get(mcu["gpio_version"])
        if mcu["gpio_version"] and modes is None:
            db["_meta"]["warnings"].append(
                f"no GPIO modes file for {mcu['gpio_version']} ({mcu['ref_name']})"
            )
        table = db.setdefault(scope, {})
        for port_pin, entries in mcu["pins"].items():
            bucket = table.setdefault(port_pin, [])
            by_pair = {(e["peripheral"], e["signal"]): e for e in bucket}
            for entry in entries:
                pair = (entry["peripheral"], entry["signal"])
                signal_name = f"{pair[0]}_{pair[1]}"
                af = modes.get((port_pin, signal_name)) if modes else None
                existing = by_pair.get(pair)
                if existing is not None:
                    if af is not None and "af" not in existing:
                        existing["af"] = af
                    continue
                merged = dict(entry)
                if af is not None:
                    merged["af"] = af
                bucket.append(merged)
                by_pair[pair] = merged
    db["_meta"]["ref_names"].sort()
    return db
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: 10 passed

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/pin_data_import.py tests/test_pin_data_import.py
git commit -m "feat: merge parsed MCUs into provenance-carrying capability DB"
```

---

### Task 4: Round-trip through the real consumers

**Files:**
- Test: `tests/test_pin_data_import.py` (append tests)

**Interfaces:**
- Consumes: `build_db` from Task 3; the repo's existing
  `board_validation.load_capability_db(path) -> PinCapabilityDB`,
  `PinCapabilityDB.supports(line, family, port_pin, peripheral, signal) -> bool | None`,
  `PinCapabilityDB.af_map() -> dict`.
- Produces: no new API; proves the generated JSON is loadable and `_meta` is inert.

- [x] **Step 1: Write the failing test**

Append to `tests/test_pin_data_import.py`:

```python
import json

from mcp_server.board_validation import load_capability_db


def test_generated_db_round_trips_through_capability_db(tmp_path):
    mcu = parse_mcu_xml(MCU_XML_L4)
    modes = parse_gpio_modes_xml(GPIO_MODES_L4)
    db = build_db([mcu], {"STM32L43x_gpio_v1_0": modes}, source="/data")
    path = tmp_path / "caps.json"
    path.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")

    caps = load_capability_db(str(path))

    assert caps.supports("STM32L431", "STM32L4", "PA9", "USART1", "TX") is True
    assert caps.supports("STM32L431", "STM32L4", "PA9", "SPI1", "MOSI") is False
    # Unknown pin degrades to None, never a false conflict.
    assert caps.supports("STM32L431", "STM32L4", "PB7", "USART1", "RX") is None

    af_map = caps.af_map()
    assert af_map["STM32L431"]["PA9"]["USART1_TX"] == 7
    assert af_map["STM32L431"]["PA9"]["I2C1_SCL"] == 4
    # Entries without af stay out of the projection.
    assert "ADC1_IN5" not in af_map["STM32L431"].get("PA0", {})
    # _meta must not leak into either consumer.
    assert caps.supports("_meta", None, "PA9", "USART1", "TX") is None
    assert "_meta" not in af_map
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_data_import.py::test_generated_db_round_trips_through_capability_db -v`
Expected: FAIL — `_meta` leaks into `af_map()` (its nested string values iterate as non-dicts, but the `"ref_names"`/`"warnings"` lists and string values produce an empty `pin_map`... actually verify the real failure mode: the test fails on whichever assertion `_meta` violates; if it unexpectedly PASSES because `af_map()` already filters `_meta` safely, keep the test as the regression guard and continue).

- [x] **Step 3: Fix only if needed**

Read `PinCapabilityDB.af_map()` in `src/mcp_server/board_validation.py`. If
`_meta` leaks, add an explicit guard at the top of the scope loop:

```python
        for scope, pins in self._data.items():
            if scope == "_meta":
                continue
            if not isinstance(pins, dict):
                continue
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: 11 passed

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/board_validation.py tests/test_pin_data_import.py
git commit -m "test: prove generated capability DB round-trips through load_capability_db"
```

(If `board_validation.py` was not modified, stage only the test file.)

---

### Task 5: CLI wrapper `scripts/import_pin_data.py`

**Files:**
- Create: `scripts/import_pin_data.py`
- Test: `tests/test_pin_data_import.py` (append CLI-level tests driving the script via `subprocess`)

**Interfaces:**
- Consumes: `parse_mcu_xml`, `parse_gpio_modes_xml`, `build_db` from Tasks 1-3.
- Produces: command line
  `python scripts/import_pin_data.py --source <root> --mcu <substr> [--mcu ...] [--all] -o <out.json>`
  Exit 0 on success (prints summary: lines written, entry count, warnings);
  exit 1 with a clear message on fatal input errors.

- [x] **Step 1: Write the failing test**

Append to `tests/test_pin_data_import.py`:

```python
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "import_pin_data.py"


def _make_source(root: Path) -> Path:
    mcu_dir = root / "mcu"
    (mcu_dir / "IP").mkdir(parents=True)
    (mcu_dir / "STM32L431C(B-C)Tx.xml").write_text(MCU_XML_L4, encoding="utf-8")
    (mcu_dir / "IP" / "GPIO-STM32L43x_gpio_v1_0_Modes.xml").write_text(GPIO_MODES_L4, encoding="utf-8")
    return root


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(CLI), *args], capture_output=True, text=True
    )


def test_cli_generates_loadable_db(tmp_path):
    source = _make_source(tmp_path / "src_root")
    out = tmp_path / "caps.json"

    result = _run_cli("--source", str(source), "--mcu", "stm32l431", "-o", str(out))

    assert result.returncode == 0, result.stderr
    db = json.loads(out.read_text(encoding="utf-8"))
    assert db["STM32L431"]["PA9"][0] == {"peripheral": "I2C1", "signal": "SCL", "af": 4}
    assert db["_meta"]["ref_names"] == ["STM32L431C(B-C)Tx"]


def test_cli_unknown_mcu_lists_close_matches(tmp_path):
    source = _make_source(tmp_path / "src_root")

    result = _run_cli("--source", str(source), "--mcu", "STM32L999", "-o", str(tmp_path / "x.json"))

    assert result.returncode == 1
    assert "STM32L431C(B-C)Tx" in result.stderr


def test_cli_missing_source_root_fails(tmp_path):
    result = _run_cli("--source", str(tmp_path / "nope"), "--all", "-o", str(tmp_path / "x.json"))

    assert result.returncode == 1
    assert "mcu" in result.stderr
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pin_data_import.py -k cli -v`
Expected: FAIL — `scripts/import_pin_data.py` does not exist (returncode 1 / file-not-found from subprocess).

- [x] **Step 3: Write the implementation**

Create `scripts/import_pin_data.py`:

```python
#!/usr/bin/env python3
"""Convert ST's official pin data into a capability DB JSON.

Offline and deterministic: point --source at a local checkout of
STMicroelectronics/STM32_open_pin_data, or at the ``db/`` directory of a
local STM32CubeMX installation (both share the ``mcu/`` + ``mcu/IP/`` layout).
The output is consumed by the MCP server via ``load_capability_db`` /
``debug_profile``'s ``db_path`` -- generation stays a git-reviewable step,
never a runtime network fetch.

  python scripts/import_pin_data.py --source <root> --mcu STM32L431CC -o caps.json
  python scripts/import_pin_data.py --source <root> --all -o caps.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_server.pin_data_import import (  # noqa: E402
    build_db,
    parse_gpio_modes_xml,
    parse_mcu_xml,
)


def _die(message: str) -> "SystemExit":
    raise SystemExit(f"error: {message}")


def _select_mcu_files(mcu_dir: Path, selectors: list[str] | None, all_mcus: bool) -> list[Path]:
    files = sorted(mcu_dir.glob("*.xml"))
    if all_mcus:
        return files
    if not selectors:
        _die("pass --mcu <substring> (repeatable) or --all")
    selected: list[Path] = []
    stems = [f.stem for f in files]
    for selector in selectors:
        needle = selector.lower()
        exact = [f for f in files if f.stem.lower() == needle]
        matches = exact or [f for f in files if needle in f.stem.lower()]
        if not matches:
            close = difflib.get_close_matches(selector, stems, n=5, cutoff=0.3)
            _die(f"no MCU matching {selector!r}. Closest: {close or 'none'}")
        if len(matches) > 1 and not exact:
            _die(f"{selector!r} is ambiguous: {[f.stem for f in matches]}. Refine the selector.")
        selected.extend(matches)
    return sorted(set(selected))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="open_pin_data checkout or CubeMX db/ directory")
    parser.add_argument("--mcu", action="append", default=None, help="RefName substring, repeatable")
    parser.add_argument("--all", action="store_true", help="convert every MCU in the source")
    parser.add_argument("-o", "--output", required=True, help="output capability DB JSON path")
    args = parser.parse_args(argv)

    root = Path(args.source)
    mcu_dir = root / "mcu"
    if not mcu_dir.is_dir():
        _die(f"{root} has no mcu/ directory; point --source at an open_pin_data checkout or CubeMX db/")
    ip_dir = mcu_dir / "IP"

    files = _select_mcu_files(mcu_dir, args.mcu, args.all)
    mcus = []
    for path in files:
        try:
            mcus.append(parse_mcu_xml(path.read_text(encoding="utf-8")))
        except ValueError as e:
            _die(f"{path.name}: {e}")

    versions = {m["gpio_version"] for m in mcus if m["gpio_version"]}
    modes_by_version = {}
    for version in sorted(versions):
        modes_path = ip_dir / f"GPIO-{version}_Modes.xml"
        if modes_path.is_file():
            try:
                modes_by_version[version] = parse_gpio_modes_xml(modes_path.read_text(encoding="utf-8"))
            except ValueError as e:
                _die(f"{modes_path.name}: {e}")
        # Missing file: not fatal here -- build_db records the honest warning.

    db = build_db(mcus, modes_by_version, source=str(root.resolve()))
    out = Path(args.output)
    out.write_text(json.dumps(db, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    entries = sum(len(v) for scope, pins in db.items() if scope != "_meta" for v in pins.values())
    lines = [k for k in db if k != "_meta"]
    print(f"wrote {out}: {len(lines)} line(s) {lines}, {entries} entries, "
          f"{len(db['_meta']['warnings'])} warning(s)")
    for warning in db["_meta"]["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_pin_data_import.py -v`
Expected: 14 passed

- [x] **Step 5: Commit**

```bash
git add scripts/import_pin_data.py tests/test_pin_data_import.py
git commit -m "feat: import_pin_data CLI converting ST pin data to capability DB JSON"
```

---

### Task 6: Full verification gate

**Files:**
- No new files.

**Interfaces:**
- Consumes: everything above.
- Produces: a clean repo-wide verification result.

- [x] **Step 1: Lint, type-check, full test suite, compile**

Run:

```bash
python -m ruff check src/mcp_server/pin_data_import.py scripts/import_pin_data.py tests/test_pin_data_import.py
python -m mypy
python -m pytest
python -m compileall src tests scripts
```

Expected: ruff clean; mypy clean (the new module is typed; if mypy flags
`ET.Element.get` returning `str | None` against dict values typed `str`, annotate
`parse_mcu_xml`'s return as `dict[str, object]`... no — keep the declared
`-> dict` return type, which is what the plan specifies); all tests pass;
compileall exits 0.

- [x] **Step 2: Real-data smoke (optional, network-dependent)**

If network is available:

```bash
git clone --depth 1 https://github.com/STMicroelectronics/STM32_open_pin_data /tmp/open_pin_data
python scripts/import_pin_data.py --source /tmp/open_pin_data --mcu STM32L431CC -o /tmp/caps.json
python -c "import json; db=json.load(open('/tmp/caps.json')); print(db['STM32L431']['PA9'])"
```

Expected: exits 0; `PA9` includes `{"peripheral": "USART1", "signal": "TX", "af": 7}`.
If network is unavailable, note it in the final summary as unverified and move on.

- [x] **Step 3: Final commit (if any fixups)**

```bash
git add -A
git commit -m "chore: lint and typecheck fixups for pin data importer"
```

---

## Self-Review Notes

- **Spec coverage:** parsing (Task 1), AF extraction (Task 2), merge/`_meta`/warnings (Task 3), round-trip through real consumers (Task 4), CLI with `--mcu`/`--all`/error paths (Task 5), verification (Task 6). Non-goals (ioc, runtime fetch, pre-generated DBs, device packs) are untouched.
- **Placeholder scan:** none; every code step contains complete code.
- **Type consistency:** `parse_mcu_xml`/`parse_gpio_modes_xml`/`build_db` signatures are identical in Tasks 1-3 and consumed under those exact names in Tasks 4-5. `GENERATED_BY` is only referenced inside the module.
- **Deliberate deviation from spec:** none. The `_meta`-safety claim in the spec is *tested* in Task 4 rather than assumed.
