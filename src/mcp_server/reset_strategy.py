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

    resolved = {
        "server_type": normalized_server,
        "strategy": normalized_strategy,
        "command": strategies[normalized_strategy][bool(halt)],
    }
    # Say so when a strategy resolves to the same command as another: 'under_reset'
    # is currently an alias of 'default' for every backend, and silently doing
    # nothing different is exactly the false-success shape this server keeps
    # getting bitten by. Real connect-under-reset is a server_args/reset_config
    # concern at server start, not a runtime monitor command.
    # Only when the caller asked for something OTHER than the default and got the
    # default's command anyway — asking for "default" and getting it is no surprise.
    aliases = sorted(
        name for name, commands in strategies.items()
        if name != normalized_strategy and commands[bool(halt)] == resolved["command"]
    ) if normalized_strategy != DEFAULT_STRATEGY else []
    if aliases:
        resolved["note"] = (
            f"'{normalized_strategy}' resolves to the same command as {aliases} for "
            f"{normalized_server}. For true connect-under-reset, start the server with "
            "server_args containing -c \"reset_config srst_only srst_nogate connect_assert_srst\"."
        )
    return resolved
