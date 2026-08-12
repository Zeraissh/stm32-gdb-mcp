# Hardware Verification Report — stm32l151_telemetry

| Field | Value |
|---|---|
| **Date** | 2026-08-03 |
| **Device** | STM32L151/152 (Cat.3), dev_id = 0x427, CPUID = 0x412fc230 (Cortex-M3) |
| **ELF** | `build/stm32l151_telemetry.elf` |
| **Toolchain** | GCC 14.3.1 20250623 (GNU Tools for STM32) |
| **Debug Backend** | OpenOCD + ST-Link (stlink.cfg / stm32l1.cfg, 4000 kHz) |

---

## Verification Items

### (a) uptime_ms 计时精度

| 测量点 | 值 |
|---|---|
| 初始 uptime_ms | 154232 ms |
| run_for_duration elapsed_sec | 3.315 s |
| 运行后 uptime_ms | 157527 ms |
| 实测增量 | 3295 ms |
| 预期范围 (3315 ± 10%) | 2984 ~ 3647 ms |

**PASS** ✓ — 3295 ms 在允许范围内。

---

### (b) g_telemetry 常量校验

| 字段 | 实测值 | 预期值 | 判定 |
|---|---|---|---|
| `hclk_hz` | 16000000 | 16000000 | **PASS** ✓ |
| `magic` | 1413827661 (0x54454C4D) | 0x54454C4D | **PASS** ✓ |

---

### (c) reset_target 后 boot_count 严格 +1 & uptime_ms 归零

> **Firmware rev:** boot-count-fix, 2026-08-03.
> g_boot_count 语义：每次复位在 main() 中严格递增 1（.noinit 节，跨复位保持）。
> g_telemetry.uptime_ms 在 main() 中初始化为 0，复位后从零重计。

| 测量点 | g_boot_count | Δ | 判定 |
|---|---|---|---|
| B0 (首次完整启动后 halt) | 2 | — | — |
| B1 (第 1 次 reset_target 后) | 3 | +1 | **PASS** ✓ |
| B2 (第 2 次 reset_target 后) | 4 | +1 | **PASS** ✓ |

| 测量点 | g_telemetry.uptime_ms | 判定 |
|---|---|---|
| 复位后 run 0.8s 后 halt | 810 | < 2000 → **PASS** ✓ |

> g_boot_count 严格遵循 B1 == B0+1, B2 == B1+1，每次复位增量为精确 1。

**PASS** ✓ — boot_count 严格 +1，uptime_ms 归零重计。

---

### (d) g_fault_request = 1 → HardFault（DIVBYZERO）

| 检查项 | 实测值 | 预期 | 判定 |
|---|---|---|---|
| 停止位置 | `HardFault_Handler` @ main.c:152 | HardFault_Handler | **PASS** ✓ |
| `g_fault_latch.magic` | 1179407432 (0x464C5448) | 0x464C5448 | **PASS** ✓ |
| `g_fault_latch.cfsr` | 33554432 (0x02000000) | bit25 (DIVBYZERO) set | **PASS** ✓ |
| `g_fault_latch.hfsr` | 1073741824 (0x40000000) | FORCED | **PASS** ✓ |
| `g_fault_latch.stacked_pc` | 134218038 (0x08000136) | Flash [0x08000000, 0x08040000] | **PASS** ✓ |
| `g_fault_latch.stacked_lr` | 134217977 (0x080000f9) | — | OK |
| Fault 诊断 | UsageFault(DIVBYZERO) → HardFault(FORCED) | DIVBYZERO escalation | **PASS** ✓ |
| Fault 源码行 | main.c:192 (`1 / zero`) | 除零行 | **PASS** ✓ |

---

### (e) 二次复位后 g_fault_latch 保持 (.noinit) & boot_count 严格 +1

| 字段 | 值 (reset 前) | 值 (reset 后) | 判定 |
|---|---|---|---|
| `magic` | 1179407432 (0x464C5448) | 1179407432 (0x464C5448) | **PASS** ✓ |
| `cfsr` | 33554432 (0x02000000) | 33554432 (0x02000000) | **PASS** ✓ |
| `hfsr` | 1073741824 (0x40000000) | 1073741824 (0x40000000) | **PASS** ✓ |
| `stacked_pc` | 134218038 (0x08000136) | 134218038 (0x08000136) | **PASS** ✓ |
| `stacked_lr` | 134217977 (0x080000f9) | 134217977 (0x080000f9) | **PASS** ✓ |

> 二次复位后 g_boot_count = 7（复位前 boot_count 为 6，增量严格 +1）。

**PASS** ✓ — 全部五个 fault_latch 字段跨复位保持不变，.noinit 语义正确；boot_count 严格 +1。

---

## Summary

| 项目 | 结果 |
|---|---|
| (a) uptime 精度 | **PASS** |
| (b) 常量校验 | **PASS** |
| (c) Boot 计数严格 +1 & uptime 归零 | **PASS** |
| (d) HardFault 注入 | **PASS** |
| (e) .noinit 持久性 & boot_count 严格 +1 | **PASS** |

**Overall: 5/5 PASS** — 固件 (boot-count-fix, 2026-08-03) 在 STM32L151RC 真机上行为完全符合预期。
