# Contributing

Thanks for helping improve `stm32-gdb-mcp`. This project sits close to real
hardware, so good evidence matters more than guesswork.

## Development Setup

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python setup_env.py
```

`setup_env.py` checks for host tools such as `arm-none-eabi-gdb`, OpenOCD,
SEGGER J-Link tools, and ST-Link tools. A missing tool can be acceptable for a
pure unit-test change, but it should be called out in the pull request.

## Quality Gate

Run the same checks used by CI before opening a pull request:

```bash
python -m ruff check .
python -m pytest -q
python -m compileall src tests
python -m build
```

## Change Guidelines

- Prefer small, evidence-backed changes.
- Keep MCP tool responses stable unless the change is explicitly about response
  schema migration.
- Add unit tests for parser, config, response-shape, and GDB interaction logic.
- Use hardware-in-the-loop validation for changes that affect flashing, reset,
  target execution, probe behavior, RTT, UART, or live RTOS inspection.
- Do not include proprietary firmware, private source paths, credentials, serial
  numbers, or vendor license data in issues, tests, logs, or snapshots.

## Reporting Bugs

Use the bug report template and include sanitized evidence:

- MCP tool call sequence
- debug config shape
- GDB server type and probe
- MCU and board
- relevant GDB, RTT, UART, or fault-register output

For HardFault and FreeRTOS issues, attach the output of
`capture_debug_snapshot` with private symbols and source paths removed.
