"""Bound docassemble's namespace serializer so cyclic graphs cannot hang.

docassemble serializes the interview namespace when ``assemble`` raises and
``interview.debug`` is on (``parse.py``'s ``serializable_dict(user_dict)``),
and unconditionally for untrapped code-block errors (``exec_with_trap``).
Its ``safe_json`` caps depth at 20 but re-expands shared and cyclic
references on every path, so a dense object graph makes the traversal
exponential: three mutually-referencing DAObjects already spend about 25
seconds, and four never finish.  The simulator then waits inside
``interview.assemble`` and never reports the original unresolved-variable
error.

This guard wraps ``docassemble.base.functions.safe_json`` with path-based
cycle detection plus a per-serialization node budget.  Output is unchanged
for graphs the guard does not cut; truncated branches become ``None``
(``'None'`` for keys), matching docassemble's own depth cutoff.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

# Upstream's depth cutoff is 20.  The budget bounds total work even for wide
# graphs where cycle detection alone still leaves exponentially many simple
# paths.  It is large enough for ordinary interview namespaces and small
# enough that error reporting stays responsive.
_NODE_BUDGET = 5000

_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "docassemble_simulator_safe_json_state", default=None
)


def install_serialization_guard() -> None:
    """Wrap ``docassemble.base.functions.safe_json`` with a bounded walk.

    Idempotent per function object.  No-op when docassemble or its
    ``safe_json`` is unavailable, so stub runtimes keep working unchanged.
    """
    try:
        from docassemble.base import functions
    except ImportError:
        return
    original = getattr(functions, "safe_json", None)
    if not callable(original):
        return
    if getattr(original, "_dasimulator_bounded", False):
        return

    def bounded_safe_json(the_object: Any, level: int = 0, is_key: bool = False):
        state = _STATE.get()
        if state is None:
            # Outermost call: own the budget for this serialization.  Nested
            # calls (including ones that restart at level 0 from ``as_dict``
            # or ``to_json``) share it through the ContextVar.
            state = {"seen": set(), "count": 0}
            token = _STATE.set(state)
            try:
                return bounded_safe_json(the_object, level=level, is_key=is_key)
            finally:
                _STATE.reset(token)
        state["count"] += 1
        if state["count"] > _NODE_BUDGET:
            return "None" if is_key else None
        marker = id(the_object)
        if marker in state["seen"]:
            return "None" if is_key else None
        state["seen"].add(marker)
        try:
            return original(the_object, level=level, is_key=is_key)
        finally:
            state["seen"].discard(marker)

    bounded_safe_json._dasimulator_bounded = True
    bounded_safe_json._dasimulator_original = original
    functions.safe_json = bounded_safe_json


__all__ = ["install_serialization_guard"]
