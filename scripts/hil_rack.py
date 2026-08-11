#!/usr/bin/env python3
"""Drive a programmable USB hub rack from CI.

Two commands, both used by .github/workflows/hil.yml:

  isolate --config rack.yaml --board l431
      Leave only that board's probe enumerated, so detect_probe sees exactly one
      probe and start_debug_session's single-probe auto-select stays on its safe
      path. This is what replaces the human who used to pick a board from a
      dropdown.

  restore --config rack.yaml
      Reconnect every data line and restore power. Runs with `if: always()` --
      a failed leg must not leave the rack dark for whoever runs next.

Requires the optional extra:  pip install 'stm32-gdb-mcp[hub]'
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp_server.debug_config import load_debug_config  # noqa: E402
from mcp_server.hub import (  # noqa: E402
    HubBusyError,
    HubGuardError,
    HubManager,
    HubUnavailableError,
    _normalize_map,
)


def _rack(config_path: str) -> tuple[HubManager, dict]:
    loaded = load_debug_config(config_path)
    validation = loaded["validation"]
    if not validation["valid"]:
        raise SystemExit(f"invalid rack config {config_path}: {validation['errors']}")
    spec = loaded["config"].get("hub")
    if not spec:
        raise SystemExit(f"{config_path} has no `hub:` block; nothing to drive")
    manager = HubManager()
    manager.configure(spec)
    return manager, spec


def _channel_for_board(spec: dict, board: str) -> int:
    entries = _normalize_map(spec.get("map"))
    for channel, entry in sorted(entries.items()):
        if entry.get("label") == board:
            return channel
    raise SystemExit(
        f"no hub.map entry labelled '{board}'. Known labels: "
        f"{sorted(e.get('label') for e in entries.values() if e.get('label'))}"
    )


def isolate(args) -> int:
    manager, spec = _rack(args.config)
    channel = _channel_for_board(spec, args.board)
    result = manager.data(channel, "on", confirm=True, exclusive=True)
    print(f"board '{args.board}' isolated on hub channel {channel}; "
          f"data lines off: {result.get('data_off_channels', [])}")
    for port in manager.describe()["ports"]:
        print(f"  ch{port['channel']}: power={port['power']} data={port['data']}")
    return 0


def restore(args) -> int:
    manager, _spec = _rack(args.config)
    for channel in manager.channels():
        manager.data(channel, "on", confirm=True)
        manager.power(channel, "on", confirm=True)
    print("rack restored: every channel powered with data lines connected")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    isolate_parser = sub.add_parser("isolate", help="expose only one board's probe")
    isolate_parser.add_argument("--config", required=True, help="rack config YAML")
    isolate_parser.add_argument("--board", required=True, help="hub.map label to isolate")
    isolate_parser.set_defaults(func=isolate)

    restore_parser = sub.add_parser("restore", help="reconnect and re-power every channel")
    restore_parser.add_argument("--config", required=True, help="rack config YAML")
    restore_parser.set_defaults(func=restore)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (HubBusyError, HubGuardError, HubUnavailableError) as exc:
        # A CI log should show what to fix, not a traceback into the vendor library.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
