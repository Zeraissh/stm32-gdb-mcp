# Release Checklist / 发布检查清单

Use this checklist when cutting a versioned release.

发布版本化 release 时使用此检查清单。

## Before Tagging / 打标签前

- Confirm `main` is green in CI. / 确认 `main` 分支 CI 通过。
- Run the local quality gate / 运行本地质量门禁：

```bash
python -m ruff check .
python -m pytest -q
python -m compileall src tests
python -m build
```

- Run hardware-in-the-loop validation for hardware-facing changes. / 对影响硬件行为的变更运行硬件在环验证。
- Run the skipped-by-default HIL smoke test when STM32 hardware is available. / 有 STM32 硬件时运行默认跳过的 HIL 烟测：

```powershell
$env:STM32_GDB_MCP_HIL = "1"
$env:STM32_GDB_MCP_HIL_CONFIG = "examples/configs/stm32l431_openocd.yaml"
python -m pytest -q tests/hil -m hil
```

- Optionally build the STM32L431 example firmware when Arm GCC and CMake are available. / 当 Arm GCC 和 CMake 可用时，可选构建 STM32L431 示例固件：

```bash
cmake -G Ninja -S examples/firmware/stm32l431_blinky -B build/stm32l431_blinky -DCMAKE_TOOLCHAIN_FILE=examples/firmware/stm32l431_blinky/cmake/arm-none-eabi.cmake
cmake --build build/stm32l431_blinky
```

- Update `pyproject.toml` version. / 更新 `pyproject.toml` 中的版本号。
- Update README examples when public behavior changed. / 当公开行为变化时更新 README 示例。
- Review new MCP tools for schema clarity and backward compatibility. / 检查新增 MCP 工具的 schema 清晰度和向后兼容性。
- Confirm generated distributions in `dist/` install in a clean environment. / 确认 `dist/` 中生成的包可以在干净环境安装。

## Tagging / 打标签

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

Pushing a `v*` tag triggers `.github/workflows/release.yml`, which runs the
quality gate, builds the sdist + wheel, and publishes them to PyPI. 推送 `v*`
标签会触发 `.github/workflows/release.yml`：跑质量门禁、构建 sdist + wheel 并发布到 PyPI。

## Publishing to PyPI / 发布到 PyPI

Publishing is automated via **PyPI Trusted Publishing (OIDC)** — no API token is
stored in the repo. 发布通过 **PyPI 可信发布 (OIDC)** 自动完成，仓库中不保存任何 API token。

**One-time setup / 一次性配置** (only needed before the first PyPI publish):

1. On PyPI, add a trusted publisher at
   <https://pypi.org/manage/account/publishing/> (use the "pending publisher"
   form if `stm32-gdb-mcp` does not exist on PyPI yet) with exactly:
   - **PyPI Project Name**: `stm32-gdb-mcp`
   - **Owner**: `Zeraissh`
   - **Repository name**: `stm32-gdb-mcp`
   - **Workflow name**: `release.yml`
   - **Environment name**: `pypi`
2. In the GitHub repo, create an Environment named `pypi`
   (Settings → Environments → New environment). Optionally add required
   reviewers to gate publishing behind a manual approval.
3. Ensure `pyproject.toml` `version` is the version you intend to publish, then
   tag and push (see **Tagging** above).

在 PyPI 的 <https://pypi.org/manage/account/publishing/> 添加可信发布者（若项目尚不存在则用
“pending publisher”表单），四项须与上面完全一致；在 GitHub 仓库 Settings → Environments 建一个
名为 `pypi` 的环境（可加必需审核人以在发布前人工确认）；确认 `pyproject.toml` 版本号无误后打标签推送即可。

**Manual dry run / 手动演练**: trigger the workflow via *Actions →
Release → Run workflow* (`workflow_dispatch`). It runs the quality gate and
build but **skips the publish step** (that step only runs for `v*` tag pushes),
so you can verify the pipeline without releasing. 通过 *Actions → Release → Run
workflow* 手动触发只会跑质量门禁与构建、**跳过发布步骤**（发布仅在 `v*` 标签推送时执行），可用于在不发布的情况下验证流水线。

**Existing tags / 已存在的标签**: a tag pushed before this workflow existed
(e.g. `v0.3.0`) will not have triggered it. To publish that version, either
re-push the tag (`git push origin :refs/tags/vX.Y.Z` then re-tag and push) or
publish it once manually with `python -m build && python -m twine upload dist/*`.
本工作流之前推送的标签（如 `v0.3.0`）不会自动触发；如需发布该版本，可删除并重推标签，或用
`python -m build && python -m twine upload dist/*` 手动发布一次。

## Release Notes / 发布说明

Include / 请包含：

- new MCP tools or response-shape changes / 新增 MCP 工具或响应结构变化
- supported probe or MCU workflow changes / 支持的调试器或 MCU 工作流变化
- compatibility notes / 兼容性说明
- test and HIL evidence / 测试和 HIL 证据
- known limits / 已知限制
