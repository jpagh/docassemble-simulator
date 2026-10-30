"""Standard-library compatibility for the oldest supported Python (3.10)."""

from __future__ import annotations

import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum

    import tomllib
else:  # pragma: no cover - exercised by the CPython 3.10 lane
    from enum import Enum

    import tomli as tomllib

    class StrEnum(str, Enum):
        """Python 3.11's ``StrEnum`` for 3.10."""

        __str__ = str.__str__
        __format__ = str.__format__

        @staticmethod
        def _generate_next_value_(name, start, count, last_values):
            return name.lower()


__all__ = ["StrEnum", "tomllib"]
