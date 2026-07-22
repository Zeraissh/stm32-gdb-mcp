"""Domain-split MCP tool modules (schema + handler co-located via @register)."""

from .registry import REGISTRY, TOOL_ORDER, register


def load_all() -> None:
    """Import every domain module so their @register calls populate REGISTRY.

    Explicit imports, no directory scanning: the import list IS the roster.
    """
    from . import breakpoint_tools, inspect_tools  # noqa: F401


__all__ = ["REGISTRY", "TOOL_ORDER", "register", "load_all"]
