from pathlib import Path

IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
FILE_KINDS = {
    ".elf": "elf",
    ".axf": "elf",
    ".out": "elf",
    ".map": "map",
    ".ld": "linker_script",
    ".svd": "svd",
    ".ioc": "ioc",
}


def inspect_project(project_root: str | None = None, profile: dict | None = None) -> dict:
    profile = profile or {}
    root = Path(project_root).resolve() if project_root else None
    files: dict[str, list[str]] = {kind: [] for kind in ("elf", "map", "linker_script", "svd", "ioc")}

    if root:
        for path in _iter_project_files(root):
            kind = FILE_KINDS.get(path.suffix.lower())
            if kind:
                files[kind].append(str(path))

    _add_profile_path(files, "elf", profile.get("elf_path"))
    _add_profile_path(files, "svd", profile.get("svd_path"))

    metadata = {}
    for ioc_path in files["ioc"]:
        metadata.update(_parse_ioc(Path(ioc_path)))

    result = {
        "project_root": str(root) if root else None,
        "mcu": profile.get("mcu") or metadata.get("mcu"),
        "project_name": metadata.get("project_name"),
        "toolchain": metadata.get("toolchain"),
        "package": metadata.get("package"),
        "files": {kind: sorted(paths) for kind, paths in files.items()},
    }
    return {key: value for key, value in result.items() if value is not None}


def _iter_project_files(root: Path):
    for path in root.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if path.is_file():
            yield path.resolve()


def _add_profile_path(files: dict, kind: str, path: str | None):
    if not path:
        return
    resolved = str(Path(path).resolve())
    if resolved not in files[kind]:
        files[kind].append(resolved)


def _parse_ioc(path: Path) -> dict:
    metadata: dict[str, str] = {}
    keys = {
        "Mcu.Name": "mcu",
        "Mcu.Package": "package",
        "ProjectManager.ProjectName": "project_name",
        "ProjectManager.TargetToolchain": "toolchain",
    }
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return metadata

    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in keys:
            metadata[keys[key]] = value.strip()
    return metadata
