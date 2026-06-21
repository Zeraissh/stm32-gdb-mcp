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

## Release Notes / 发布说明

Include / 请包含：

- new MCP tools or response-shape changes / 新增 MCP 工具或响应结构变化
- supported probe or MCU workflow changes / 支持的调试器或 MCU 工作流变化
- compatibility notes / 兼容性说明
- test and HIL evidence / 测试和 HIL 证据
- known limits / 已知限制
