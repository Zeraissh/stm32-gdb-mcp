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
    steps = []
    started = False

    async def invoke(name, arguments):
        response = await call_tool(name, arguments)
        payload = response if isinstance(response, dict) else response.structuredContent
        steps.append({"tool": name, "ok": bool(payload.get("ok"))})
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
        if hil.get("halt", True):
            await invoke("continue_execution", {})
        return {
            "ok": bool(identity["ok"]),
            "server": server,
            "identity": identity,
            "expected_family": expected_family,
            "steps": steps,
        }
    finally:
        if started:
            await invoke("stop_debug_session", {})
