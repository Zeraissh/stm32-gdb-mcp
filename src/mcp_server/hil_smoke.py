import asyncio


def run_hil_smoke(config: dict, call_tool=None) -> dict:
    return asyncio.run(_run_hil_smoke(config, call_tool))


async def _run_hil_smoke(config: dict, call_tool=None) -> dict:
    if call_tool is None:
        from .server import handle_call_tool

        call_tool = handle_call_tool

    expected_family = config.get("mcu")
    if not expected_family:
        raise ValueError("HIL config must define the expected MCU in 'mcu'.")

    hil = config.get("hil", {})
    steps: list[dict] = []
    started = False
    result = {
        "ok": False,
        "expected_family": expected_family,
        "expected_core": hil.get("expected_core"),
        "expected_device": hil.get("expected_device"),
        "steps": steps,
    }

    async def invoke(name, arguments):
        response = await call_tool(name, arguments)
        payload = response if isinstance(response, dict) else response.structuredContent
        step = {"tool": name, "ok": bool(payload.get("ok"))}
        if not payload.get("ok"):
            step["error"] = payload.get("error")
            step["raw_response"] = payload.get("raw_response")
        steps.append(step)
        if not payload.get("ok"):
            error = payload.get("error") or {}
            raise RuntimeError(f"{name} failed: {error.get('message', 'unknown MCP error')}")
        return payload.get("data")

    try:
        start_args = {
            "server_type": config["server_type"],
            "server_args": config.get("server_args", []),
        }
        if config.get("serial"):
            start_args["serial"] = config["serial"]
        server = await invoke("start_debug_session", start_args)
        started = True
        identity = await invoke(
            "self_check",
            {"expected_family": expected_family, "halt": hil.get("halt", True)},
        )
        hil_checks = [
            {"name": "identity", "ok": bool(identity.get("ok")), "actual": identity.get("device")},
        ]
        if hil.get("expected_core"):
            hil_checks.append({
                "name": "core",
                "ok": identity.get("core") == hil["expected_core"],
                "expected": hil["expected_core"],
                "actual": identity.get("core"),
            })
        if hil.get("expected_device"):
            actual_device = identity.get("device") or ""
            hil_checks.append({
                "name": "device",
                "ok": hil["expected_device"].lower() in actual_device.lower(),
                "expected": hil["expected_device"],
                "actual": actual_device,
            })
        if hil.get("halt", True):
            await invoke("continue_execution", {})
        result.update({
            "ok": all(check["ok"] for check in hil_checks),
            "server": server,
            "identity": identity,
            "hil_checks": hil_checks,
        })
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if started:
            try:
                await invoke("stop_debug_session", {})
            except Exception as exc:
                result["ok"] = False
                result["cleanup_error"] = str(exc)
    return result
