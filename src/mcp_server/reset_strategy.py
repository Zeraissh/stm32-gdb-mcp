DEFAULT_STRATEGY = "default"

RESET_COMMANDS = {
    "openocd": {
        "default": {True: "monitor reset halt", False: "monitor reset run"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset run"},
        "software": {True: "monitor soft_reset_halt", False: "monitor reset run"},
    },
    "stlink": {
        "default": {True: "monitor reset halt", False: "monitor reset run"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset run"},
        "software": {True: "monitor reset halt", False: "monitor reset run"},
    },
    "jlink": {
        "default": {True: "monitor reset halt", False: "monitor reset go"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset go"},
        "software": {True: "monitor reset halt", False: "monitor reset go"},
    },
}


def resolve_reset_command(
    server_type: str | None,
    halt: bool,
    strategy: str | None = None,
    command: str | None = None,
) -> dict:
    if command and command.strip():
        return {
            "server_type": (server_type or "unknown").lower(),
            "strategy": "custom",
            "command": command.strip(),
        }

    normalized_server = (server_type or "openocd").lower()
    normalized_strategy = strategy or DEFAULT_STRATEGY
    strategies = RESET_COMMANDS.get(normalized_server)
    if not strategies or normalized_strategy not in strategies:
        raise ValueError(f"Unsupported reset strategy '{normalized_strategy}' for server type '{normalized_server}'")

    return {
        "server_type": normalized_server,
        "strategy": normalized_strategy,
        "command": strategies[normalized_strategy][bool(halt)],
    }
