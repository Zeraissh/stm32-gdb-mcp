# Maintenance and Quality Plan

## Goal

Turn the STM32 GDB MCP repository into a maintainable private project with
repeatable quality gates, hardware validation guidance, and contributor-facing
project hygiene.

### Task 1: Add Quality Gate

- [x] Add Ruff as a development dependency.
- [x] Configure lint rules in `pyproject.toml`.
- [x] Add lint to GitHub Actions CI.
- [x] Fix lint findings.

### Task 2: Add Hardware Validation Path

- [x] Add a manual HIL GitHub Actions workflow for self-hosted STM32 runners.
- [x] Document HIL runner requirements, smoke coverage, and evidence capture.

### Task 3: Add Maintenance Files

- [x] Add issue templates.
- [x] Add pull request template.
- [x] Add contribution and security guidance.
- [x] Add release checklist.

### Task 4: Verify and Publish

- [x] Run lint, unit tests, compile check, and package build.
- [x] Commit and push the completed project-maturity batch.
