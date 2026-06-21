# Maintenance and Quality Plan / 维护与质量计划

## Goal / 目标

English: Turn the STM32 GDB MCP repository into a maintainable private project
with repeatable quality gates, hardware validation guidance, and
contributor-facing project hygiene.

中文：将 STM32 GDB MCP 仓库落地为可维护的私有项目，具备可重复的质量门禁、硬件验证指引和面向贡献者的项目规范。

### Task 1: Add Quality Gate / 任务 1：增加质量门禁

- [x] Add Ruff as a development dependency. / 将 Ruff 加入开发依赖。
- [x] Configure lint rules in `pyproject.toml`. / 在 `pyproject.toml` 中配置 lint 规则。
- [x] Add lint to GitHub Actions CI. / 在 GitHub Actions CI 中加入 lint。
- [x] Fix lint findings. / 修复 lint 发现的问题。

### Task 2: Add Hardware Validation Path / 任务 2：增加硬件验证路径

- [x] Add a manual HIL GitHub Actions workflow for self-hosted STM32 runners. / 为自托管 STM32 runner 增加手动 HIL GitHub Actions 工作流。
- [x] Document HIL runner requirements, smoke coverage, and evidence capture. / 记录 HIL runner 要求、烟测覆盖和证据采集方式。

### Task 3: Add Maintenance Files / 任务 3：增加维护文件

- [x] Add issue templates. / 增加 issue 模板。
- [x] Add pull request template. / 增加 pull request 模板。
- [x] Add contribution and security guidance. / 增加贡献和安全指南。
- [x] Add release checklist. / 增加发布检查清单。

### Task 4: Verify and Publish / 任务 4：验证并发布

- [x] Run lint, unit tests, compile check, and package build. / 运行 lint、单元测试、编译检查和包构建。
- [x] Commit and push the completed project-maturity batch. / 提交并推送本轮项目成熟化变更。
