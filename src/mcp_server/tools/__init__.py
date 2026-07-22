"""Domain-split MCP tool modules (schema + handler co-located via @register)."""

from .registry import REGISTRY, TOOL_ORDER, register


def load_all() -> None:
    """Import every domain module so their @register calls populate REGISTRY.

    Explicit imports, no directory scanning: the import list IS the roster.
    """
    from . import (  # noqa: F401
        breakpoint_tools,
        config_tools,
        inspect_tools,
        logging_tools,
        peripheral_tools,
    )


__all__ = ["REGISTRY", "TOOL_ORDER", "register", "load_all"]
