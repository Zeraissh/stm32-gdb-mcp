# Netlist Format Importers (Tier 4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Altium/Protel `.NET`, Cadence/OrCAD `pstxnet.dat`, and generic CSV pin-map importers to `netlist_parser.py`, with format autodetect — closing Tier 4 of `docs/superpowers/plans/2026-07-01-netlist-board-model.md`.

**Architecture:** Extend the existing `src/mcp_server/netlist_parser.py` in place: one pure `parse_<format>_netlist(text) -> (components, nets)` function per format (same shape as `parse_kicad_netlist`), a branch per format in `detect_format` and `parse_netlist`, and a one-line schema-description update in `tools/board_tools.py`. No new modules, no new tools — `import_netlist(path|text, format=...)` picks the formats up automatically. Downstream (`board_model.build_board_description`, `validate_board`) is untouched because every parser emits the same `components`/`nets` contract.

**Tech Stack:** Python 3.10+, stdlib only (`csv`, `io` for the CSV importer), pytest, ruff.

## Global Constraints

- Line length 120, ruff rules E/F/I/UP/B, E501 ignored (from `pyproject.toml`).
- Target Python 3.10 (`target-version = "py310"`); stdlib only, no third-party imports.
- Plain-dict models only (JSON-native house style) — no dataclasses.
- Tests run with `python -m pytest` from the repo root (`pythonpath = ["src"]`); new tests are appended to `tests/test_netlist_parser.py`.
- **Honesty rule (house-wide):** never fabricate a `port_pin`, a component `value`, or a column mapping. Formats that lack a datum emit `None` (AF-legality downstream degrades to `unverified`, never a false conflict); unrecognized structure raises `ValueError` saying what WAS found.
- Keep `python -m ruff check .`, `python -m mypy`, and `python -m pytest` green after every task.
- Work on branch `feat/netlist-formats`.

---

### Task 1: Altium/Protel `.NET` parser

**Files:**
- Modify: `src/mcp_server/netlist_parser.py`
- Test: `tests/test_netlist_parser.py` (append)

**Interfaces:**
- Consumes: nothing new (stdlib only).
- Produces:
  - `parse_altium_netlist(text: str) -> tuple[list[dict], list[dict]]` — same `(components, nets)` shape as `parse_kicad_netlist`: components `{"ref", "value", "footprint", "pins": {pin: net}}`, nets `{"name", "nodes": [{"ref", "pin"}]}`.
  - New `detect_format` return value `"altium"`.
  - `parse_netlist(text, fmt="altium" | "auto")` routes Altium text to this parser; the resulting BoardDescription carries `"format": "altium"`.

Format notes (Protel `.NET`, emitted by Altium Designer and many other ECAD tools): component records are `[` … `]` blocks whose first three lines are designator / footprint / comment (later lines are optional and ignored); net records are `(` … `)` blocks whose first line is the net name and whose remaining lines are `REF-PIN` nodes (split on the LAST `-`; designators never contain one). The format carries **no** pinfunction, so `port_pin` is always absent.

- [x] **Step 1: Write the failing test**

Append to `tests/test_netlist_parser.py`:

```python
ALTIUM_NET = """\
[
U1
LQFP-48
STM32L431CBT6

]
[
R5
R0603
10K

]
(
+3V3
U1-8
R5-1
)
(
USART1_TX
U1-42
R5-2
)
(
GND
U1-47
)
"""


def test_parse_altium_components_and_nets():
    components, nets = parse_altium_netlist(ALTIUM_NET)

    by_ref = {c["ref"]: c for c in components}
    assert by_ref["U1"]["value"] == "STM32L431CBT6"
    assert by_ref["U1"]["footprint"] == "LQFP-48"
    assert by_ref["U1"]["pins"] == {"8": "+3V3", "42": "USART1_TX", "47": "GND"}
    assert {n["name"] for n in nets} == {"+3V3", "USART1_TX", "GND"}
    usart = next(n for n in nets if n["name"] == "USART1_TX")
    assert usart["nodes"] == [{"ref": "U1", "pin": "42"}, {"ref": "R5", "pin": "2"}]


def test_parse_altium_board_description_infers_mcu():
    board = parse_netlist(ALTIUM_NET, fmt="auto")

    assert board["format"] == "altium"
    assert board["mcu"]["part_normalized"] == "STM32L431CBT6"
    assert board["mcu"]["line"] == "STM32L431"
    tx_pin = next(p for p in board["mcu"]["pins"] if p["net"] == "USART1_TX")
    assert tx_pin["package_pin"] == "42"
    assert tx_pin["port_pin"] is None  # Protel .NET carries no pinfunction
    assert tx_pin["function"] == {"peripheral": "USART1", "signal": "TX"}
    assert board["power_nets"] == {"power": ["+3V3"], "ground": ["GND"]}


def test_parse_altium_rejects_bad_node_line():
    with pytest.raises(ValueError, match="bad node"):
        parse_altium_netlist("[\nU1\nLQFP-48\nSTM32L431CBT6\n]\n(\nNETX\nU1\n)\n")


def test_detect_format_altium():
    assert detect_format(ALTIUM_NET) == "altium"
```

Extend the existing import block at the top of the test file so it also imports `parse_altium_netlist`:

```python
from mcp_server.netlist_parser import (
    detect_format,
    load_netlist_file,
    parse_altium_netlist,
    parse_kicad_netlist,
    parse_netlist,
)
```

(Keep whatever the file already imports; add only the missing names.)

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_netlist_parser.py -k altium -v`
Expected: FAIL — `ImportError: cannot import name 'parse_altium_netlist'`.

- [x] **Step 3: Write the implementation**

In `src/mcp_server/netlist_parser.py`, add after the KiCad section (before `# --- Dispatch`):

```python
# --- Altium/Protel .NET ------------------------------------------------------


def parse_altium_netlist(text: str) -> tuple[list, list]:
    """Parse an Altium/Protel ``.NET`` netlist into ``(components, nets)``.

    Component record (``[`` … ``]``; designator / footprint / comment on the
    first three lines, optional extra lines ignored)::

        [
        U1
        LQFP-48
        STM32L431CBT6
        ]

    Net record (``(`` … ``)``; net name on the first line, then REF-PIN
    nodes, split on the LAST dash)::

        (
        USART1_TX
        U1-42
        R5-1
        )

    The format has no pinfunction concept, so nodes never carry ``port_pin``.
    """
    components: list[dict] = []
    nets: list[dict] = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line == "[":
            block: list[str] = []
            i += 1
            while i < n and lines[i].strip() != "]":
                block.append(lines[i].strip())
                i += 1
            padded = block + ["", "", ""]
            ref, footprint, value = padded[0], padded[1], padded[2]
            if ref:
                components.append({
                    "ref": ref,
                    "value": value or None,
                    "footprint": footprint or None,
                    "pins": {},
                })
            i += 1  # consume ']'
        elif line == "(":
            block = []
            i += 1
            while i < n and lines[i].strip() != ")":
                block.append(lines[i].strip())
                i += 1
            if block:
                name = block[0]
                nodes = []
                for entry in block[1:]:
                    if not entry:
                        continue
                    if "-" not in entry:
                        raise ValueError(
                            f"Altium netlist: bad node {entry!r} in net {name!r} (expected REF-PIN)")
                    ref, pin = entry.rsplit("-", 1)
                    nodes.append({"ref": ref, "pin": pin})
                nets.append({"name": name, "nodes": nodes})
            i += 1  # consume ')'
        else:
            if line:
                raise ValueError(f"Altium netlist: unexpected content outside a record: {line!r}")
            i += 1

    comp_index = {c["ref"]: c for c in components}
    for net in nets:
        for node in net["nodes"]:
            comp = comp_index.get(node["ref"])
            if comp is not None:
                comp["pins"][node["pin"]] = net["name"]
    return components, nets
```

Then wire detection and dispatch. Replace the existing `detect_format` and `parse_netlist` with:

```python
def detect_format(text: str) -> str:
    """Best-effort netlist format detection."""
    head = text.lstrip()[:256].lower()
    if head.startswith("(export") or "(netlist" in head or "(components" in head:
        return "kicad"
    if head.startswith("file_type") and "expandednetlist" in head:
        return "orcad"
    if "net_name" in head and "node_name" in head:
        return "orcad"
    if head.startswith("["):
        return "altium"
    first_line = head.splitlines()[0] if head else ""
    if "," in first_line and "net" in first_line and "pin" in first_line:
        return "csv"
    return "unknown"


def parse_netlist(text: str, fmt: str = "auto", source: str = "<memory>") -> dict:
    """Parse netlist text into a normalized BoardDescription."""
    resolved = detect_format(text) if fmt in (None, "auto") else fmt.lower()
    if resolved == "kicad":
        components, nets = parse_kicad_netlist(text)
    elif resolved == "altium":
        components, nets = parse_altium_netlist(text)
    else:
        raise ValueError(
            f"Unsupported or undetected netlist format: {resolved!r}. "
            "Supported: kicad, altium, orcad, csv.")
    return build_board_description(components, nets, source=source, fmt=resolved)
```

(The `orcad`/`csv` branches land in Tasks 2-3; the error message already names the full target set.)

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_netlist_parser.py -v`
Expected: all tests pass, including the 4 new `altium` ones and every pre-existing kicad test.

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/netlist_parser.py tests/test_netlist_parser.py
git commit -m "feat: Altium/Protel .NET netlist importer with autodetect"
```

---

### Task 2: OrCAD/Cadence `pstxnet.dat` parser

**Files:**
- Modify: `src/mcp_server/netlist_parser.py`
- Test: `tests/test_netlist_parser.py` (append)

**Interfaces:**
- Consumes: the `detect_format` / `parse_netlist` dispatch extended in Task 1.
- Produces:
  - `parse_orcad_netlist(text: str) -> tuple[list[dict], list[dict]]` — same `(components, nets)` contract as Task 1. `pstxnet.dat` carries **no part values** (those live in the companion `pstxprt.dat`), so every component is synthesized from node references with `value=None` and MCU detection emits the standard no-MCU warning rather than guessing.
  - `parse_netlist(text, fmt="orcad" | "auto")` routing; BoardDescription carries `"format": "orcad"`.

Supported subset — the structural skeleton of `FILE_TYPE=EXPANDEDNETLIST` (canonical-path and property lines are skipped):

```text
FILE_TYPE=EXPANDEDNETLIST;
{ Using PSTWRITER 17.4-2019 }
NET_NAME
'USART1_TX'
'@BOARD.SCHEMATIC1(SCH_1):USART1_TX':
C_SIGNAL='@board.schematic1(sch_1):usart1_tx';
NODE_NAME U1 42
'@BOARD.SCHEMATIC1(SCH_1):PAGE1_42@STM.STM32L431CBT6.NORMAL(CHIPS)':
'IO':;
END.
```

- [x] **Step 1: Write the failing test**

Append to `tests/test_netlist_parser.py` (and add `parse_orcad_netlist` to the import block):

```python
ORCAD_PSTXNET = """\
FILE_TYPE=EXPANDEDNETLIST;
{ Using PSTWRITER 17.4-2019 }
NET_NAME
'USART1_TX'
'@BOARD.SCHEMATIC1(SCH_1):USART1_TX':
C_SIGNAL='@board.schematic1(sch_1):usart1_tx';
NODE_NAME U1 42
'@BOARD.SCHEMATIC1(SCH_1):PAGE1_42@STM.STM32L431CBT6.NORMAL(CHIPS)':
'IO':;
NODE_NAME R5 1
'@BOARD.SCHEMATIC1(SCH_1):PAGE1_1@DISCRETE.R.NORMAL(CHIPS)':
'I':;
NET_NAME
'GND'
'@BOARD.SCHEMATIC1(SCH_1):GND':
C_SIGNAL='@board.schematic1(sch_1):gnd';
NODE_NAME U1 47
'@BOARD.SCHEMATIC1(SCH_1):PAGE1_47@STM.STM32L431CBT6.NORMAL(CHIPS)':
'I':;
END.
"""


def test_parse_orcad_nets_and_nodes():
    components, nets = parse_orcad_netlist(ORCAD_PSTXNET)

    assert [n["name"] for n in nets] == ["USART1_TX", "GND"]
    usart = nets[0]
    assert usart["nodes"] == [{"ref": "U1", "pin": "42"}, {"ref": "R5", "pin": "1"}]
    by_ref = {c["ref"]: c for c in components}
    assert by_ref["U1"]["pins"] == {"42": "USART1_TX", "47": "GND"}
    assert by_ref["U1"]["value"] is None  # pstxnet.dat carries no part values


def test_parse_orcad_board_description_degrades_honestly():
    board = parse_netlist(ORCAD_PSTXNET, fmt="auto")

    assert board["format"] == "orcad"
    assert board["mcu"] is None  # no part values -> no MCU detection, never a guess
    assert any("No STM32 MCU" in w for w in board["warnings"])
    assert board["power_nets"]["ground"] == ["GND"]


def test_parse_orcad_rejects_node_before_net():
    with pytest.raises(ValueError, match="NODE_NAME before any NET_NAME"):
        parse_orcad_netlist("FILE_TYPE=EXPANDEDNETLIST;\nNODE_NAME U1 1\n':\nEND.\n")


def test_detect_format_orcad():
    assert detect_format(ORCAD_PSTXNET) == "orcad"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_netlist_parser.py -k orcad -v`
Expected: FAIL — `ImportError: cannot import name 'parse_orcad_netlist'`.

- [x] **Step 3: Write the implementation**

In `src/mcp_server/netlist_parser.py`, add after the Altium section:

```python
# --- OrCAD/Cadence pstxnet.dat ------------------------------------------------


def parse_orcad_netlist(text: str) -> tuple[list, list]:
    """Parse a Cadence/OrCAD ``pstxnet.dat`` netlist into ``(components, nets)``.

    Supported subset (the structural skeleton of ``FILE_TYPE=EXPANDEDNETLIST``):
    a ``NET_NAME`` line followed by the quoted net name starts a net record;
    ``NODE_NAME <ref> <pin>`` lines attach nodes to the current net; canonical
    -path and property lines are skipped; ``END.`` terminates the file.

    ``pstxnet.dat`` carries no part values (those live in ``pstxprt.dat``), so
    every component is synthesized from node references with ``value=None``;
    MCU detection then emits the usual no-MCU warning rather than guessing.
    """
    nets: list[dict] = []
    refs: dict[str, dict] = {}
    current: dict | None = None
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i].strip()
        if line == "NET_NAME":
            i += 1
            while i < n and not lines[i].strip():
                i += 1
            name_line = lines[i].strip() if i < n else ""
            if not (name_line.startswith("'") and name_line.endswith("'")):
                raise ValueError("OrCAD netlist: NET_NAME not followed by a quoted name")
            current = {"name": name_line.strip("'"), "nodes": []}
            nets.append(current)
        elif line.startswith("NODE_NAME"):
            if current is None:
                raise ValueError("OrCAD netlist: NODE_NAME before any NET_NAME")
            parts = line.split()
            if len(parts) != 3:
                raise ValueError(f"OrCAD netlist: bad NODE_NAME line {line!r}")
            _, ref, pin = parts
            current["nodes"].append({"ref": ref, "pin": pin})
            refs.setdefault(ref, {"ref": ref, "value": None, "footprint": None, "pins": {}})
        elif line == "END.":
            break
        i += 1
    if not nets:
        raise ValueError("OrCAD netlist: no NET_NAME records found")

    components = list(refs.values())
    comp_index = {c["ref"]: c for c in components}
    for net in nets:
        for node in net["nodes"]:
            comp_index[node["ref"]]["pins"][node["pin"]] = net["name"]
    return components, nets
```

Then add the dispatch branch in `parse_netlist`, between the altium branch and the `else`:

```python
    elif resolved == "orcad":
        components, nets = parse_orcad_netlist(text)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_netlist_parser.py -v`
Expected: all tests pass, including the 4 new `orcad` ones.

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/netlist_parser.py tests/test_netlist_parser.py
git commit -m "feat: OrCAD/Cadence pstxnet.dat netlist importer with autodetect"
```

---

### Task 3: Generic CSV pin-map importer

**Files:**
- Modify: `src/mcp_server/netlist_parser.py`
- Test: `tests/test_netlist_parser.py` (append)

**Interfaces:**
- Consumes: stdlib `csv` + `io`; the dispatch from Tasks 1-2.
- Produces:
  - `parse_csv_netlist(text: str) -> tuple[list[dict], list[dict]]` — same contract as Tasks 1-2, plus optional `port_pin` on nodes and `value` on components when those columns exist.
  - `parse_netlist(text, fmt="csv" | "auto")` routing; BoardDescription carries `"format": "csv"`.

CSV contract: one row per component pin; header row REQUIRED. Column names matched case-insensitively after `strip()`:

| Role | Accepted headers |
|---|---|
| ref (required) | `ref`, `designator`, `reference`, `refdes` |
| pin (required) | `pin`, `pad`, `pin_number`, `pinnumber` |
| net (required) | `net`, `net_name`, `netname`, `signal`, `name` |
| value (optional) | `value`, `comment`, `part` |
| port_pin (optional) | `port_pin`, `portpin`, `pin_name`, `pinname` |

Missing required columns raise `ValueError` listing the header that WAS found — never a guessed mapping. Blank/incomplete rows (export artifacts) are skipped.

- [x] **Step 1: Write the failing test**

Append to `tests/test_netlist_parser.py` (and add `parse_csv_netlist` to the import block):

```python
CSV_PINMAP = """\
ref,pin,net,value
U1,42,USART1_TX,STM32L431CBT6
U1,47,GND,STM32L431CBT6
R5,1,USART1_TX,10K
"""


def test_parse_csv_builds_components_and_nets():
    components, nets = parse_csv_netlist(CSV_PINMAP)

    by_ref = {c["ref"]: c for c in components}
    assert by_ref["U1"]["value"] == "STM32L431CBT6"
    assert by_ref["U1"]["pins"] == {"42": "USART1_TX", "47": "GND"}
    assert {n["name"] for n in nets} == {"USART1_TX", "GND"}


def test_parse_csv_board_description_infers_mcu():
    board = parse_netlist(CSV_PINMAP, fmt="auto")

    assert board["format"] == "csv"
    assert board["mcu"]["line"] == "STM32L431"
    tx_pin = next(p for p in board["mcu"]["pins"] if p["net"] == "USART1_TX")
    assert tx_pin["function"] == {"peripheral": "USART1", "signal": "TX"}


def test_parse_csv_accepts_column_aliases_and_port_pin():
    text = "Designator,Pad,Signal,Value,Pin_Name\nU1,42,USART1_TX,STM32L431CBT6,PA9\n"
    components, nets = parse_csv_netlist(text)

    assert components[0]["value"] == "STM32L431CBT6"
    assert nets[0]["nodes"][0]["port_pin"] == "PA9"


def test_parse_csv_missing_column_lists_header():
    with pytest.raises(ValueError, match="missing required column"):
        parse_csv_netlist("foo,bar\n1,2\n")


def test_detect_format_csv():
    assert detect_format(CSV_PINMAP) == "csv"
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_netlist_parser.py -k csv -v`
Expected: FAIL — `ImportError: cannot import name 'parse_csv_netlist'`.

- [x] **Step 3: Write the implementation**

In `src/mcp_server/netlist_parser.py`, add the imports at the top of the file:

```python
import csv
import io
```

Then add after the OrCAD section:

```python
# --- Generic CSV pin-map ------------------------------------------------------


def parse_csv_netlist(text: str) -> tuple[list, list]:
    """Parse a generic CSV pin-map into ``(components, nets)``.

    One row per component pin; a header row is required. Column names are
    matched case-insensitively: ref (``ref``/``designator``/``reference``/``refdes``),
    pin (``pin``/``pad``/``pin_number``/``pinnumber``), net (``net``/``net_name``/
    ``netname``/``signal``/``name``); optional value (``value``/``comment``/``part``)
    and port_pin (``port_pin``/``portpin``/``pin_name``/``pinname``). Missing
    required columns raise ``ValueError`` listing the header that WAS found —
    never a guessed mapping.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ValueError("CSV pin-map: empty input (no header row)")
    canon = {(name or "").strip().lower(): name for name in reader.fieldnames}

    def pick(*candidates: str) -> str | None:
        for candidate in candidates:
            if candidate in canon:
                return canon[candidate]
        return None

    ref_col = pick("ref", "designator", "reference", "refdes")
    pin_col = pick("pin", "pad", "pin_number", "pinnumber")
    net_col = pick("net", "net_name", "netname", "signal", "name")
    value_col = pick("value", "comment", "part")
    port_pin_col = pick("port_pin", "portpin", "pin_name", "pinname")
    missing = [label for label, col in
               (("ref", ref_col), ("pin", pin_col), ("net", net_col)) if col is None]
    if missing:
        raise ValueError(
            f"CSV pin-map: missing required column(s) {missing}; header was {reader.fieldnames}. "
            "Accepted: ref/designator, pin/pad, net/net_name/signal.")

    components: dict[str, dict] = {}
    nets: dict[str, dict] = {}
    for row in reader:
        ref = (row.get(ref_col) or "").strip()
        pin = (row.get(pin_col) or "").strip()
        net = (row.get(net_col) or "").strip()
        if not (ref and pin and net):
            continue  # tolerate blank/export-artifact rows
        comp = components.setdefault(ref, {"ref": ref, "value": None, "footprint": None, "pins": {}})
        if value_col and (row.get(value_col) or "").strip():
            comp["value"] = row[value_col].strip()
        comp["pins"][pin] = net
        bucket = nets.setdefault(net, {"name": net, "nodes": []})
        node: dict = {"ref": ref, "pin": pin}
        if port_pin_col and (row.get(port_pin_col) or "").strip():
            node["port_pin"] = row[port_pin_col].strip()
        bucket["nodes"].append(node)
    if not nets:
        raise ValueError("CSV pin-map: header parsed but no data rows produced any nets")
    return list(components.values()), list(nets.values())
```

Then add the dispatch branch in `parse_netlist`, between the orcad branch and the `else`:

```python
    elif resolved == "csv":
        components, nets = parse_csv_netlist(text)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_netlist_parser.py -v`
Expected: all tests pass, including the 5 new `csv` ones.

- [x] **Step 5: Commit**

```bash
git add src/mcp_server/netlist_parser.py tests/test_netlist_parser.py
git commit -m "feat: generic CSV pin-map netlist importer with autodetect"
```

---

### Task 4: Tool surface polish + full verification gate

**Files:**
- Modify: `src/mcp_server/netlist_parser.py` (module docstring)
- Modify: `src/mcp_server/tools/board_tools.py:29` (schema description) and `:48` (error suggestion)
- Modify: `CHANGELOG.md` (new entry at the top, above `## [0.14.0]`)
- Test: `tests/test_netlist_parser.py` (append one test)

**Interfaces:**
- Consumes: everything above.
- Produces: `import_netlist(format=...)` documentation matches reality; the unsupported-format error names all four formats; a clean repo-wide verification result.

- [x] **Step 1: Write the failing test**

Append to `tests/test_netlist_parser.py`:

```python
def test_parse_netlist_unknown_format_lists_all_supported():
    with pytest.raises(ValueError, match=r"Supported: kicad, altium, orcad, csv"):
        parse_netlist("this is not a netlist at all", fmt="auto")
```

- [x] **Step 2: Run test to verify it fails / passes**

Run: `python -m pytest tests/test_netlist_parser.py -k unknown_format -v`
Expected: PASS already if Task 1 used the exact error message shown there
(`"... Supported: kicad, altium, orcad, csv."`). If it fails, fix the message in
`parse_netlist` to match — do NOT loosen the test.

- [x] **Step 3: Update the module docstring and tool descriptions**

Replace the first paragraph of `src/mcp_server/netlist_parser.py`'s docstring with:

```python
"""Netlist parsers that produce a normalized BoardDescription.

Supported formats: KiCad ``.net`` S-expressions, Altium/Protel ``.NET``,
Cadence/OrCAD ``pstxnet.dat``, and a generic CSV pin-map. Each parser is
stdlib-only and hands raw ``components`` / ``nets`` to
``board_model.build_board_description`` for normalization and pin-function
inference. Formats that lack a datum (``port_pin`` outside KiCad/CSV, part
values in pstxnet) emit ``None`` — never a guess.
"""
```

In `src/mcp_server/tools/board_tools.py`, change the `format` property description (line 29) to:

```python
            "format": {"type": "string", "description":
                       "Netlist format: auto (default), kicad, altium (Protel .NET), "
                       "orcad (pstxnet.dat) or csv (generic pin-map)."},
```

and the parse-error suggestion (line 48) to:

```python
            suggested_next_actions=["import_netlist with format=kicad|altium|orcad|csv"])]
```

- [x] **Step 4: Add the CHANGELOG entry**

Insert at the top of `CHANGELOG.md`, directly under `# Changelog / 更新日志`:

```markdown
## [Unreleased]

### Import netlists from Altium, OrCAD, and CSV pin-maps / 网表导入支持 Altium、OrCAD 与 CSV

- `import_netlist` gains three formats alongside KiCad: **Altium/Protel `.NET`**,
  **Cadence/OrCAD `pstxnet.dat`**, and a **generic CSV pin-map**, all autodetected
  (explicit `format=altium|orcad|csv` also accepted). The BoardDescription contract
  is unchanged, so `describe_board` / `validate_board` / framework planning work as-is. /
  三种格式全部支持自动探测，BoardDescription 契约不变，下游工具无需改动。
- Honest degradation, as everywhere else: Protel `.NET` has no pinfunction and
  `pstxnet.dat` has no part values, so `port_pin` / MCU detection degrade to
  `None` + a warning — never a guess. / 缺失数据降级为 `None` 加警告，绝不猜测。
```

- [x] **Step 5: Full verification**

Run:

```bash
python -m ruff check src/mcp_server/netlist_parser.py src/mcp_server/tools/board_tools.py tests/test_netlist_parser.py
python -m mypy
python -m pytest
python -m compileall src tests scripts
```

Expected: ruff clean; mypy clean; the full suite green (1293 pre-existing + ~14 new
netlist tests); compileall exits 0.

- [x] **Step 6: Real-file smoke (optional, needs a real export)**

If a real Altium `.NET` or OrCAD `pstxnet.dat` export is available:

```bash
python -c "from mcp_server.netlist_parser import load_netlist_file; \
b = load_netlist_file('<file>'); \
print(b['format'], b['stats'], b['warnings'])"
```

Expected: the right `format`, plausible `stats`, and only honest warnings.
If no real file is available, note it in the final summary as unverified and move on.

- [x] **Step 7: Commit**

```bash
git add src/mcp_server/netlist_parser.py src/mcp_server/tools/board_tools.py tests/test_netlist_parser.py CHANGELOG.md
git commit -m "docs: netlist format coverage in tool schema, module docstring and changelog"
```

- [x] **Step 8: Close the parent plan**

In `docs/superpowers/plans/2026-07-01-netlist-board-model.md`, change the Status section's last line from `- [ ] **Tier 4** — Altium / OrCAD / CSV importers.` to `- [x] **Tier 4** — Altium / OrCAD / CSV importers. *Done.*`, then:

```bash
git add docs/superpowers/plans/2026-07-01-netlist-board-model.md
git commit -m "docs: close Tier 4 of the netlist board-model plan"
```

---

## Self-Review Notes

- **Spec coverage:** Tier 4 of the parent plan asks for "Altium `.NET` (Protel), OrCAD, and a generic CSV pin-map importer, with format autodetect. Tests per format." — Task 1 (Altium), Task 2 (OrCAD), Task 3 (CSV), autodetect wired per task, 4-5 tests per format, Task 4 closes the parent plan. No other Tier-4 requirement exists.
- **Placeholder scan:** none; every code step contains complete code, every test step contains complete fixtures with exact expected values verified against `board_model.py` (`normalize_mcu_part("STM32L431CBT6")` → line `STM32L431`; `infer_pin_function("USART1_TX")` → `{USART1, TX}`; `classify_power_net("+3V3")` → `power`).
- **Type consistency:** all three parsers return the same `tuple[list[dict], list[dict]]` `(components, nets)` shape consumed by `build_board_description(components, nets, source, fmt)` — identical to the existing `parse_kicad_netlist`. `detect_format` return values `"kicad" | "altium" | "orcad" | "csv" | "unknown"` match the `parse_netlist` branches exactly. The OrCAD fixture syntax is taken from a documented `pstxnet.dat` sample (PSTWRITER output), not invented.
- **Deliberate scope cuts (YAGNI):** `pstxprt.dat` part-value join (would fix OrCAD MCU detection) is out of scope — the honest warning path covers it; Altium's XML netlist variant is out of scope; semicolon-delimited CSV is out of scope (comma only, honest error otherwise).
