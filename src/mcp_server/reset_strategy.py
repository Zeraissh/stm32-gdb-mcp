DEFAULT_STRATEGY = "default"

# 'cold' is the one strategy whose difference is NOT in the command: it removes
# target power through a programmable USB hub and then issues the same reset the
# default would. What makes it cold is the power sequence, so it maps to the same
# command on every backend by design -- and is excluded from the alias note below,
# because calling it an alias would be actively misleading.
POWER_CYCLE_STRATEGY = "cold"

RESET_COMMANDS = {
    "openocd": {
        "default": {True: "monitor reset halt", False: "monitor reset run"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset run"},
        "software": {True: "monitor soft_reset_halt", False: "monitor reset run"},
        "cold": {True: "monitor reset halt", False: "monitor reset run"},
    },
    "stlink": {
        "default": {True: "monitor reset halt", False: "monitor reset run"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset run"},
        "software": {True: "monitor reset halt", False: "monitor reset run"},
        "cold": {True: "monitor reset halt", False: "monitor reset run"},
    },
    "jlink": {
        "default": {True: "monitor reset halt", False: "monitor reset go"},
        "under_reset": {True: "monitor reset halt", False: "monitor reset go"},
        "software": {True: "monitor reset halt", False: "monitor reset go"},
        "cold": {True: "monitor reset halt", False: "monitor reset go"},
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

    resolved: dict = {
        "server_type": normalized_server,
        "strategy": normalized_strategy,
        "command": strategies[normalized_strategy][bool(halt)],
    }
    if normalized_strategy == POWER_CYCLE_STRATEGY:
        # Not an alias: the caller gets a real power cycle before this command runs.
        # reset_target refuses rather than silently downgrading when no hub is
        # mapped, so this flag is the contract between the two.
        resolved["requires_power_cycle"] = True
        return resolved
    # Say so when a strategy resolves to the same command as another: 'under_reset'
    # is currently an alias of 'default' for every backend, and silently doing
    # nothing different is exactly the false-success shape this server keeps
    # getting bitten by. Real connect-under-reset is a server_args/reset_config
    # concern at server start, not a runtime monitor command.
    # Only when the caller asked for something OTHER than the default and got the
    # default's command anyway — asking for "default" and getting it is no surprise.
    aliases = sorted(
        name for name, commands in strategies.items()
        if name != normalized_strategy and name != POWER_CYCLE_STRATEGY
        and commands[bool(halt)] == resolved["command"]
    ) if normalized_strategy != DEFAULT_STRATEGY else []
    if aliases:
        resolved["note"] = (
            f"'{normalized_strategy}' resolves to the same command as {aliases} for "
            f"{normalized_server}. For true connect-under-reset, start the server with "
            "server_args containing -c \"reset_config srst_only srst_nogate connect_assert_srst\". "
            "For a real cold boot (power actually removed), use strategy=\"cold\" with a "
            "programmable USB hub configured."
        )
    # soft_reset_halt resets the core THROUGH THE DEBUG UNIT and never asserts
    # SYSRESETREQ, so the reset does not reach RCC_CSR and its sticky flag field
    # does not move. Invisible until you try to PROVE a reset happened -- and it
    # silently breaks the standard cold-boot proof, which is "a warm reset sets
    # SFTRST, then a real power cut clears it again". Measured on an STM32L151:
    # strategy="software" left RCC_CSR at 0x0C000000 while strategy="default" moved
    # it to 0x1C000000, so a cold reset compared against the software one looks
    # like it did nothing.
    if resolved["command"].endswith("soft_reset_halt"):
        soft_note = (
            "'software' issues soft_reset_halt, which resets the core through the debug unit "
            "WITHOUT asserting SYSRESETREQ -- so RCC_CSR does not record it and its sticky reset "
            "flags do not move. Use strategy=\"default\" when the reset has to be visible in "
            "RCC_CSR, e.g. when proving a cold boot by watching a sticky flag disappear."
        )
        resolved["note"] = f"{resolved['note']} {soft_note}" if resolved.get("note") else soft_note
    return resolved
