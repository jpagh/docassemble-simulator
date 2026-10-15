"""Environment policy applied before loading the target document runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping


def target_interpreter_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Disable ORT telemetry before import, even if the caller enabled it.

    The post-import disable_telemetry_events() API still constructs the native
    telemetry machinery implicated in the macOS exit race. Force the pre-import
    switch instead; a caller's opt-in cannot safely override this mitigation.
    """
    return {
        **(os.environ if environment is None else environment),
        "ORT_DISABLE_TELEMETRY": "1",
    }
