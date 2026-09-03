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


def _die(message: str) -> SystemExit:
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
    print(f"wrote {out}: {len(lines)} line(s) {lines}, {entries} entries, {len(db['_meta']['warnings'])} warning(s)")
    for warning in db["_meta"]["warnings"]:
        print(f"warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
