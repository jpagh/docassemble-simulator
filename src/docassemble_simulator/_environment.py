"""Environment policy applied before loading the target document runtime."""

from __future__ import annotations

import os
from collections.abc import Mapping


def target_interpreter_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Disable ORT telemetry before import, even if the caller enabled it.

    The pre-import switch prevents the 1DS SDK manager/uploader initialization,
    not the existence of ONNX Runtime's PosixTelemetry provider object. The
    post-import disable_telemetry_events() API is too late to prevent that SDK
    initialization and the macOS exit race. Force the switch instead; a caller's
    opt-in cannot safely override this mitigation.

    Callers bypassing CLI/runner launch seams (direct in-process bootstrap,
    custom test harnesses, or sitecustomize/.pth startup hooks) must set
    ORT_DISABLE_TELEMETRY=1 before any runtime bootstrap or onnxruntime import.
    """
    return {
        **(os.environ if environment is None else environment),
        "ORT_DISABLE_TELEMETRY": "1",
    }
