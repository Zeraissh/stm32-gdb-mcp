class DebugProfileStore:
    ALLOWED_FIELDS = {
        "mcu",
        "board",
        "probe",
        "server_type",
        "server_args",
        "elf_path",
        "svd_path",
        "project_root",
        "notes",
    }

    def __init__(self):
        self._profile = {}

    def update(self, values: dict) -> dict:
        unknown = sorted(set(values) - self.ALLOWED_FIELDS)
        if unknown:
            raise ValueError(f"Unknown debug profile field(s): {', '.join(unknown)}")

        for key, value in values.items():
            if value is not None:
                self._profile[key] = value
        return self.get()

    def get(self) -> dict:
        return dict(self._profile)
