# HANDOFF-PROBE-LOCK.md

**交付物**: 跨进程探针锁 (`probe_lock.py` + `gdb_manager.py` 集成 + `error_taxonomy.py` probe_locked 分类 + `tests/test_probe_lock.py`)

**复核日期**: 2026-08-05

**复核结论**: ✅ 全部通过 — 四道质量门禁零错误，五项实现要点与源码一致，无需修改代码。

---

## 1. 质量门禁

| # | 命令 | 退出码 | 关键输出 |
|---|------|--------|----------|
| 1 | `python -m pytest -q` | **0** | `916 passed, 1 skipped in 8.54s` |
| 2 | `python -m ruff check .` | **0** | `All checks passed!` |
| 3 | `python -m mypy` | **0** | `Success: no issues found in 86 source files` |
| 4 | `python -m compileall src tests` | **0** | 全部 Listing 完成，无 SyntaxError |

AC1 ✓ — pytest 退出码 0，passed=916 ≥ 916。
AC2 ✓ — ruff 退出码 0。
AC3 ✓ — mypy 退出码 0。
AC4 ✓ — compileall 退出码 0。

（1 skipped 为 `hil` 标记的硬件在环测试，基线如此，非回归。）

---

## 2. 实现要点核对清单

| # | 要点 | 源码位置 | 状态 |
|---|------|----------|------|
| 1 | 锁键三级派生: serial > interface cfg > server_type | `probe_lock.py:derive_probe_key()` L44-60 | ✅ 匹配 |
| 2 | 占用错误消息含 `"held by PID"` | `probe_lock.py:ProbeLockError.__init__()` L145-153 | ✅ 匹配 |
| 3 | `probe_locked` 分类 `retryable=false` | `error_taxonomy.py:_RULES` 第一条 L41-50 | ✅ 匹配 |
| 4 | adopted 路径不取锁 | `gdb_manager.py:start()` L85-90 在 `probe_lock_manager.acquire()` 之前 return | ✅ 匹配 |
| 5 | start() 失败路径释放锁 | `gdb_manager.py:start()` L129-134 except 块调用 `probe_lock_manager.release()` | ✅ 匹配 |
| 6 | stop() 释放锁 | `gdb_manager.py:stop()` L221-223 调用 `probe_lock_manager.release()` | ✅ 匹配 |

AC5 ✓ — 本报告存在，含四道门禁各自的真实退出码与 pytest 通过数。
AC6 ✓ — 报告中六项核对点均指向具体源码行号，且与源码实际一致。

---

## 3. 测试覆盖 (相关测试函数)

`tests/test_probe_lock.py` 共含以下场景，全部通过：

- `test_acquire_release_reacquire` — 获取→释放→再获取生命周期
- `test_live_pid_conflict_with_real_child` — 活进程占用冲突 + "held by PID" 消息
- `test_both_dead_stale_lock_cleared` — 双死进程 → 过期锁清理 → 获取成功
- `test_locker_dead_child_alive_still_occupied` — locker 死 / child 活 → 仍被占用
- `test_same_process_double_acquire_conflict` — 同进程重复获取冲突
- `test_derive_key_serial_priority` — serial 优先
- `test_derive_key_interface_cfg_fallback` — interface cfg 回退
- `test_derive_key_server_type_fallback` — server_type 兜底
- `test_derive_key_no_args` — 无参场景
- `test_start_failure_releases_lock` — start() 失败释放锁
- `test_error_message_contains_held_by_pid_exact_substring` — 消息精确子串
- `test_probe_locked_classification_retryable_false` — 分类 retryable=false
- `test_probe_locked_matches_lowercase_variation` — 大小写不敏感匹配
- `test_stop_releases_lock` — stop() 释放锁
- `test_adopted_path_does_not_take_lock` — adopted 不取锁
