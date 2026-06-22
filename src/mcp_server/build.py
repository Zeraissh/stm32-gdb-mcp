"""Build firmware via Keil µVision, CMake, make, or a custom command.

Closes the loop from "fix the code" to "rebuild → flash → debug" without leaving
the agent. The command construction is pure and unit-tested; the actual build runs
locally via subprocess. Keil output (`.axf`) is ELF/DWARF, so it debugs through the
existing GDB tools exactly like a GCC `.elf`.
"""

import os
import subprocess

# Common Keil µVision install locations for the UV4 command-line builder.
KEIL_DEFAULT_PATHS = [
    r"C:\Keil_v5\UV4\UV4.exe",
    r"C:\Keil\UV4\UV4.exe",
    r"C:\Keil_v5\ARM\UV4\UV4.exe",
]


def find_uv4():
    for path in KEIL_DEFAULT_PATHS:
        if os.path.isfile(path):
            return path
    return None


def resolve_build_command(kind, project=None, build_dir=None, directory=None, target=None,
                          config=None, rebuild=False, uv4_path=None, log_path=None, command=None):
    """Construct the build argv for a toolchain. Pure and unit-tested."""
    normalized = (kind or "").lower()

    if normalized == "keil":
        if not project:
            raise ValueError("keil build requires 'project' (a .uvprojx/.uvproj path)")
        cmd = [uv4_path or "UV4", "-r" if rebuild else "-b", project, "-j0"]
        if log_path:
            cmd += ["-o", log_path]
        return cmd

    if normalized == "cmake":
        if not build_dir:
            raise ValueError("cmake build requires 'build_dir'")
        cmd = ["cmake", "--build", build_dir]
        if target:
            cmd += ["--target", target]
        if config:
            cmd += ["--config", config]
        return cmd

    if normalized == "make":
        if not directory:
            raise ValueError("make build requires 'directory'")
        cmd = ["make", "-C", directory]
        if target:
            cmd.append(target)
        return cmd

    if normalized == "custom":
        if not command:
            raise ValueError("custom build requires 'command' (a list of argv strings)")
        return list(command)

    raise ValueError(f"Unsupported build kind: {kind!r}. Use keil, cmake, make, or custom.")


def is_build_success(kind, returncode) -> bool:
    """Keil UV4 returns 1 for warnings-only (still a usable build); others need 0."""
    if (kind or "").lower() == "keil":
        return returncode <= 1
    return returncode == 0


def run_build(argv, timeout=600, cwd=None, log_path=None) -> dict:
    """Run the build and capture output (preferring Keil's -o log file when present)."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    output = (proc.stdout or "") + (proc.stderr or "")
    if log_path and os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                file_output = f.read()
            if file_output.strip():
                output = file_output
        except OSError:
            pass
    return {"returncode": proc.returncode, "output": output}
