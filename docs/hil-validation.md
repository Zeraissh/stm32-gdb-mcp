# Hardware-in-the-loop Validation

Hardware-in-the-loop checks are manual by design. The normal CI workflow must
stay hardware-free, while the HIL workflow runs on a trusted self-hosted runner
that has a probe, board, firmware image, and vendor tools installed.

## Runner Requirements

- A self-hosted GitHub Actions runner with labels `self-hosted` and `stm32`
- Python 3.10 or newer
- `arm-none-eabi-gdb` on `PATH`
- One supported GDB server on `PATH`: OpenOCD, J-Link, or ST-Link
- A connected STM32 target board
- A debug config YAML file for the target
- Optional RTT or UART host tools when validating log capture

## Manual Workflow

Run the GitHub Actions workflow named `Hardware-in-the-loop`.

Inputs:

- `config_path`: YAML config to validate before touching hardware
- `smoke_command`: optional command executed after setup and config validation

Example smoke command:

```bash
python -m pytest -q tests -m hil
```

The repository does not ship board-specific HIL tests yet because those require
firmware, target wiring, and probe-specific reset behavior. Keep board tests in
your private environment until they can be sanitized and generalized.

## Suggested Smoke Coverage

- validate the debug config
- start a debug session
- flash a known firmware image
- reset and halt
- read core registers
- set and hit a breakpoint near `main`
- capture a debug snapshot
- decode one SVD peripheral register
- read current FreeRTOS task when the firmware uses FreeRTOS
- collect RTT or UART logs when enabled

## Evidence to Keep

For every HIL run, keep:

- workflow run URL
- config path and sanitized config contents
- probe and board identity
- firmware commit or build ID
- MCP responses for the smoke sequence
- target logs around failures

This evidence is what lets an AI client or maintainer distinguish host setup
problems from firmware, probe, and MCP bugs.
