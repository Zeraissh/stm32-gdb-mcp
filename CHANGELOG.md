# Changelog / 更新日志

## [Unreleased] — part 3 (hardware-validated on STM32L151)

Validating parts 1 and 2 on a real L151 found the root cause under issue #22 —
and a regression part 1 had introduced. 在真实 L151 上验证第 1、2 部分时找到了
issue #22 的根因,以及第 1 部分引入的一个回归。

### MI async mode / MI 异步模式 (the root cause)

- GDB now runs with `mi-async on`, set immediately after launch and before any
  target is attached. In the default all-stop synchronous mode GDB accepts **no**
  command while the target runs — not even `-exec-interrupt`, which simply never
  answers (measured: 5 s, zero records) and wedges every command queued behind it.
  So `halt_execution` could never actually stop a running target. /
  GDB 现在以 `mi-async on` 运行,在启动后、attach 目标前设置。默认的 all-stop 同步模式下,
  目标运行期间 GDB 不接受任何命令,连 `-exec-interrupt` 也永不应答,后续命令全部堵死,
  因此 `halt_execution` 从来无法真正停下一个运行中的目标。
- This is why the old spurious SIGINT existed at all: the only interrupt that ever
  reached the target was the one leaked at connect time and queued by the stub,
  which fired on the next resume. Removing the leak without enabling async would
  have left no way to halt at all. / 这也解释了旧版幽灵 SIGINT 的由来:唯一真正送达
  目标的中断,正是连接时泄漏、被 stub 排队、在下次恢复执行时触发的那个。只堵住泄漏而不
  开启 async,就会彻底失去停止运行中目标的能力。
- GDB rejects the setting once an inferior exists, so it must be the first command
  after launch; an old GDB without `mi-async` degrades to halted-target flows.

### Run-state tracking / 运行状态跟踪 (regression fix)

- Part 1 established "is the target halted?" by probing it with an MI command.
  On a **running** target that command is not answered until it halts, and the
  late reply offsets every later response — after any `run_and_wait` timeout the
  whole session was unusable. State now comes only from GDB's own
  `*running`/`*stopped` records; no command is ever sent to find it out. /
  第 1 部分用发送 MI 命令的方式判断目标是否停止;在运行中的目标上该命令要等到目标停下
  才被应答,迟到的回复会让此后每条响应错位,导致任何 `run_and_wait` 超时后整个会话不可用。
  现在状态只来自 GDB 自己的 `*running`/`*stopped` 记录,绝不为此发送命令。
- Attaching emits no `*stopped`, so `connect` seeds the halted state — GDB servers
  halt the target on attach, which is why identity reads work immediately. /
  attach 不产生 `*stopped`,因此 `connect` 播种"已停止"状态。
- A late `*stopped` (Windows pipe polling delivers it after the wait window) is
  now caught by a patient final drain rather than by probing. /
  迟到的 `*stopped` 改由末尾的耐心 drain 捕获。

## [Unreleased] — part 2

Leaner family schemas, argument names that are actually real, and a read-only dispatcher
that stops costing approval prompts.

更精简的族 schema、真实存在的参数名,以及一个不再触发审批提示的只读分发工具。

### Merged-family schemas / 合并族 schema

- Each `oneOf` branch used to repeat the family's entire property map — descriptions and
  `session` included — making these the largest schemas advertised. Arguments are now hoisted
  into one top-level `properties` map and a branch carries only its discriminator and its own
  `required` list. `logging` 2.9k -> 1.8k chars, `breakpoint` 2.5k -> 1.5k. /
  每个 `oneOf` 分支此前重复整个属性表(含描述与 `session`),现将参数提升到顶层,分支只保留
  判别值与自身的 `required`。
- A property that two actions genuinely mean differently stays in its branch —
  `breakpoint.location` is "where to break" for `set` and "what to watch" for `watch`. Only
  duplicates that differ by an omitted description are collapsed. /
  两个动作含义确实不同的属性仍留在各自分支;仅合并"只差一个描述"的重复项。

### Argument names / 参数名 (AI-facing bug)

- Twelve actions across eight families documented arguments that do not exist, or hid required
  ones: `delete(number)` when the schema wants `breakpoint_id`, `watch(expression)` when it wants
  `location`+`access_type`, `select(number)` when it wants `level`, `assert(expressions)` when it
  wants `assertions`, and so on. Every call written from those descriptions was rejected by schema
  validation and had to be retried. All of them now name the real arguments. /
  八个族中的十二个动作此前记录了不存在的参数或隐藏了必填参数,导致模型照描述发起的调用必被
  schema 拒绝并重试;现已全部改为真实参数名。
- A test pins the contract: inside a `choice(...)` group every identifier must be a real property
  of that action's schema, and every required property must be listed outside the `[optional]`
  bracket. / 新增测试锁定该约定,防止再次漂移。

### call_read / 只读分发

- New `call_read(tool, args)`: same escape hatch as `call`, restricted to tools that only read
  state, and annotated `readOnlyHint` accordingly. `call` is necessarily annotated
  destructive + open-world (it can reach anything), so reaching a hidden **read-only** tool
  through it triggered an approval prompt that tool would never have triggered directly —
  compact mode hides ~78 tools, and 50 of them are read-only. /
  新增 `call_read(tool, args)`:与 `call` 相同的逃生口,但仅限只读工具,因而可标注为只读。
  `call` 必须保守地标注为破坏性+开放世界,于是经它访问隐藏的**只读**工具会触发本不该有的
  审批提示;紧凑模式隐藏约 78 个工具,其中 50 个是只读的。
- It refuses anything that writes (and the dispatchers, including itself) with a `not_read_only`
  error pointing at `call`. / 对任何写操作(以及各分发工具本身)返回 `not_read_only` 错误并指向 `call`。

## [Unreleased]

Token diet + honest errors: the advertised surface and every result envelope shrink
substantially, and tools stop reporting `ok:true` for GDB operations that actually failed.

Token 瘦身 + 诚实错误:工具面与每条结果包络大幅缩小,且工具不再对实际失败的 GDB
操作报告 `ok:true`。

### Token economy / Token 经济性

- Dropped the per-tool `outputSchema`. Every tool advertised the same ~460-char response
  envelope; it is now documented once in the server instructions and `tool_response.OUTPUT_SCHEMA`.
  Compact mode: 41.9k -> 26.3k chars (~10.5k -> ~6.6k tokens); full surface: 110k -> 72k chars. /
  移除每工具的 `outputSchema`:同一份约 460 字符的响应包络此前在每个工具上重复广告,
  现只在服务器说明中记录一次。紧凑模式 ~10.5k -> ~6.6k tokens;完整工具面 110k -> 72k 字符。
- Result JSON is emitted compactly (no indent) and omits empty envelope fields
  (`data`/`error`/`raw_response`/`suggested_next_actions`). A typical result went from
  180 to 52 chars. Consumers already used `.get()`, so the shape is compatible. /
  结果 JSON 改为紧凑输出且省略空字段,典型结果从 180 字符降至 52 字符。
- Raw GDB/MI records no longer ride along on **successful** results unless the server runs
  with `STM32_GDB_MCP_VERBOSE=1`. Failures always keep them — that is the evidence needed
  to diagnose. / 成功结果默认不再附带原始 GDB/MI 记录(除非设置 `STM32_GDB_MCP_VERBOSE=1`);
  失败结果始终保留,因为那是诊断依据。

### Honest errors / 诚实的错误 (#21)

- New `mi_guard`: the ok/error verdict is derived from the raw MI records, not from "the
  command returned". It catches `^error` results plus the failures GDB only prints as
  log/console text. / 新增 `mi_guard`:ok/error 判定改为基于原始 MI 记录,同时捕获
  `^error` 结果与 GDB 仅以 log/console 文本形式输出的失败。
- `flash_firmware`/`flash_and_run` now fail loudly on `Error erasing flash`, and require a
  terminal MI result record — a download that returned before completion no longer reads as
  a successful flash. / `flash_firmware` 现在对 `Error erasing flash` 明确报错,并要求终结
  MI 结果记录:尚未完成就返回的下载不再被当作烧录成功。
- `load_symbols`, `write_memory`/`typed_memory`, and `verify_flash` are guarded the same way;
  `verify_flash` additionally fails on `compare-sections` **MIS-MATCHED** output, so a device
  running different code than the ELF can no longer pass verification. /
  `load_symbols`、`write_memory`/`typed_memory`、`verify_flash` 同样受保护;`verify_flash`
  额外检测 `compare-sections` 的 **MIS-MATCHED**。
- Error taxonomy gains `elf_load_failed`, `flash_failed`, and `flash_mismatch`. A bad ELF path
  is no longer classified as a missing host toolchain (GDB says "No such file or directory"
  for both). / 错误分类新增三类;错误的 ELF 路径不再被误判为"缺少宿主工具链"。

### Stop-event correctness / 停止事件正确性 (#22)

- `halt_execution` no longer sends `-exec-interrupt` to an already-halted target. The pending
  interrupt used to fire as a spurious `SIGINT` on the **next** resume, which made
  `continue_execution` and `run_for_duration` useless for the rest of the session. /
  `halt_execution` 不再对已停止的目标发送 `-exec-interrupt`:此前遗留的中断会在**下一次**
  恢复运行时以伪 `SIGINT` 触发,导致该会话后续的 `continue_execution` 失效。
- `run_and_wait` drains stale async records before resuming, so a previous halt's `*stopped`
  can no longer be reported as the stop of the current run. /
  `run_and_wait` 在恢复运行前清空滞留异步记录,避免上一次停止被当作本次停止上报。
- On timeout, the wait probes the target: if the core is verifiably halted, the result is a
  `stopped-no-notification` stop event with the current frame instead of a false timeout
  (Windows pipe polling was observed to drop the `*stopped` of a hit breakpoint). /
  等待超时时会探测目标:若核确实已停止,则返回带当前帧的 `stopped-no-notification` 事件,
  而非虚假超时(Windows 管道轮询曾丢失断点命中的 `*stopped`)。
- `run_for_duration` reports `ran_full_duration` and `stopped_early`. It no longer sleeps
  through the requested window and claims a clean free-run when the target stopped at the
  start of or during it. / `run_for_duration` 新增 `ran_full_duration` 与 `stopped_early`
  字段,不再在目标已停止的情况下佯装完成了整段自由运行。
- Windows backslash paths are normalized before every GDB command. `C:\proj\fw.elf` used to
  be eaten by MI escaping and silently load nothing while reporting success. /
  所有 GDB 命令前统一规范化 Windows 反斜杠路径:`C:\proj\fw.elf` 此前会被 MI 转义吞掉,
  静默加载失败却报告成功。

## [0.6.0] - 2026-07-22

Internal architecture release: the 3,600-line server.py monolith is decomposed into a
tool registry plus 16 domain modules. **The advertised tool surface is byte-identical**
(pinned by a golden snapshot test) — no client-visible behavior changes.

内部架构版本:3,600 行的 server.py 巨石文件拆分为工具注册表 + 16 个领域模块。
**对外工具面逐字节一致**(由金快照测试锁定)——客户端可见行为无任何变化。

### Architecture / 架构

- New `mcp_server/tools/` package: `@register(Tool(...))` co-locates every tool schema
  with its handler in a domain module (session, firmware, execution, breakpoint, memory,
  inspect, fault, rtos, logging, peripheral, config, meta, board, acceptance, design,
  pipeline); `TOOL_ORDER` pins the advertised order. server.py shrinks from 3,651 to
  ~500 lines and remains the composition root: session resolution, per-session locking,
  MERGED-family translation, error envelopes, and batch/call/run_scenario. /
  新增 `mcp_server/tools/` 包:`@register` 将每个工具的 schema 与 handler 同置于领域
  模块;`TOOL_ORDER` 锁定广告顺序。server.py 从 3,651 行缩至约 500 行,仅保留组合根职责。
- Handlers receive a per-dispatch `ToolContext` built from server globals at call time,
  preserving existing test monkeypatch surfaces and the recovery/journaling semantics. /
  handler 通过每次调度构建的 `ToolContext` 访问会话对象,保留既有测试 patch 面与恢复/
  日志语义。
- A golden snapshot test pins the full advertised surface (names, order, schemas,
  annotations, compact mode); mypy now covers the entire src tree with no exemptions,
  and CI enforces a coverage floor (81%). / 金快照测试锁定完整工具面;mypy 全树无豁免,
  CI 覆盖率下限 81%。

## [0.5.1] - 2026-07-22

Reliability polish: every GDB operation deadline is now centrally configurable, and several
small resource-management gaps are closed. No tool schemas or defaults changed.

可靠性打磨:所有 GDB 操作超时现已全部集中可配置,并修补了若干资源管理小缺口。
工具 schema 与默认值均无变化。

### Timeouts / 超时

- All previously hardcoded GDB deadlines in `gdb_client` (symbols, monitor, breakpoint list,
  step/finish, stack, source, disassembly, symbol lists, evaluation, coredump capture/load,
  flash verify) now route through named `TimeoutConfig` entries, so `set_timeouts` can widen
  any of them once for a slow probe and replayed sessions stay deterministic. Default values
  are unchanged. / `gdb_client` 中所有硬编码超时(符号加载、monitor、断点列表、单步/finish、
  栈、源码、反汇编、符号列表、表达式求值、coredump 采集/加载、烧录校验)全部改走命名
  `TimeoutConfig`,`set_timeouts` 可一次性放宽,重放会话保持确定性;默认值不变。
- A regression test asserts every timeout name used by `gdb_client` exists in `DEFAULTS`,
  guarding against silent fallback to the 1s default. / 新增回归测试断言 `gdb_client` 使用的
  超时名全部存在于 `DEFAULTS`,防止静默回退到 1 秒默认值。

### Robustness / 健壮性

- PC-sample symbolization (`profile_pc`) caches `info symbol` results (LRU, 4096 entries),
  cleared whenever the symbol table changes — repeated profiling no longer re-queries hot
  addresses. / PC 采样符号化新增 LRU 缓存(4096 条,符号表变更即清空),重复分析不再重复
  查询热点地址。
- Named-session port allocation probes each candidate with a real bind, skipping ports held
  by zombie OpenOCD or foreign processes, and fails loudly after 100 slots instead of looping.
  / 命名会话端口分配改为真实 bind 探测,跳过被僵尸 OpenOCD 或外部进程占用的端口,超过
  100 个槽位时明确报错而非死循环。
- `close_session` now prunes the closed session's dispatch lock, so `_session_locks` no
  longer grows without bound. / `close_session` 现在会清理已关闭会话的调度锁,
  `_session_locks` 不再无限增长。

## [0.5.0] - 2026-07-21

This update converges installation, configuration, MCP contracts, and hardware diagnostics.
It preserves the existing TextContent JSON API while adding native MCP structured results.

本次更新集中收敛安装、配置、MCP 契约和硬件故障诊断。现有 TextContent JSON API 保持兼容，
同时新增 MCP 原生结构化结果。

### Installation and configuration / 安装与配置

- Source entry points now work from an uninstalled clone. `stm32-gdb-mcp-check-env --json`
  reports readiness as GDB plus any supported backend and returns a failing exit code when
  prerequisites are missing. / 未安装包的干净源码也可运行入口；环境检查支持 JSON，并以
  “GDB + 任一 backend”为就绪条件，缺少前置条件时返回非零退出码。
- Codex installation now uses `codex mcp add`, verifies with `codex mcp get --json`, succeeds
  idempotently for matching configuration, requires `--force` for conflicts, and keeps
  `codex --print` as a valid TOML fallback. Deployment stops on installation failure and
  lists multiple ELF candidates instead of guessing. / Codex 安装改为 CLI 新增后验证；
  相同配置幂等成功，冲突需 `--force`，并保留合法 TOML 回退。部署失败立即停止，多个 ELF
  只列候选、不擅自选择。
- Debug config paths resolve relative to the config file. Profile backend arguments, probe
  serial, logging channels, and reset strategy now flow into session start, logging, reset,
  flash, and `flash_and_run`; `suggest_server_args` exposes `speed_khz`. /
  调试配置中的相对路径按配置文件目录解析；backend 参数、探针序列号、日志通道和复位策略会
  贯通启动、日志、复位与烧录工具，`suggest_server_args` 正式支持 `speed_khz`。

### MCP client experience / MCP 客户端体验

- Merged action tools expose precise generated `oneOf` schemas, every tool accepts
  `session`, and compact mode provides `tool_help` for hidden descriptions and schemas. /
  合并工具自动生成精确 `oneOf` schema，所有工具支持 `session`，compact 模式可用
  `tool_help` 查询隐藏工具。
- Tool results now include `structuredContent`, a shared `outputSchema`, and `isError=true`
  for error envelopes while preserving the original JSON text. Read/write/external-write
  annotations let clients present appropriate risk prompts. /
  工具结果新增 `structuredContent`、统一 `outputSchema` 和错误 `isError=true`，同时保留
  原 JSON 文本；工具 annotations 帮助客户端正确提示只读、硬件写入和外部写入风险。

### Diagnostics, HIL, and release gates / 诊断、HIL 与发行门禁

- Connection failures distinguish `probe_busy`, `probe_unavailable`, `target_unreachable`,
  `debug_auth_required`, `invalid_target_config`, and `tool_missing`. Only transient probe/USB
  faults retry; partial starts clean up GDB/OpenOCD and return attempted settings plus bounded
  server logs. / 连接失败按上述错误码分类，仅瞬时探针/USB 故障重试；启动中途失败会清理
  GDB/OpenOCD，并返回尝试参数和受限长度日志。
- HIL smoke now uses the public MCP chain and requires decoded CPUID, Cortex-M core, and
  expected-family evidence without flashing. CI and release workflows gate clean-source
  entry points, distribution contents, clean-wheel installation, and console scripts. /
  HIL 烟测改走公开 MCP 调用链，必须验证已解码 CPUID、Cortex-M 内核和预期系列，且不烧录；
  CI/发行流程新增干净源码入口、发行内容、clean-wheel 安装和 console script 门禁。
- Public README, installation, troubleshooting, release, HIL, skills, firmware examples,
  and debug prompts are bilingual. The bundled plugin advances to `0.2.0` because its skills
  and SessionStart guidance changed. / 公开 README、安装、排障、发行、HIL、skills、固件示例
  和调试提示均完成中英双语；因内置 skills 与 SessionStart 引导变化，插件升级到 `0.2.0`。
- Physical probe discovery now uses host USB state rather than OpenOCD's compiled adapter
  list, preserves identical probes by serial, and auto-selects only when exactly one probe is
  connected. Keil builds verify the target reported by UV4. / 物理探针识别改用主机 USB 状态，
  不再误用 OpenOCD 编译驱动列表；同型号探针按序列号保留，且仅在唯一探针时自动选择。
  Keil 构建会核对 UV4 实际报告的 Target。
- HIL now provides non-flashing L151/L431/U535 profiles with exact Cortex-M expectations and
  retained JSON/JUnit evidence. CI covers Windows Python 3.10-3.13 plus Linux 3.13; tagged
  releases publish wheel/sdist/checksums to GitHub after PyPI succeeds. / HIL 新增不烧录的
  L151/L431/U535 配置及精确内核断言，并保留 JSON/JUnit 证据；CI 覆盖 Windows Python
  3.10-3.13 与 Linux 3.13，标签发行在 PyPI 成功后向 GitHub 发布 wheel、sdist 和校验和。

## [0.4.0] - 2026-07-14

### Tooling / 工具链

- Added `.github/workflows/release.yml`: pushing a `v*` tag now runs the quality gate (ruff / pytest / compileall), builds the sdist + wheel, checks the metadata with `twine`, and publishes to **PyPI via Trusted Publishing (OIDC)** — no API token is stored in the repo. `workflow_dispatch` runs everything except the publish step for a safe dry run. One-time PyPI trusted-publisher + `pypi` environment setup is documented in `docs/release.md`. 新增 `.github/workflows/release.yml`：推送 `v*` 标签即跑质量门禁、构建 sdist + wheel、用 `twine` 校验元数据，并通过 **PyPI 可信发布 (OIDC)** 发布（仓库不存 token）；`workflow_dispatch` 可做除发布外的安全演练；一次性配置见 `docs/release.md`。

## [0.3.0] - 2026-07-01

First tagged release. Turns the STM32 GDB MCP server into a **spec-to-silicon autonomous pipeline**: a netlist plus a product spec becomes a framework design, HAL code, and a machine-checked acceptance spec, then hands off to the bounded closed-loop debug-verify already in the server. The machine layer stays deterministic and never hallucinates — every unknown surfaces as an honest `unresolved` / `located:false` / `TODO` / `conflict` marker rather than a guess. 首个打标签发布：把 STM32 GDB MCP 服务器演进为**从规格到芯片的自主流水线**——网表加产品规格生成框架设计、HAL 代码与机检验收规格，随后交接给服务器内既有的有界闭环调试验证；机器层保持确定性、绝不臆造，一切未知都以诚实的 `unresolved` / `located:false` / `TODO` / `conflict` 标记呈现，而非猜测。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar G — end-to-end orchestration)

- The whole deterministic **design half** now runs in a single call. Until now, turning a netlist plus a product spec into a flashable skeleton meant hand-chaining seven tools in the right order, wiring each output into the next input, and manually collecting the `unresolved` gaps every stage reports independently -- the last of the five pipeline-review weak points. The new **`run_pipeline`** capstone tool re-dispatches the existing, individually verified tools in dependency order (`import_netlist? → import_spec? → design_framework → solve_clock_tree? → solve_timer? → render_framework → synthesize_acceptance`) and returns one consolidated report, bringing the tool count to 79. **Pure orchestration -- nothing new is guessed here**: the only added logic is choosing which stages to run (optional stages run only when their input is present; `solve_timer` is gated on the post-design plan's timer targets) with what arguments, and aggregating every stage's honest `unresolved` / `conflict` items -- each tagged with its originating stage and type -- into a single "human decisions / data still needed" list. Honest by construction: a required-stage hard error (no board, invalid input) stops the run and is reported as `pipeline_status = "blocked"` at that stage, keeping the sub-tool's own error code and message; expected gaps (an alternate-function number needing a pin DB, an unmodelled clock device) are **not** failures -- the pipeline runs through and surfaces them as `complete_with_unresolved`; a clean run is `complete`. The report hands back the products in hand -- the `mcu`, the rendered `files`, and the `acceptance` spec highlights -- always as a success envelope. Deterministic design half only: it stops before build / flash / verify (the hardware half, already orchestrated by the Pillar C acceptance loop) and hands off.
- 现在整个确定性的**设计前半程**都能一次调用完成。此前，把网表加产品规格变成可烧录骨架，意味着要按正确顺序手工串起七个工具、把每个输出接到下一个输入、并手动收集每个阶段各自上报的 `unresolved` 缺口——这正是流水线评审五个薄弱点中的最后一个。新增的收官工具 **`run_pipeline`** 会按依赖顺序重新派发既有的、已各自验证过的工具（`import_netlist? → import_spec? → design_framework → solve_clock_tree? → solve_timer? → render_framework → synthesize_acceptance`），并返回一份汇总报告，使工具总数增至 79。**纯编排——这里不臆造任何新东西**：唯一新增的逻辑是决定运行哪些阶段（可选阶段仅在其输入存在时运行；`solve_timer` 依据设计后计划中的定时器目标决定）、以何参数运行，以及把每个阶段各自诚实的 `unresolved` / `conflict` 项——每项都标注其来源阶段与类型——汇聚成一份“待人决策 / 仍需数据”清单。天生诚实：必需阶段的硬错误（无板卡、非法输入）会中止运行并在该阶段以 `pipeline_status = "blocked"` 如实上报，保留子工具自身的错误码与消息；而预期内的缺口（需要引脚库的复用功能号、未建模的时钟器件）**不算失败**——流水线会照常跑完并以 `complete_with_unresolved` 呈现；干净跑完则为 `complete`。报告把成品直接交到手上——`mcu`、渲染出的 `files`、以及 `acceptance` 规格要点——且始终以成功信封返回。仅限确定性设计前半程：它止步于编译 / 烧录 / 验证（硬件后半程，已由 Pillar C 验收环编排），随后交接。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar F — data-driven device packs)

- Adding STM32 family coverage is now "supply verified data", never "trust the model". The four device-specific fact tables the deterministic solvers depend on -- the clock PLL profile, the DMA request-routing table, the irregular NVIC vectors, and which timers sit on APB2 / are 32-bit -- were hardcoded for STM32F4 / L4 inside the solvers, so every new family meant hand-coding another datasheet table (a hallucination risk the machine layer must never take). Those facts now live in a pure **`device_packs`** registry keyed by family. A **device pack** is a validated JSON object (schema `stm32-device-pack/v1`) of verified facts for one family, with optional `clock` / `dma` / `nvic` / `timer` sections; **STM32F4 and STM32L4 ship as built-in packs** with their facts relocated *verbatim*, so their behavior is byte-identical. `dma_solver` / `interrupt_solver` / `timer_solver` / `clock_solver` now read every device fact through registry accessors -- their public signatures are unchanged. The new **`load_device_pack`** tool registers a pack from a `path` or inline `pack` (`allow_override` to shadow a built-in; called with no arguments it reports current coverage), bringing the tool count to 78. This generalizes the same data-driven discipline that GPIO **alternate-function** numbers already used (a CubeMX `db_path` / `af_map`): the machine owns the logic, verifiable data supplies the device fact. Honest by construction -- a malformed pack is rejected with the full list of problems and never half-loaded, shadowing a built-in needs `allow_override`, and a family with no pack stays honestly `unresolved` (a guessed DMA stream or PLL profile is never emitted). Covering G4 / F7 / H7 or a new peripheral kind is now a follow-up that supplies a verified pack, not a code change.
- 扩展 STM32 系列覆盖现在是“提供经核验的数据”，而不再是“相信模型的记忆”。确定性求解器所依赖的四张器件相关事实表——时钟 PLL 配置、DMA 请求路由表、不规则的 NVIC 向量、以及哪些定时器位于 APB2 / 为 32 位——原先在各求解器内为 STM32F4 / L4 硬编码，因此每加一个系列都意味着再手写一张数据手册表格（这是机器层绝不能承担的臆造风险）。这些事实现在集中到一个纯粹的、按系列索引的 **`device_packs`** 注册表中。一个 **device pack（器件包）** 是针对单个系列、经过校验的 JSON 对象（schema `stm32-device-pack/v1`），含可选的 `clock` / `dma` / `nvic` / `timer` 段；**STM32F4 与 STM32L4 作为内置包**，其事实*原样*迁入，因此行为逐字节一致。`dma_solver` / `interrupt_solver` / `timer_solver` / `clock_solver` 现在都通过注册表访问器读取每一项器件事实——它们的公共签名保持不变。新增 **`load_device_pack`** 工具可从 `path` 或内联 `pack` 注册器件包（`allow_override` 用于覆盖内置包；不带参数调用时报告当前覆盖情况），工具总数增至 78。这把 GPIO **复用功能**号早已采用的数据驱动纪律（CubeMX 的 `db_path` / `af_map`）推广开来：机器负责逻辑，可核验的数据提供器件事实。天生诚实——畸形的器件包会连同完整问题清单一并被拒绝、绝不半加载，覆盖内置包需要 `allow_override`，没有器件包的系列如实保持 `unresolved`（绝不发出臆造的 DMA 流或 PLL 配置）。覆盖 G4 / F7 / H7 或新的外设种类，现在成了“提供一个经核验的器件包”的后续工作，而非改代码。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar E — failure→source provenance)

- A failing acceptance check now points straight at the line of init code that was supposed to make it pass. Every check the machine derives carries a `provenance` join key taken **verbatim from the plan** (an RCC clock macro, an IRQ vector name, a port-pin like `PA9`, or the stopped-at symbol); `render_framework` returns a `source_map` scanned from the rendered init text (per file: the init functions with their line spans, plus the tagged constructs -- `clock_enable` / `nvic_enable` / `gpio_mode` -- each with its 1-based line); and `synthesize_acceptance` welds the two at synth time so every stored check gains `provenance.source = {located, file, init_fn, line, text}`. Because `evaluate_acceptance` threads that provenance onto every **non-pass** result, a failing `run_acceptance` verdict **and** every Pillar-C debug-loop verdict now carry `result.provenance.source` -- e.g. `bsp_init.c:123 in MX_USART1_UART_Init` -- so a fix is precision-guided instead of a whole-file hunt. It stays honest and deterministic like the rest of the machine layer: the join is compiler-debug-line-style (one check <- one plan element <- one rendered construct), never a fuzzy guess; a stopped-at symbol that is not an emitted init function, or a construct the plan never emitted (an unresolved/TODO, or plan drift since synth), is surfaced as `located:false` with a reason, never a fabricated file/line; passing checks stay lean (no provenance noise). No new tool -- provenance is baked into the spec at synth time and rides the existing `synthesize_acceptance` / `run_acceptance` surface, so the tool count stays 77.
- 验收检查失败时，现在能直接指向本应让它通过的那一行初始化代码。机器派生的每条检查都带有一个**从计划原样取来**的 `provenance` 关联键（RCC 时钟宏、IRQ 向量名、像 `PA9` 这样的端口引脚，或停驻符号）；`render_framework` 返回一份从渲染出的初始化文本扫描得到的 `source_map`（逐文件：各初始化函数及其行区间，以及带标签的构造——`clock_enable` / `nvic_enable` / `gpio_mode`——每个都带以 1 起始的行号）；而 `synthesize_acceptance` 在综合时把两者焊接起来，使每条存储的检查获得 `provenance.source = {located, file, init_fn, line, text}`。由于 `evaluate_acceptance` 把该来源信息附加到每条**非通过**结果上，失败的 `run_acceptance` 判据以及每次 Pillar-C 调试环判据现在都带有 `result.provenance.source`——例如 `bsp_init.c:123 in MX_USART1_UART_Init`——于是修复是精确制导的，而不是全文件翻找。它与机器层其余部分一样诚实且确定：这种关联如同编译器调试行信息（一条检查 <- 一个计划元素 <- 一处渲染构造），绝非模糊猜测；若某停驻符号不是被发出的初始化函数、或某构造是计划从未发出的（unresolved/TODO，或综合之后计划发生漂移），则如实呈现为 `located:false` 并附原因，绝不臆造文件/行；通过的检查保持精简（无 provenance 噪声）。无新增工具——provenance 在综合时烘焙进规格，沿用既有的 `synthesize_acceptance` / `run_acceptance` 接口，工具总数保持 77。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (deeper auto-derived acceptance)

- The machine-generated acceptance judge now verifies more of what the plan actually does. On top of the no_fault check and RCC clock-enable checks, `synthesize_acceptance` derives two more honest kinds. **NVIC ISER** checks -- for every interrupt the plan enables (peripheral vectors *and* DMA stream vectors), assert the Cortex-M set-enable bit is set (`ISER[n] = 0xE000E100 + 4*n`, IRQ `k` -> bit `k % 32`); the only device-specific fact, the IRQ *number* for a vector name, comes from the loaded SVD (new `svd_parser.interrupt_numbers()`) or an explicit `irq_map`. **GPIO MODER** checks -- for every configured pin, a masked-equality assert that its two mode bits are AF (`0b10`) or analog (`0b11`), with the port base from the SVD's `GPIO<port>.MODER` address or a `gpio_map`. Both placements are architecture-standard, so once the one device-specific number/base is resolved the check is exact; anything unresolvable is surfaced in `unresolved`, never guessed, and STM32F1 GPIO (CRL/CRH, not MODER) is skipped honestly. Peripheral-enable bits (USART `UE`, SPI `SPE`, ...) stay deliberately out of scope: not arch-standard and HAL's post-Init state is non-uniform, so a derived check could falsely fail. The tool gains `irq_map` / `gpio_map` / `include_nvic` / `include_gpio` and reports `resolver_sources` + `nvic_checks` / `gpio_checks`.
- 机器生成的验收判据现在能核对计划实际所做的更多事情。在 no_fault 与 RCC 时钟使能检查之上，`synthesize_acceptance` 又确定性地派生出两类诚实检查。**NVIC ISER** 检查——对计划使能的每个中断（外设向量*以及* DMA 流向量），断言 Cortex-M 置位使能位已置（`ISER[n] = 0xE000E100 + 4*n`，IRQ `k` -> 位 `k % 32`）；其中唯一与器件相关的事实——向量名对应的 IRQ *号*——来自已加载的 SVD（新增 `svd_parser.interrupt_numbers()`）或显式 `irq_map`。**GPIO MODER** 检查——对每个已配置引脚，用带掩码的相等断言其两位模式为 AF（`0b10`）或模拟（`0b11`），端口基址取自 SVD 的 `GPIO<port>.MODER` 地址或 `gpio_map`。两处地址都是架构标准，因此只要那一个器件相关的号/基址被解析出来，检查就是精确的；任何无法解析的都在 `unresolved` 中呈现、绝不臆造，而 STM32F1 的 GPIO（用 CRL/CRH 而非 MODER）被诚实地跳过。外设使能位（USART `UE`、SPI `SPE` 等）仍被有意排除：它们不是架构标准，且 HAL 在 Init 后的状态不统一，派生检查可能误报。工具新增 `irq_map` / `gpio_map` / `include_nvic` / `include_gpio`，并返回 `resolver_sources` 与 `nvic_checks` / `gpio_checks`。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (upstream product-spec guard)

- New **`import_spec`** tool + `spec_model.py` close the pipeline's last hand-written link that had **no machine guard** -- and the most upstream one, where a mistranslation would silently propagate through every deterministic stage below and generate precisely-wrong code. Instead of hand-writing HAL macros into `design_framework`, you give a **controlled-vocabulary product spec** in human/product terms -- UART `framing` '8N1', `direction` 'txrx', `flow_control` 'rtscts'; SPI `role`, `spi_mode` 0..3, `data_size`, `bit_order`; I2C `speed`, `addressing`; ADC `resolution`, `conversion`; a timer `update_hz`; plus `dma` / `interrupt` / `priority` opt-ins -- and the machine expands it to design params deterministically, then `design_framework(from_spec=true)` builds the plan (explicit design keys still win when both are given). Framing follows HAL's rule that the parity bit lives inside WordLength (8E1 -> `UART_WORDLENGTH_9B` + `UART_PARITY_EVEN`); SPI mode 0..3 -> the standard CPOL/CPHA pair. And it is honest like every other pillar: the spec is cross-checked against the imported netlist, so a peripheral the board does not wire is a `conflict` (left out of the design, no code for absent hardware), an intent key or value the machine does not model is `unresolved`, and the clock- and family-dependent I2C bus-timing register is recorded as a target but never fabricated -- surfaced, never guessed. The agent's creative job shrinks to "turn the human requirements doc into a controlled spec"; the mechanical, error-prone translation is now deterministic.
- 新增 **`import_spec`** 工具 + `spec_model.py`，补上了流水线上最后一个 **没有机器护栏**的手写环节——而且是最上游的那个：这里翻译错了会悄无声息地传到下游每一个确定性阶段，生成精确错误的代码。不再让 agent 向 `design_framework` 手写 HAL 宏，而是用人类/产品术语给出一份 **受控词汇的产品规格**——UART 的 `framing` '8N1'、`direction` 'txrx'、`flow_control` 'rtscts'；SPI 的 `role`、`spi_mode` 0..3、`data_size`、`bit_order`；I2C 的 `speed`、`addressing`；ADC 的 `resolution`、`conversion`；定时器的 `update_hz`；以及 `dma` / `interrupt` / `priority` 开关——机器确定性地将其展开为 design 参数，再由 `design_framework(from_spec=true)` 构建方案（两者同时给出时显式 design 键优先）。framing 遵循 HAL "校验位计入 WordLength" 的规则（8E1 -> `UART_WORDLENGTH_9B` + `UART_PARITY_EVEN`）；SPI mode 0..3 -> 标准的 CPOL/CPHA 组合。与其他支柱一样诚实：规格会与导入的网表交叉校验，板子没有接的外设是 `conflict`（不进设计，不为不存在的硬件生成代码），机器不建模的键或值是 `unresolved`，而依赖时钟与系列的 I2C 总线时序寄存器只记录目标但绝不臆造——呈现，而非猜测。agent 的创造性工作缩小为“把人类需求文档变成受控规格”，而易错的机械翻译现在是确定性的。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — DMA association)

- `design_framework` now **auto-attaches DMA streams** to a peripheral straight from intent and reuses the NVIC backbone for the transfer interrupt. Opt in per peripheral (`design={'USART1': {'dma': true}}`, or `'rx'` / `'tx'` / `['rx','tx']`, plus an optional `dma_priority`) and the generated init emits the full `DMA_HandleTypeDef` wiring -- `Instance`, the request selector (`Init.Request = DMA_REQUEST_n` on L4, `Init.Channel = DMA_CHANNEL_n` on F4), direction, the `__HAL_RCC_DMAn_CLK_ENABLE`, `HAL_DMA_Init`, and `__HAL_LINKDMA(&hperiph, hdmarx/hdmatx/DMA_Handle, hdma_...)` -- followed by the DMA stream/channel `HAL_NVIC_SetPriority` + `HAL_NVIC_EnableIRQ` and one ISR per DMA vector dispatching into `HAL_DMA_IRQHandler`. The request routing is a fixed hardware fact encoded in a **verified** per-family table (RM0090 F4 stream+channel / RM0394 L4 channel+CSELR request) covering USART1 / SPI1 / I2C1 / ADC1, cross-checked against ground-truth CubeMX output (an AI summary got the SPI1/ADC1 request numbers wrong -- exactly why the machine layer never guesses). The DMA stream IRQ vector itself is *derived* from the resolved stream by the regular `DMAc_Streams_IRQn` / `DMAc_Channels_IRQn` rule. Deterministic and honest, like the rest of the machine layer: a peripheral or family not in the table, an ADC transmit mismatch, or two peripherals colliding on one DMA instance is surfaced as `dma_unresolved` / `dma_conflict` and rendered as a clear `TODO`, never a guessed stream. No new tool: DMA rides the existing `design_framework` design param, so the tool count is unchanged.
- `design_framework` 现在直接从意图为外设 **自动挂接 DMA 流**，并复用 NVIC 骨干处理传输中断。逐外设按需开启（`design={'USART1': {'dma': true}}`，或 `'rx'` / `'tx'` / `['rx','tx']`，以及可选的 `dma_priority`），生成的初始化即发出完整的 `DMA_HandleTypeDef` 连线——`Instance`、请求选择子（L4 上 `Init.Request = DMA_REQUEST_n`，F4 上 `Init.Channel = DMA_CHANNEL_n`）、方向、`__HAL_RCC_DMAn_CLK_ENABLE`、`HAL_DMA_Init`、以及 `__HAL_LINKDMA(&hperiph, hdmarx/hdmatx/DMA_Handle, hdma_...)`——随后是 DMA 流/通道的 `HAL_NVIC_SetPriority` + `HAL_NVIC_EnableIRQ`，以及每个 DMA 向量一个调用 `HAL_DMA_IRQHandler` 的 ISR。请求路由是固定的硬件事实，编码于一份**已核实**的逐系列表（RM0090 F4 流+通道 / RM0394 L4 通道+CSELR 请求），覆盖 USART1 / SPI1 / I2C1 / ADC1，并与 CubeMX 实际输出交叉校验（某 AI 摘要把 SPI1/ADC1 的请求号给错了——这正是机器层绝不臆测的原因）。DMA 流中断向量本身由已解析的流按规则 `DMAc_Streams_IRQn` / `DMAc_Channels_IRQn` *推导*。与机器层其余部分一致，确定且诚实：表中没有的外设或系列、ADC 发送方向不匹配、或两个外设冲突于同一 DMA 实例，都以 `dma_unresolved` / `dma_conflict` 呈现并渲染为明确的 `TODO`，绝不臆测。无新增工具：DMA 沿用既有的 `design_framework` 设计参数，工具总数不变。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — NVIC interrupt backbone)

- `design_framework` now wires the **NVIC interrupt backbone** for a peripheral straight from intent: opt in per peripheral (`design={'USART1': {'nvic': true}}`, or `nvic_priority: 5` / `[preempt, sub]`, or an explicit `irqn=` override) and the generated init emits `HAL_NVIC_SetPriority` + `HAL_NVIC_EnableIRQ` for every resolved vector plus one dispatching interrupt service routine per vector that calls the matching HAL handler (`HAL_UART_IRQHandler`, `HAL_I2C_EV/ER_IRQHandler`, `HAL_TIM_IRQHandler`, ...). Vector names that follow the universal CMSIS rule are derived (uart/spi -> `{name}_IRQn`; i2c -> `{name}_EV_IRQn` + `{name}_ER_IRQn` on families with the EV/ER split); the irregular shared vectors (TIM2-5, TIM6_DAC, ADC) come from a built-in per-family table (F4/L4). A shared vector emits a single ISR that fans out to every attached handle, so no duplicate-symbol errors. Deterministic and honest, like the rest of the machine layer: a peripheral with no explicit priority gets a documented default (preempt 5 / sub 0, flagged for RTOS review), and an interrupt whose vector is genuinely unknown -- advanced timers TIM1/TIM8, folded TIM9-17, an i2c on a family without the EV/ER split -- is surfaced as `nvic_unresolved` and rendered as a clear `TODO: enable <periph> interrupt`, never a guessed IRQn (supply `irqn=` to resolve). No new tool: interrupts ride the existing `design_framework` design param, so the tool count is unchanged.
- `design_framework` 现在直接从意图接好外设的 **NVIC 中断骨干**：逐外设按需开启（`design={'USART1': {'nvic': true}}`，或 `nvic_priority: 5` / `[抢占, 子优先级]`，或显式 `irqn=` 覆盖），生成的初始化即为每个已解析向量发出 `HAL_NVIC_SetPriority` + `HAL_NVIC_EnableIRQ`，并为每个向量生成一个分发式中断服务程序，调用对应的 HAL 处理函数（`HAL_UART_IRQHandler`、`HAL_I2C_EV/ER_IRQHandler`、`HAL_TIM_IRQHandler` 等）。符合 CMSIS 通用规则的向量名自动推导（uart/spi -> `{name}_IRQn`；i2c 在具备 EV/ER 拆分的族系上 -> `{name}_EV_IRQn` + `{name}_ER_IRQn`）；不规则的共享向量（TIM2-5、TIM6_DAC、ADC）取自内置的逐族系表（F4/L4）。共享向量只生成一个 ISR 并扇出到每个挂接句柄，避免重复符号错误。与机器层其余部分一致，确定且诚实：未显式给优先级的外设采用有文档记录的默认值（抢占 5 / 子 0，并标注供 RTOS 复核），而向量确实未知的中断——高级定时器 TIM1/TIM8、折叠的 TIM9-17、无 EV/ER 拆分族系上的 i2c——会以 `nvic_unresolved` 呈现并渲染为明确的 `TODO: enable <外设> interrupt`，绝不臆测 IRQn（传入 `irqn=` 即可解析）。无新增工具：中断沿用既有的 `design_framework` 设计参数，工具总数不变。

### Spec-to-silicon pipeline / 芯片流水线 (Pillar D Tier 3 — timer base-frequency solver)

- New `solve_timer` tool turns a timer's **target update frequency into concrete `Prescaler` (PSC) and `Period` (ARR)** register values. Record intent with `design_framework(design={'TIM3': {'update_hz': 1000}})`, solve the clock tree, then `solve_timer` fills the two values that used to be `/* TODO */` holes. The timer input clock (TIMxCLK) is derived from the solved clock tree via the STM32 `PCLKx x1/x2` rule, and the APB bus plus 16/32-bit counter width are resolved per family (F4/L4). Deterministic and honest: an exact target yields zero-error PSC/ARR, an inexact one reports the achieved frequency and ppm error, and an unrepresentable target or an unknown timer bus is surfaced (`no_clock_solution` / `bus_unknown` / `target_too_fast` / `target_too_slow`), never guessed. Pass `timer_clock_hz=` for a pure what-if.
- 新增 `solve_timer` 工具：把定时器的**目标更新频率自动求解为具体的 `Prescaler`（PSC）与 `Period`（ARR）**寄存器值。用 `design_framework(design={'TIM3': {'update_hz': 1000}})` 记录意图，求解时钟树后，`solve_timer` 即可填上原先是 `/* TODO */` 的两个值。定时器输入时钟（TIMxCLK）依据 STM32 的 `PCLKx x1/x2` 规则从已求解的时钟树推导，APB 总线与 16/32 位计数器位宽按系列（F4/L4）解析。确定性且诚实：目标可整除时给出零误差 PSC/ARR，不可整除时报告实际频率与 ppm 误差，无法表示的目标或未知的定时器总线会被如实标注（`no_clock_solution` / `bus_unknown` / `target_too_fast` / `target_too_slow`），绝不臆测。传入 `timer_clock_hz=` 可做纯假设推演。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — complete peripheral .Init structs)

- `design_framework` now emits **complete, valid peripheral `.Init` structs** instead of only the handful of fields the engineer typed. Previously `design={'USART1': {'baud': 115200}}` rendered a struct with `BaudRate` set but `WordLength` / `StopBits` / `Parity` / `Mode` / `OverSampling` left uninitialized — a latent bug that hands the HAL stack-junk configuration. Every mandatory member is now filled with the HAL-standard default CubeMX itself emits (UART 8N1 / 16x oversample / TX+RX; SPI master / 8-bit / mode 0 / MSB; I2C 7-bit addressing / no-stretch; TIM up-counting / div1), and a few values are derived straight from the netlist: UART hardware flow control from the RTS/CTS pins (`UART_HWCONTROL_RTS_CTS` / `_RTS` / `_CTS` / `_NONE`) and SPI NSS management from a hardware NSS/CS pin (`SPI_NSS_HARD_OUTPUT` vs `_SOFT`). Every field is tagged `explicit` / `derived` / `default` and rendered with a trailing `/* default */` or `/* derived: ... */` comment, with precedence explicit > derived > default so your value always wins. True to the deterministic layer, values that genuinely need a human decision and have no safe universal default — UART baud, TIM Prescaler/Period, I2C Timing/ClockSpeed (variant- and clock-dependent) — are never invented: they surface as `param_unresolved` and render as a clear `TODO: set <handle>.Init.<field>`. / `design_framework` 现在生成**完整、有效的外设 `.Init` 结构体**，而非仅工程师手写的那几个字段。此前 `design={'USART1': {'baud': 115200}}` 渲染出的结构体只设了 `BaudRate`，而 `WordLength` / `StopBits` / `Parity` / `Mode` / `OverSampling` 未初始化——一个会把栈上垃圾值当配置交给 HAL 的隐患。现在每个强制成员都用 CubeMX 本身会生成的 HAL 标准默认值填充（UART 8N1 / 16 倍过采样 / 收发；SPI 主机 / 8 位 / 模式0 / MSB；I2C 7 位地址 / 不拉伸；TIM 向上计数 / div1），并从网表直接派生几个值：UART 硬件流控由 RTS/CTS 引脚推导（`UART_HWCONTROL_RTS_CTS` / `_RTS` / `_CTS` / `_NONE`）、SPI NSS 由硬件 NSS/CS 引脚推导（`SPI_NSS_HARD_OUTPUT` 或 `_SOFT`）。每个字段标注 `explicit` / `derived` / `default` 并带尾部 `/* default */` 或 `/* derived: ... */` 注释，优先级 explicit > derived > default，你的显式值总是胜出。恰守确定性层原则：真正需要人为决策且无安全通用默认值的参数——UART 波特率、TIM Prescaler/Period、I2C Timing/ClockSpeed（因外设版本与内核时钟而异）——绝不臆测：它们以 `param_unresolved` 呈现，并渲染为明确的 `TODO: set <handle>.Init.<field>`。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — GPIO alternate-function resolution)

- `design_framework` now derives GPIO **alternate-function numbers** from the pin-capability DB, erasing the last routine hand-written gap in the generated init code. Pin-capability entries accept an optional integer `"af"`, so the same CubeMX-derived DB that checks AF *legality* (`validate_board`) now also supplies the AF *number* during synthesis. Point `design_framework` at the DB (`db_path`, or the `STM32_GDB_MCP_PIN_DB` env, mirroring `validate_board`) and the rendered code carries concrete `GPIO_AF<n>_<PERIPH>` values instead of `TODO: Alternate` markers — with zero hand-written `af_map`. An explicit `af_map` still wins per pin (it is merged on top of the DB), so you can correct or extend the DB without restating every pin. Honest by construction: a pin the DB does not know stays an `af_unknown` / datasheet `TODO`, never a guessed number, and a bad `db_path` is an explicit `invalid_db` error. / `design_framework` 现在从引脚能力库推导 GPIO **复用功能号**，抹去生成初始化代码中最后一处常规手写缺口。引脚能力条目新增可选整数 `"af"`，于是同一份 CubeMX 导出的库——既用于校验 AF *合法性*（`validate_board`），现在也在合成时提供 AF *编号*。将 `design_framework` 指向该库（`db_path`，或 `STM32_GDB_MCP_PIN_DB` 环境变量，与 `validate_board` 一致），渲染出的代码就带有具体的 `GPIO_AF<n>_<PERIPH>` 而非 `TODO: Alternate` 标记——无需任何手写 `af_map`。显式 `af_map` 仍按引脚优先（叠加在库之上），可在不重写每个引脚的情况下修正或扩展该库。本质上诚实：库中未知的引脚仍保留 `af_unknown` / 数据手册 `TODO`，绝不臆测编号；错误的 `db_path` 则是明确的 `invalid_db` 错误。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — clock-tree solver)

- Added `solve_clock_tree`: synthesize a concrete `SystemClock_Config()` for the session's FrameworkPlan — filling the single most conspicuous hand-written gap in the generated init code. Given a clock source (HSE + crystal Hz, or HSI) and a target SYSCLK, it computes the exact PLL dividers (M/N, PLLP on F4 or PLLR on L4, plus Q for an exact 48 MHz USB/SDIO/RNG clock), the AHB/APB bus prescalers, and the flash wait-states by pure bounded datasheet math, then stores the result on the plan so the next `render_framework` emits a real HAL clock configuration instead of a TODO stub. Deterministic and honest, like the rest of the machine layer: an unmodelled device (no built-in profile and no explicit `profile`), an HSE with no crystal frequency, or an infeasible target is surfaced in `unresolved`, never guessed. Built-in profiles ship for STM32F401/F407/F411 and mainstream L4 (<=80 MHz); the golden configs match vendor output (F407 HSE 8 MHz -> 168 MHz with exact 48 MHz USB; L431 HSI16 -> 80 MHz). This closes the last deterministic gap in 网表图 + 产品规格 -> 框架设计 + 代码编写. / 新增 `solve_clock_tree`：为会话的 FrameworkPlan 合成具体的 `SystemClock_Config()`——填补生成初始化代码中最显眼的手写空白。给定时钟源（HSE + 晶振 Hz，或 HSI）与目标 SYSCLK，它用纯数学精确求解 PLL 分频（M/N、F4 的 PLLP 或 L4 的 PLLR，以及用于精确 48 MHz USB/SDIO/RNG 的 Q）、AHB/APB 总线分频、以及 Flash 等待周期，再将结果存入 plan，使下一次 `render_framework` 生成真实的 HAL 时钟配置而非 TODO 桩。与机器层其余部分一致，确定且诚实：未建模的器件（无内置档案且未传 `profile`）、缺少晶振频率的 HSE、或不可行的目标都列入 `unresolved` 而非臆测。内置档案覆盖 STM32F401/F407/F411 与主流 L4（<=80 MHz）；黄金配置与原厂输出一致（F407 HSE 8 MHz -> 168 MHz 含精确 48 MHz USB；L431 HSI16 -> 80 MHz）。至此“网表图 + 产品规格 -> 框架设计 + 代码编写”的最后一个确定性缺口已补齐。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D Tier 3 — auto-derived acceptance)

- Added `synthesize_acceptance`: auto-derive a machine-checked **AcceptanceSpec** directly from the synthesized FrameworkPlan (Pillar D) and load it as the session's acceptance judge — so even the pass/fail judge is now machine-generated, welding design synthesis (D) to the acceptance judge (B1) and the bounded loop (C). It always emits a `no_fault` check (init must not HardFault — a target-independent ARM fact) and, for every clock the plan enables, a `memory_u32` `bits_set` check on the RCC enable bit, resolving each bit's placement from the session's loaded SVD or an explicit `register_map`. True to the deterministic layer, any clock whose RCC bit cannot be resolved is surfaced in `unresolved`, never guessed; the scope is deliberately bounded to `no_fault` + RCC clock-enable checks (peripheral-enable and GPIO-mode checks are deferred because their register layouts differ across families). / 新增 `synthesize_acceptance`：直接从已合成的 FrameworkPlan（Pillar D）自动推导机器可校验的 **AcceptanceSpec** 并载入为该会话的验收裁判——至此连“通过/失败”裁判也由机器生成，把设计合成（D）、验收裁判（B1）与有界闭环（C）焊接为一体。它始终生成一个 `no_fault` 断言（初始化不得触发 HardFault——与目标无关的 ARM 事实），并为计划使能的每个时钟生成一个针对 RCC 使能位的 `memory_u32` `bits_set` 断言，其位偏移从会话已加载的 SVD 或显式 `register_map` 解析。恪守确定性层原则：任何无法解析 RCC 位的时钟都列入 `unresolved` 而非臆测；范围刻意限定为 `no_fault` + RCC 时钟使能断言（外设使能与 GPIO 模式断言因各族系寄存器布局差异过大而暂缓）。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar D — design synthesis)

- Added `design_framework`, `describe_framework`, and `render_framework`: synthesize a deterministic **FrameworkPlan** from the imported netlist board model (Pillar A) plus an optional per-peripheral design config, then render it to a HAL C init skeleton (`bsp_init.c` / `bsp_init.h`). The solver derives which clocks to enable, how each pin must be muxed (AF push-pull / open-drain / analog, pull, speed), and which peripheral init blocks to emit — in dependency order (clocks → GPIO → peripherals). Everything derivable from the board alone is exact; a value that needs target data (a GPIO alternate-function number) or a human decision (a baud rate) is surfaced in `unresolved` and rendered as a clearly marked `TODO`, never guessed. This closes the last hand-written link in the pipeline — 网表图 + 产品规格 → 框架设计 + 代码编写 is now machine-generated scaffolding the agent completes, flashes, and verifies via the acceptance loop (Pillar C). / 新增 `design_framework` / `describe_framework` / `render_framework`：从已导入的网表板级模型（Pillar A）及可选的逐外设设计配置合成确定性的 **FrameworkPlan**，再渲染为 HAL C 初始化骨架（`bsp_init.c` / `bsp_init.h`）。求解器推导需使能哪些时钟、每个引脚如何复用（复用推挽/开漏/模拟、上下拉、速度）、以及要生成哪些外设初始化块——并按依赖顺序（时钟 → GPIO → 外设）排列。凡从板级模型可推导的均为精确值；需要目标数据（GPIO 复用功能号）或人为决策（波特率）的值则列入 `unresolved` 并渲染为明确的 `TODO`，绝不臆测。至此流水线最后一个靠手写的环节打通——“网表图 + 产品规格 → 框架设计 + 代码编写”现为机器生成的脚手架，由 agent 补全、烧录，并经验收闭环（Pillar C）验证。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar C — bounded acceptance loop)

- Added `start_acceptance_loop`, `run_acceptance_iteration`, and `acceptance_loop_status`: a **bounded, agent-driven** closed loop that ties the netlist board model (Pillar A) and the AcceptanceSpec judge (Pillar B1) together. Each iteration does one deterministic *build → flash → run-to-state → evaluate* pass and returns an objective decision (`converged` / `should_continue` / `exhausted` / `stalled`) plus the exact checks still to fix. The machine owns the mechanics and the bounds — it stops on convergence, on `max_iterations`, or when the same checks keep failing (`stall_patience`) — while the agent supplies only the creative step (the code fix) between iterations. Build or run-to failures are recorded as a `phase_error`, never a crash; a terminal loop refuses to re-run unless `force=true`. This closes the full spec-to-silicon loop: 网表图 + 产品规格 → 框架/代码 → 调试验证 → 不过则继续改代码. / 新增 `start_acceptance_loop` / `run_acceptance_iteration` / `acceptance_loop_status`：一个**有界的、由 agent 驱动**的闭环，把网表板级模型（Pillar A）与 AcceptanceSpec 裁判（Pillar B1）串起来。每次迭代执行一轮确定性的*编译 → 烧录 → 运行到指定状态 → 求值*，并返回客观决策（`converged` / `should_continue` / `exhausted` / `stalled`）及仍需修复的具体断言。机器掌控机制与边界——收敛、达到 `max_iterations`、或同一批断言反复失败（`stall_patience`）时停止——而 agent 只在迭代间负责创造性的一步（改代码）。编译或运行失败记为 `phase_error`，而非崩溃；已终止的回路除非 `force=true` 否则拒绝重跑。至此完整的“从规格到芯片”闭环打通。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar B1 — acceptance)

- Added `load_acceptance`, `run_acceptance`, and `describe_acceptance`: turn a product spec into a machine-checked **AcceptanceSpec** (deterministic checks — `memory_u32` for any memory-mapped register, `variable` for a C global, `core_register`, `no_fault`, and `stopped_at`) and evaluate it against live silicon, returning a per-check pass/fail/error verdict. This is the closed-loop *judge* that lets an agent decide “verification failed → keep fixing” objectively; an unreadable target is reported as `error`, never a silent pass. / 新增 `load_acceptance` / `run_acceptance` / `describe_acceptance`：将产品规格转为机器可校验的 **AcceptanceSpec**（确定性断言——`memory_u32` 适用于任意内存映射寄存器、`variable` 读 C 全局变量、`core_register`、`no_fault`、`stopped_at`）并对真实芯片状态求值，逐项返回 通过/失败/错误 裁决。这是闭环的“裁判”，让 agent 客观地判定“验证不过→继续修改”；无法读取的目标报为 `error`，绝不静默通过。

### Spec-to-silicon pipeline / 从规格到芯片流水线 (Pillar A)

- Added `import_netlist` and `describe_board`: parse a schematic netlist (KiCad `.net`) into a machine-readable BoardDescription — MCU part/family/line, a per-pin map (package pin → port pin → net → inferred peripheral function), and power/ground nets — the input contract for automated framework design. / 新增 `import_netlist` 与 `describe_board`：将原理图网表（KiCad `.net`）解析为机器可读的 BoardDescription——MCU 型号/族系/产品线、逐脚映射（封装引脚 → 端口引脚 → 网络 → 推断的外设功能）以及电源/地网络——作为自动框架设计的输入契约。
- Added `validate_board`: check a BoardDescription for structural faults — a package pin wired to multiple nets (short), a peripheral signal routed to multiple pins, a port pin driven by multiple nets — plus missing power/ground/debug/reset nets, and (with an optional CubeMX-derived pin-capability DB via `db_path`/`STM32_GDB_MCP_PIN_DB`) alternate-function legality; unknown pins degrade to `unverified` rather than a false conflict. / 新增 `validate_board`：检测 BoardDescription 的结构性错误——同一封装引脚接到多个网络（短路）、同一外设信号布到多个引脚、同一端口引脚被多个网络驱动——以及缺失的电源/地/调试/复位网络，并在提供 CubeMX 引脚能力库（`db_path`/`STM32_GDB_MCP_PIN_DB`）时校验复用功能合法性；未知引脚降级为`unverified` 而非误报冲突。

### Toolchain & robustness / 工具链与健壮性

- Added `build_firmware`: build with Keil uVision (UV4), CMake, make, or a custom command; Keil `.axf` (ELF/DWARF) debugs through the existing tools like a `.elf`. / 新增 `build_firmware`,支持 Keil uVision(UV4)、CMake、make 或自定义命令构建;Keil 的 `.axf` 与 `.elf` 一样可被现有工具调试。
- `start_debug_session` now rejects openocd with empty `server_args` up front (clear guidance), and openocd config errors are classified as non-retryable `invalid_server_args` instead of a misleading `probe_unavailable`. / `start_debug_session` 现在对 openocd 缺少 `server_args` 提前给出清晰报错,openocd 配置错误被正确分类为不可重试的 `invalid_server_args`。

### Single-target excellence / 单端极致 (Phase 2)

- Comprehension layer: `read_core_registers`, `read_call_stack`, `read_frame_variables` now return decoded structured data + a one-line summary, with raw output opt-in via `include_raw`. / 理解层:核心读取工具返回解码后的结构化数据与一行摘要,原始输出通过 `include_raw` 可选。
- Minimal-step composites: `debug_until`, `capture_state`, `flash_and_run` collapse multi-step repro sequences into one call. / 最少步骤复合工具,将多步复现压缩为一次调用。
- Determinism: append-only session journal and declarative `run_scenario` replay (`get_session_journal`, `clear_session_journal`, `run_scenario`). / 确定性:仅追加会话日志与声明式 `run_scenario` 回放。
- Reliability: `self_check` (byte-order / Cortex-M core / device-family validation) and a structured error taxonomy with actionable next-actions. / 可靠性:`self_check` 链路自检与结构化错误分类。
- Observability: per-tool metrics (`get_session_metrics`), `get_session_timeline`, and run-id-correlated structured logging. / 可观测性:逐工具指标、会话时间线与按 run-id 关联的结构化日志。
- Reproducibility: `export_debug_report` bundles journal + metrics + profile (+ optional snapshot/coredump) into one run-id-keyed JSON artifact. / 可复现:`export_debug_report` 将日志+指标+profile 打包为单一工件。
- Reliability: retry/backoff for transient probe failures and `recover_session` to restart a dropped/wedged probe; centralized overridable timeouts (`get_timeouts`/`set_timeouts`). / 可靠性:瞬时探针失败的重试退避、`recover_session` 会话恢复、集中可覆盖的超时配置。
- Fixed a byte-order bug in 32-bit memory word reads, and stale first-read after reset, both found via HIL on STM32L431. / 修复 32 位内存字读取字节序 bug 与复位后首次读脏数据(均在 STM32L431 真机验证中发现)。

### Autonomous debug loop / 自主调试闭环

- Added `run_and_wait` / `wait_for_stop` structured stop events to close the observe loop. / 新增 `run_and_wait`/`wait_for_stop` 结构化停止事件以闭合观察环。
- Added source symbolization and frame navigation: `select_frame`, `read_frame_variables`, `list_source`, `resolve_address`. / 新增源码符号化和栈帧导航工具。
- Extended `set_breakpoint` with condition, temporary, and ignore_count. / 为 `set_breakpoint` 增加条件、临时和忽略计数选项。
- Added `reconstruct_fault_context` to unwind the stacked exception frame and recover the faulting PC's source line. / 新增 `reconstruct_fault_context`，展开异常压栈帧并还原出错 PC 的源码行。
- Added memory-write guardrails and audit log (`set_write_policy`, `get_write_audit_log`). / 新增内存写入护栏和审计日志。
- Added `configure_debug_freeze` to freeze IWDG/WWDG/timers via DBGMCU while halted. / 新增 `configure_debug_freeze`，halt 时通过 DBGMCU 冻结看门狗/定时器。
- Added `check_session_health` with optional reconnect for long autonomous runs. / 新增 `check_session_health` 及可选重连。
- Added Tier 3 depth tools: execution control (`step_out`, `step_instruction`, `run_to_line`), `disassemble`, symbol/type discovery, coredump capture/load, `verify_flash`, and DWT timing/PC sampling. / 新增第三梯队深度工具:执行控制、反汇编、符号/类型发现、coredump、flash 校验、DWT 计时与 PC 采样。

### Earlier in Unreleased / 早前未发布内容

- Added probe-specific reset strategy profiles and YAML reset config. / 新增面向不同调试器的复位策略 profile 和 YAML reset 配置。
- Migrated MCP tool responses to the stable JSON envelope. / 将 MCP 工具响应迁移到稳定 JSON 包络。
- Added SWO/ITM process-output log capture tools. / 新增 SWO/ITM 进程输出日志采集工具。
- Added skipped-by-default HIL smoke regression tests and STM32L431 OpenOCD config. / 新增默认跳过的 HIL 烟测回归测试和 STM32L431 OpenOCD 配置。
- Added a minimal STM32L431 example firmware project. / 新增最小 STM32L431 示例固件工程。

## 0.2.0 - 2026-06-21

- Added Cortex-M fault diagnosis and structured debug snapshots. / 新增 Cortex-M 故障诊断和结构化调试快照。
- Added SVD register bitfield decoding. / 新增 SVD 寄存器位域解码。
- Added FreeRTOS task, list, queue, mutex, and heap inspection. / 新增 FreeRTOS 任务、链表、队列、互斥量和堆检查。
- Added SEGGER RTT and UART log capture. / 新增 SEGGER RTT 和 UART 日志采集。
- Added automated debug experiment tools. / 新增自动化调试实验工具。
- Added YAML debug config load/save/validation. / 新增 YAML 调试配置加载、保存和校验。
- Added CI, examples, and repository hygiene files. / 新增 CI、示例和仓库维护文件。

## 0.1.0 - 2026-06-21

- Initial STM32 GDB MCP server prototype. / 初始 STM32 GDB MCP 服务器原型。
- Added debug server startup, GDB connection, flashing, reset, breakpoints, stepping, memory, variables, call stack, watchpoints, and basic SVD access. / 新增调试服务器启动、GDB 连接、烧录、复位、断点、单步、内存、变量、调用栈、观察点和基础 SVD 访问。
