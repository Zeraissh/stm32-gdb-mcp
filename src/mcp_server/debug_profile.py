class DebugProfileStore:
    ALLOWED_FIELDS = {
        "mcu",
        "board",
        "probe",
        "serial",
        "server_type",
        "server_args",
        "elf_path",
        "svd_path",
        "project_root",
        "rtt",
        "uart",
        "swo",
        "reset",
        "hil",
        "hub",
        "notes",
    }

    def __init__(self):
        self._profile = {}

    def update(self, values: dict, merge: bool = False) -> dict:
        """Apply ``values``. Top-level fields merge; a nested block REPLACES its stored twin.

        Replace is the default because it is the only way to REMOVE something, and
        a stale entry here is not cosmetic: ``HubManager.channel_for`` selects the
        first ``hub.map`` entry whose serial matches and the hub tools then cut
        VBUS on that channel, so a map that can only ever grow eventually
        power-cycles the wrong board. ``merge=True`` deep-merges dict-valued
        fields instead, for the caller that wants to add one key -- labelling a
        channel without discarding the identity ``hub(action=discover)`` wrote
        there, which for a serial-less probe is the only identity it has.
        """
        unknown = sorted(set(values) - self.ALLOWED_FIELDS)
        if unknown:
            raise ValueError(f"Unknown debug profile field(s): {', '.join(unknown)}")

        for key, value in values.items():
            if value is None:
                continue
            current = self._profile.get(key)
            if merge and isinstance(current, dict) and isinstance(value, dict):
                self._profile[key] = deep_merge(current, value)
            else:
                self._profile[key] = value
        return self.get()

    def get(self) -> dict:
        return dict(self._profile)


def deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively overlay ``overlay`` on ``base``; only dict values merge.

    Lists and scalars replace -- a list has no key to merge on, and a
    half-merged ``server_args`` would be a command line nobody wrote. Nothing is
    ever removed: ``None`` already means "field omitted" one level up, so there is
    no spelling for "delete this key" -- deleting is what the default replace is
    for.
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged
