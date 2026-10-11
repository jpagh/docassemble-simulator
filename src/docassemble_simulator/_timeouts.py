"""Shared timeout boundary for worker operations and runtime adapters."""

from __future__ import annotations


class OperationTimeout(BaseException):
    """An operation exceeded its deadline and must not be converted to a flow error."""
