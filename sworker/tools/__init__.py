"""Built-in tool registry."""

from __future__ import annotations

from .base import Tool, ToolContext, ToolError, ToolRegistry, ToolResult, truncate  # noqa: F401
from . import browser, data, exec as exec_tools, fs, git, http, knowledge, message
from ..sales.tools import SALES_TOOLS


def build_registry() -> ToolRegistry:
    r = ToolRegistry()
    for module in (fs, data, exec_tools, http, git, browser, message, knowledge):
        for tool in module.TOOLS:
            r.register(tool)
    # Sales tools are registered on demand by workers that declare them by name
    # (or via the "sales.*" glob). They are excluded from the default registry so
    # a worker only gets sales access when it explicitly opts in. See
    # docs/SALES_INTEGRATION.md §2.
    for tool in SALES_TOOLS:
        if not r.has(tool.name):
            r.register(tool)
    return r


DEFAULT_REGISTRY = build_registry()

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "build_registry",
    "DEFAULT_REGISTRY",
    "truncate",
]
