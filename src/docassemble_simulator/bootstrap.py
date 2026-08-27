"""Compatibility exports for the package-private simulator runtime.

The runtime implementation and its installation state live in
:mod:`docassemble_simulator._runtime`. The historical module remains as a
small import-compatible façade for callers that still use its names.
"""

from docassemble_simulator._runtime import (
    DEFAULT_CONFIG_TEXT,
    PDF_UNAVAILABLE_MESSAGE,
    FakeRedis,
    PDFConversionUnavailable,
    SimulatorTask,
    _configured_timezone,
    _foreground_background_action,
    _install_background_action_fallback,
    _without_pdf_conversion,
    apply_session_stubs,
    bootstrap,
    dyld_fallback_value,
    install_attachment_filename_fallback,
    install_diagnostic_logging,
    install_fake_redis,
    neutralize_argv,
    prepare_environment,
    register_hooks,
)

__all__ = [
    "DEFAULT_CONFIG_TEXT",
    "PDF_UNAVAILABLE_MESSAGE",
    "FakeRedis",
    "PDFConversionUnavailable",
    "SimulatorTask",
    "_configured_timezone",
    "_foreground_background_action",
    "_install_background_action_fallback",
    "_without_pdf_conversion",
    "apply_session_stubs",
    "bootstrap",
    "dyld_fallback_value",
    "install_attachment_filename_fallback",
    "install_diagnostic_logging",
    "install_fake_redis",
    "neutralize_argv",
    "prepare_environment",
    "register_hooks",
]
