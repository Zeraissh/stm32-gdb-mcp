# Pin Data Importer — Design / 引脚数据导入器设计

Date: 2026-09-03
Status: approved (design), pending implementation plan

## Goal / 目标

Convert ST's official, machine-readable pin data into the capability DB JSON
that `board_validation.PinCapabilityDB` already consumes, so that AF-legality
validation and `af_map()` framework synthesis stop reporting `unresolved` for
lack of data.

把 ST 官方的机器可读引脚数据离线转换成现有 `PinCapabilityDB` 直接消费的
capability DB JSON，消灭 `unresolved` 中最大的缺口（AF 编号）。

Scope: **importer only**. No `.ioc` parsing, no CubeMX headless generation,
no runtime network fetching. Those are separate future increments.

## Data sources / 数据源

Both share the same on-disk layout, so one parser serves both:

- A local checkout of `STMicroelectronics/STM32_open_pin_data` (GitHub).
- The `db/` directory of a local STM32CubeMX installation.

Layout:

```
<root>/mcu/<RefName>.xml                  # per-MCU: Family/Line/Package + pins + signals
<root>/mcu/IP/GPIO-<Version>_Modes.xml    # per-die GPIO IP: numeric AF per pin signal
```

Confirmed by inspection (2026-09-03):

- MCU XML root: `<Mcu Family="STM32F1" Line="STM32F103" Package="LQFP48"
  RefName="STM32F103C(8-B)Tx">`; pins as `<Pin Name="PA0-WKUP" Position="10"
  Type="I/O">` with `<Signal Name="USART2_CTS"/>` children. Signal names are
  already `PERIPHERAL_SIGNAL`.
- The MCU XML names its GPIO IP via
  `<IP Name="GPIO" Version="STM32F103x8_gpio_v1_0">`; the matching modes file is
  `mcu/IP/GPIO-<Version>_Modes.xml`.
- GPIO Modes XML: `<GPIO_Pin PortName="PA" Name="PA9">` →
  `<PinSignal Name="I2C1_SCL">` →
  `<SpecificParameter Name="GPIO_AF"><PossibleValue>GPIO_AF4_I2C1</PossibleValue>`.
  Numeric AF extracted with `^GPIO_AF(\d+)_`.
- F1-family dies use the AFIO remap model (`__HAL_AFIO_REMAP_*`, no numeric AF).
  Such signals honestly produce entries **without** an `af` field, which the DB
  schema explicitly allows.

## Architecture / 架构

Two files, following the existing `hil_rack.py` pattern:

1. **`src/mcp_server/pin_data_import.py`** — pure module, stdlib only
   (`xml.etree.ElementTree`), no imports from the rest of the package,
   everything plain dicts. All parsing/merging/projection logic lives here so
   it is unit-testable via the existing `pythonpath = ["src"]` pytest setup.
2. **`scripts/import_pin_data.py`** — thin CLI wrapper: argument parsing,
   file discovery, writing the output JSON, exit codes. Mirrors `hil_rack.py`'s
   structure (`sys.path` insert, `argparse`, `SystemExit` on fatal input errors).

The MCP server itself is unchanged: it keeps consuming DB JSON through
`load_capability_db(path)`. Generation is an offline, git-reviewable step.

## Data flow / 数据流

```
<source root>
  mcu/<RefName>.xml  ──parse──> pins: {port_pin: [{peripheral, signal}]}, Family, Line, gpio_version
  mcu/IP/GPIO-<gpio_version>_Modes.xml ──parse──> {(port_pin, "PERIPHERAL_SIGNAL"): af_int}
                      │
                      ▼ merge (af looked up per pin+signal; absent → entry without "af")
        per-Line tables: {Line: {port_pin: [{peripheral, signal, af?}]}}
                      │
                      ▼ union across RefNames sharing a Line
        capability DB JSON + "_meta" provenance block
```

### Keying / 键名

DB entries are keyed by **`Line`** (e.g. `STM32F103`, `STM32L431`).
`PinCapabilityDB._pins_for` looks up line first, then family — Line keys hit
the most specific slot. Port-pin → AF mapping is a die-level fact, consistent
across packages of one Line, so multiple RefNames of the same Line are merged
as a union keyed by port pin.

### Signal-name splitting / 信号名切分

Split `PERIPHERAL_SIGNAL` at the **first** underscore: `ADC1_IN0` →
`("ADC1", "IN0")`, `RCC_OSC32_IN` → `("RCC", "OSC32_IN")`. Signals named
`GPIO` or `EVENTOUT` carry no peripheral and are skipped.

### Port-pin normalization / 引脚名归一化

`PA0-WKUP` → `PA0`; `PC14-OSC32_IN` → `PC14` (strip the `-...` suffix).
Non-I/O pins (`Type="Power"|"Reset"|...`) are excluded from the DB.

### AF extraction / AF 提取

From the matched GPIO Modes XML, for each `GPIO_Pin/PinSignal` read the
`SpecificParameter[@Name="GPIO_AF"]/PossibleValue` text. If it matches
`GPIO_AF(\d+)_`, attach `af: <int>`. Otherwise (F1 AFIO remap values, or a
missing GPIO Modes file) the entry is emitted **without** `af` — never
fabricated. A missing GPIO Modes file for a requested MCU produces a warning
count in the CLI output and in `_meta`, not a hard error.

### Provenance / 溯源

Output JSON carries a top-level `"_meta"` key:

```json
{"_meta": {"source": "<absolute source root>", "db_version": "V3.0",
           "generated_by": "stm32-gdb-mcp scripts/import_pin_data.py",
           "ref_names": ["STM32L431CCUx", ...],
           "warnings": ["no GPIO modes file for STM32F103x8_gpio_v1_0"]},
 "STM32L431": {"PA9": [{"peripheral": "USART1", "signal": "TX", "af": 7}]}}
```

Verified harmless to consumers: `supports()` only looks up real line/family
keys; `af_map()` skips scopes whose value is not a dict of pin lists
(`_meta`'s string/list values fail the `isinstance(entry, dict)` filter).

## CLI / 命令行

```bash
python scripts/import_pin_data.py --source <root> --mcu STM32L431CC --mcu STM32F103C8 -o caps.json
python scripts/import_pin_data.py --source <root> --all -o caps.json
```

- `--mcu` matches by substring against RefName file stems, case-insensitive;
  ambiguous matches are an error listing candidates (never a silent pick —
  same philosophy as `detect_probe`).
- Unknown `--mcu` / missing source root / unreadable XML → `SystemExit` with a
  clear message (CLI), mirrored as exceptions in the pure module.

## Error handling / 错误处理

| Situation | Behaviour |
|---|---|
| Source root missing or lacks `mcu/` | hard error, exit non-zero |
| `--mcu` matches nothing | hard error listing closest RefNames |
| `--mcu` matches several RefNames | hard error listing candidates |
| MCU XML malformed | hard error naming the file |
| GPIO Modes file for a die missing | entries without `af` + warning (honest degradation) |
| Signal without numeric AF (F1 AFIO) | normal: entry without `af`, no warning |

## Testing / 测试

`tests/test_pin_data_import.py`, fixtures as small hand-cut XML strings
inline (no network, no real ST data in the repo):

1. MCU XML parse: Family/Line/package extraction, pin/signal extraction,
   port-pin normalization, non-I/O exclusion.
2. Signal splitting incl. multi-underscore names; `GPIO`/`EVENTOUT` skipped.
3. GPIO Modes parse: `GPIO_AF4_I2C1` → 4; AFIO remap values → no `af`.
4. Merge: two RefNames of one Line union correctly; duplicate entries deduped.
5. Missing GPIO Modes file → warning recorded, entries still emitted.
6. Ambiguous / unknown `--mcu` selection → error.
7. Round-trip: generated JSON loads through the real `load_capability_db`,
   `supports()` returns True/False/None as expected, `af_map()` projects the
   `af` entries, and `_meta` does not disturb either consumer.

## Non-goals / 非目标

- `.ioc` parsing or CubeMX headless code generation.
- Runtime fetching from st.com / MCUFinder.
- Shipping pre-generated DBs in the wheel.
- `device_packs` (clock/DMA/NVIC/timer facts) generation — separate schema,
  separate future increment.
