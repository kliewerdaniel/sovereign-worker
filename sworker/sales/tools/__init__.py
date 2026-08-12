"""Sales tools — the ONLY way a worker touches sales data.

Every tool is an ordinary ``sworker.tools.base.Tool`` subclass with a declared
``risk``, so the existing ``PermissionEngine`` + five-tier worker ``policy``
governs it with no new approval mechanism. A worker reaches the DailySalesOS
ledger only through ``ctx.resolve`` — a worker whose ``fs_roots`` excludes the
ledger physically cannot touch it.
"""

from __future__ import annotations

from .base import SALES_TOOLS

TOOLS = SALES_TOOLS

__all__ = ["TOOLS", "SALES_TOOLS"]
