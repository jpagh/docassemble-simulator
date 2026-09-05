"""Package-private simulator runtime installation and root activation.

This is the distilled, generalized version of the bootstrap that took real
effort to get right in the original interview-harness (see
docassemble-automatedpleading/var/interview-test-framework). The docassemble
library assumes a running webapp; these pieces are stubbed:

| Piece                     | Why                                        | What we do |
|---------------------------|--------------------------------------------|------------|
| docassemble.webapp.daredis| imported at webapp-import time; connects   | swap module into sys.modules with FakeRedis before any webapp import |
|                           | to redis immediately                       |            |
| get_configuration / voice / dialect / locale / timezone / country / | pluggy hooks; webapp defaults raise NotImplementedError | register webapp hook modules then a MinimalHooks plugin |
| hostname / main_page_parts|                                            |            |
| set_sessions_data /       | called by interview setup blocks           | no-op monkeypatch |
| set_sessions_title_stage /|                                            |            |
| cleanup_sessions          |                                            |            |
| DA_CONFIG_FILE            | interviews render Jinja against `jinja data` config | write a config file (sqlite + fake redis + jinja data) and point the env var at it |
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import sys
import types
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_TEXT = """\
db:
  database name: docassemble-simulator
  driver: sqlite
redis: "redis://localhost:6399"
debug: true
host: localhost
locale: en_US
country: US
"""

PDF_UNAVAILABLE_MESSAGE = (
    "PDF output is unavailable in the simulator: generated PDF conversion is "
    "not supported and no external converter is invoked; use a real "
    "docassemble deployment for PDF downloads."
)


def _file_metadata(reference, *, resolved_path: Path | None = None) -> dict:
    """Build the common docassemble file-reference metadata shape."""
    if resolved_path is None:
        value = str(reference)
        parsed = urlparse(value)
        filename = Path(parsed.path).name or "download"
        path = value
    else:
        filename = resolved_path.name
        path = str(resolved_path)
    extension = Path(filename).suffix.removeprefix(".")
    mimetype, _ = mimetypes.guess_type(filename)
    return {
        "path": path,
        "fullpath": path,
        "filename": filename,
        "extension": extension,
        "mimetype": mimetype,
    }


_BACKGROUND_ACTION_MODE = "foreground"
_BACKGROUND_INSTALLED = False
_DIAGNOSTIC_LOGGING_INSTALLED = False
_ACTIVE_ROOT: ContextVar[Path | None] = ContextVar(
    "docassemble_simulator_runtime_root", default=None
)
_ACTIVE_BACKGROUND_ACTION_MODE: ContextVar[str | None] = ContextVar(
    "docassemble_simulator_background_action_mode", default=None
)


class PDFConversionUnavailable(RuntimeError):
    """Raised instead of exposing a late missing-PDF file lookup."""


class SimulatorTask:
    """Small, pickleable task value for local foreground background actions."""

    def __init__(self, value=None, error: BaseException | None = None, *, ready=True):
        self._value = value
        self._error_type = type(error).__name__ if error is not None else None
        self._error_message = str(error) if error is not None else None
        self._ready = ready
        self.status = (
            "SUCCESS"
            if ready and error is None
            else ("FAILURE" if error else "PENDING")
        )
        self.state = self.status

    def ready(self):
        return self._ready

    def failed(self):
        return self._error_message is not None

    def wait(self):
        return self._ready

    def get(self, *args, **kwargs):
        if self._error_message is not None:
            raise RuntimeError(f"{self._error_type}: {self._error_message}")
        return self._value

    def result(self):
        return self._value

    def revoke(self, *args, **kwargs):
        return None

    def date_done(self):
        return None


class FakeRedis:
    """Minimal stand-in for the server redis connection."""

    def __init__(self, *args, **kwargs):
        pass

    def pipeline(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self):
        return []

    def get(self, *args, **kwargs):
        return None

    def set(self, *args, **kwargs):
        return True

    def delete(self, *args, **kwargs):
        return True

    def hget(self, *args, **kwargs):
        return None

    def hset(self, *args, **kwargs):
        return True

    def hincrby(self, *args, **kwargs):
        return 1

    def expire(self, *args, **kwargs):
        return True

    def ttl(self, *args, **kwargs):
        return -1

    def keys(self, *args, **kwargs):
        return []

    def scan_iter(self, *args, **kwargs):
        return iter([])

    def ping(self):
        return True

    def incr(self, *args, **kwargs):
        return 1

    def decr(self, *args, **kwargs):
        return 0

    def exists(self, *args, **kwargs):
        return 0

    def sadd(self, *args, **kwargs):
        return 1

    def srem(self, *args, **kwargs):
        return 1

    def smembers(self, *args, **kwargs):
        return set()

    def lpush(self, *args, **kwargs):
        return 1

    def rpush(self, *args, **kwargs):
        return 1

    def lrange(self, *args, **kwargs):
        return []

    def llen(self, *args, **kwargs):
        return 0


def _effective_config_text(extra_config: dict | None) -> str:
    text = DEFAULT_CONFIG_TEXT
    if extra_config is None:
        return text
    import yaml

    from docassemble_simulator.config import deep_merge, pass_through_config

    merged = yaml.safe_load(text) or {}
    deep_merge(merged, pass_through_config(extra_config))
    return yaml.safe_dump(merged, sort_keys=False)


def prepare_environment(
    *,
    config_path: Path | None = None,
    extra_config: dict | None = None,
) -> None:
    """Set native-library and config-file env vars before docassemble imports.

    Process-level native setup is one-time, while each call selects the active
    root's effective config and local file registry.
    """
    global _PREPARED
    if config_path is None:
        configured = os.environ.get("DA_CONFIG_FILE") if _PREPARED else None
        config_path = (
            Path(configured)
            if configured
            else Path.cwd() / ".simulator" / "config-effective.yml"
        )
    from docassemble_simulator._artifacts import activate_file_registry

    activate_file_registry(config_path.parent / "files")
    if _PREPARED:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if extra_config is not None:
            config_path.write_text(
                _effective_config_text(extra_config), encoding="utf-8"
            )
        elif (
            not config_path.exists()
            or not config_path.read_text(encoding="utf-8").strip()
        ):
            config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
        os.environ["DA_CONFIG_FILE"] = str(config_path)
        return
    _PREPARED = True

    if sys.platform == "darwin":
        _append_dyld_fallback()

    text = _effective_config_text(extra_config)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists() or extra_config is not None:
        config_path.write_text(text, encoding="utf-8")
    elif not config_path.read_text(encoding="utf-8").strip():
        config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")

    os.environ["DA_CONFIG_FILE"] = str(config_path)


# Homebrew installs native libs under /opt/homebrew on Apple Silicon and
# /usr/local on Intel; other prefixes only work when the user set the var
# themselves (we never override an existing DYLD_FALLBACK_LIBRARY_PATH).
_DYLD_CANDIDATES = ("/opt/homebrew/lib", "/usr/local/lib")


def dyld_fallback_value() -> str:
    """Existing user paths plus our candidates, deduplicated, in order."""
    existing = [
        p for p in os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "").split(":") if p
    ]
    seen = dict.fromkeys(existing + list(_DYLD_CANDIDATES))
    return ":".join(seen)


def _append_dyld_fallback() -> None:
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = dyld_fallback_value()


def install_fake_redis() -> None:
    """Swap docassemble.webapp.daredis before any webapp module is imported."""
    fake = types.ModuleType("docassemble.webapp.daredis")
    fake.r = FakeRedis()
    fake.r_user = FakeRedis()
    fake.get_redis_connection = lambda *a, **k: FakeRedis()
    sys.modules["docassemble.webapp.daredis"] = fake


def _configured_timezone() -> str:
    try:
        import docassemble.base.config as da_config

        configured = getattr(da_config, "daconfig", {}).get("timezone")
        if configured:
            return str(configured)
    except (
        ImportError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        RuntimeError,
        OSError,
    ) as exc:
        logger.debug("configured timezone not available: %s", exc)
    try:
        from tzlocal import get_localzone_name

        return get_localzone_name()
    except (
        ImportError,
        ValueError,
        OSError,
        RuntimeError,
        AttributeError,
        LookupError,
    ) as exc:
        logger.debug("tzlocal lookup failed, using default: %s", exc)
        return "America/New_York"


_ATTACHMENT_FALLBACK_INSTALLED = False


def _without_pdf_conversion(result: dict) -> None:
    """Remove generated PDF formats before docassemble dispatches converters."""
    formats = result.get("formats_to_use")
    if isinstance(formats, (list, tuple)):
        result["formats_to_use"] = [item for item in formats if item != "pdf"]
    valid_formats = result.get("valid_formats")
    if isinstance(valid_formats, (list, tuple)):
        result["valid_formats"] = [item for item in valid_formats if item != "pdf"]


def install_attachment_filename_fallback() -> None:
    """Stub PDF conversion and give nameless attachments a safe filename.

    A few generic attachment blocks leave the compiled ``filename`` as None
    even though their rendered attachment name is valid.  The docassemble
    server normally gets a filename from the attachment option; without one,
    its save path concatenation raises late, after a successful DOCX render.
    Keep the server behavior for named files and only supply a fallback for
    this invalid ``None`` case. PDF conversion is handled separately by
    removing generated ``pdf`` formats before the real finalizer runs.
    """
    global _ATTACHMENT_FALLBACK_INSTALLED
    if _ATTACHMENT_FALLBACK_INSTALLED:
        return
    try:
        from docassemble.base.parse import Question
    except ImportError:
        return

    original = getattr(Question, "finalize_attachment", None)
    if original is None:
        return
    if getattr(original, "_dasimulator_filename_fallback", False):
        _ATTACHMENT_FALLBACK_INSTALLED = True
        return

    def finalize_with_filename(self, attachment, result, user_dict):
        had_pdf = "pdf" in (result.get("formats_to_use") or ()) or "pdf" in (
            result.get("valid_formats") or ()
        )
        _without_pdf_conversion(result)
        if (
            had_pdf
            and not result.get("formats_to_use")
            and not result.get("valid_formats")
        ):
            raise PDFConversionUnavailable(PDF_UNAVAILABLE_MESSAGE)
        if result.get("filename") is None:
            name = result.get("name") or "attachment"
            filename = re.sub(r"[^\w.-]+", "_", str(name), flags=re.UNICODE).strip("._")
            result["filename"] = filename or "attachment"
        return original(self, attachment, result, user_dict)

    finalize_with_filename._dasimulator_filename_fallback = True
    Question.finalize_attachment = finalize_with_filename
    _ATTACHMENT_FALLBACK_INSTALLED = True


def install_diagnostic_logging() -> None:
    """Route expected lazy-seek logs through operation diagnostics, not stderr."""
    global _DIAGNOSTIC_LOGGING_INSTALLED
    if _DIAGNOSTIC_LOGGING_INSTALLED:
        return
    try:
        from docassemble.base import logger as da_logger
    except ImportError:
        return
    previous = da_logger.the_logmessage
    if getattr(previous, "_dasimulator_diagnostic_dispatch", False):
        _DIAGNOSTIC_LOGGING_INSTALLED = True
        return

    def dispatch(message):
        from docassemble_simulator._diagnostics import (
            is_collecting,
            is_lazy_seek_log,
        )

        if is_collecting() and is_lazy_seek_log(message):
            return None
        return previous(message)

    dispatch._dasimulator_diagnostic_dispatch = True
    da_logger.set_logmessage(dispatch)
    _DIAGNOSTIC_LOGGING_INSTALLED = True


def _authored_file_path(
    file_reference,
    *,
    question=None,
    folder=None,
    package=None,
):
    """Resolve a package data reference inside the active simulator root."""
    if not isinstance(file_reference, str):
        return None
    reference = file_reference.strip()
    if ":" in reference:
        package, reference = reference.split(":", 1)
    if package is None and question is not None:
        package = getattr(question, "package", None)
    if package is None:
        from docassemble.base.functions import this_thread

        package = getattr(this_thread, "current_package", None)
    if not package:
        package = "docassemble.base"
    package_path = str(package).removeprefix("docassemble.").replace(".", "/")
    relative = reference.lstrip("/")
    if not relative.startswith("data/"):
        relative = f"data/{folder or 'static'}/{relative}"
    relative_path = Path(relative)
    if any(part in {"", ".", ".."} for part in relative_path.parts):
        return None
    root = _ACTIVE_ROOT.get()
    if root is None:
        return None
    # The corpus runner uses package symlinks in per-case workspaces, so use
    # the validated lexical path here rather than comparing resolved paths.
    candidate = root / "docassemble" / package_path / relative_path
    return candidate if candidate.is_file() else None


class _SimulatorRuntimeBindings:
    """Shared simulator behavior behind the docassemble runtime adapters."""

    def get_ext_and_mimetype(self, filename):
        metadata = _file_metadata(filename)
        return metadata["extension"].lower() or None, metadata["mimetype"]

    def get_default_voice(self):
        return ""

    def get_default_dialect(self):
        return ""

    def get_default_language(self):
        return "en"

    def get_default_locale(self):
        return "en_US"

    def get_default_timezone(self):
        return _configured_timezone()

    def get_default_country(self):
        return "US"

    def get_hostname(self):
        return "localhost"

    def get_debug_status(self):
        return True

    def get_main_page_parts(self):
        return {}

    def get_button_class_prefix(self):
        return "btn"

    def save_numbered_file(self, filename, orig_path, yaml_file_name=None, uid=None):
        from docassemble_simulator._artifacts import active_file_registry

        registry = active_file_registry()
        if registry is None:
            raise RuntimeError("simulator local file registry is not active")
        return registry.save(filename, orig_path)

    def file_finder(
        self,
        file_reference,
        question=None,
        folder=None,
        package=None,
        filename=None,
        return_nonexistent=False,
        uids=None,
    ):
        if isinstance(file_reference, str) and file_reference.startswith(
            ("http://", "https://")
        ):
            return _file_metadata(file_reference)
        path = _authored_file_path(
            file_reference, question=question, folder=folder, package=package
        )
        if path is None and return_nonexistent and isinstance(file_reference, str):
            # Preserve the standard hook's missing-file semantics while
            # keeping path construction confined to the active workspace.
            return None
        if path is None:
            return None
        return _file_metadata(path, resolved_path=path)

    def file_number_finder(
        self, file_number, filename=None, uids=None, privileged=False
    ):
        from docassemble_simulator._artifacts import active_file_registry

        registry = active_file_registry()
        return None if registry is None else registry.find(file_number, filename)

    def url_finder(self, file_reference, kwargs=None):
        from docassemble_simulator._artifacts import active_file_registry

        options = dict(kwargs or {})
        question = options.get("question", options.get("_question"))
        package = options.get("package", options.get("_package"))
        registry = active_file_registry()
        if registry is not None:
            url = registry.url_for(file_reference)
            if url is not None:
                return url
        path = _authored_file_path(file_reference, question=question, package=package)
        return None if path is None else path.resolve().as_uri()

    def get_configuration(self):
        # Return the live server config (not a stub): interviews read
        # `jinja data` and package settings through this hook.
        import docassemble.base.config as da_config_mod

        cfg = dict(getattr(da_config_mod, "daconfig", {}) or {})
        cfg.setdefault("debug", True)
        cfg.setdefault("host", "localhost")
        cfg.setdefault("locale", "en_US")
        cfg.setdefault("country", "US")
        return cfg


def _install_relationship_methods() -> None:
    from docassemble.base.util import Individual

    # These convenience relationship methods are present in the documented
    # interview API but are commented out in some installed base packages.
    if not hasattr(Individual, "get_spouse"):
        Individual.get_spouse = lambda self, tree, create=False: self.get_peer_relation(
            "spouse", tree, create=create
        )
    if not hasattr(Individual, "set_spouse"):
        Individual.set_spouse = lambda self, target, tree: self.set_peer_relationship(
            target, "spouse", tree, replace=True
        )
    if not hasattr(Individual, "is_spouse_of"):
        Individual.is_spouse_of = lambda self, target, tree: self.is_peer_relation(
            target, "spouse", tree
        )


def _register_pluggy_runtime_bindings(bindings: _SimulatorRuntimeBindings) -> None:
    """Register simulator bindings through the docassemble 1.10 hook API."""
    from docassemble.base.plugin_manager import pm
    from docassemble.webapp import main as webapp_main
    from docassemble.webapp.interview import hooks as interview_hooks
    from docassemble.webapp.main import hooks as main_hooks

    # These are normally auto-registered when docassemble.webapp imports;
    # only add them if somehow missing.
    for module, name in (
        (webapp_main, "docassemble.webapp.main"),
        (main_hooks, "docassemble.webapp.main.hooks"),
        (interview_hooks, "docassemble.webapp.interview.hooks"),
    ):
        if pm.get_plugin(name) is None:
            pm.register(module, name=name)

    import pluggy

    hookimpl = pluggy.HookimplMarker("docassemble")

    class MinimalHooks:
        # pluggy >=1.0 requires the project's HookimplMarker on every method;
        # unmarked methods are silently ignored.
        @hookimpl(tryfirst=True)
        def get_ext_and_mimetype(self, filename):
            return bindings.get_ext_and_mimetype(filename)

        @hookimpl
        def get_default_voice(self):
            return bindings.get_default_voice()

        @hookimpl
        def get_default_dialect(self):
            return bindings.get_default_dialect()

        @hookimpl
        def get_default_language(self):
            return bindings.get_default_language()

        @hookimpl
        def get_default_locale(self):
            return bindings.get_default_locale()

        @hookimpl
        def get_default_timezone(self):
            return bindings.get_default_timezone()

        @hookimpl
        def get_default_country(self):
            return bindings.get_default_country()

        @hookimpl
        def get_hostname(self):
            return bindings.get_hostname()

        @hookimpl
        def get_debug_status(self):
            return bindings.get_debug_status()

        @hookimpl
        def get_main_page_parts(self):
            return bindings.get_main_page_parts()

        @hookimpl
        def get_button_class_prefix(self):
            return bindings.get_button_class_prefix()

        @hookimpl
        def save_numbered_file(
            self, filename, orig_path, yaml_file_name=None, uid=None
        ):
            return bindings.save_numbered_file(
                filename, orig_path, yaml_file_name=yaml_file_name, uid=uid
            )

        @hookimpl(tryfirst=True)
        def file_finder(
            self,
            file_reference,
            question=None,
            folder=None,
            package=None,
            filename=None,
            return_nonexistent=False,
            uids=None,
        ):
            return bindings.file_finder(
                file_reference,
                question=question,
                folder=folder,
                package=package,
                filename=filename,
                return_nonexistent=return_nonexistent,
                uids=uids,
            )

        @hookimpl(tryfirst=True)
        def file_number_finder(
            self, file_number, filename=None, uids=None, privileged=False
        ):
            return bindings.file_number_finder(
                file_number, filename=filename, uids=uids, privileged=privileged
            )

        @hookimpl(tryfirst=True)
        def url_finder(self, file_reference, kwargs):
            return bindings.url_finder(file_reference, kwargs)

        @hookimpl
        def get_configuration(self):
            return bindings.get_configuration()

    if pm.get_plugin("dasimulator.minimal") is None:
        pm.register(MinimalHooks(), name="dasimulator.minimal")


def _register_legacy_runtime_bindings(bindings: _SimulatorRuntimeBindings) -> None:
    """Install simulator bindings on docassemble 1.9's server object."""
    from docassemble.base import functions

    server = functions.server
    server.get_ext_and_mimetype = bindings.get_ext_and_mimetype
    server.get_default_voice = bindings.get_default_voice
    server.get_default_dialect = bindings.get_default_dialect
    server.get_default_language = bindings.get_default_language
    server.get_default_locale = bindings.get_default_locale
    server.get_default_timezone = bindings.get_default_timezone
    server.get_default_country = bindings.get_default_country
    server.default_voice = bindings.get_default_voice()
    server.default_dialect = bindings.get_default_dialect()
    server.default_language = bindings.get_default_language()
    server.default_locale = bindings.get_default_locale()
    server.default_timezone = bindings.get_default_timezone()
    server.default_country = bindings.get_default_country()
    server.hostname = bindings.get_hostname()
    server.debug = bindings.get_debug_status()
    server.debug_status = bindings.get_debug_status()
    server.main_page_parts = bindings.get_main_page_parts()
    server.button_class_prefix = bindings.get_button_class_prefix()
    server.daconfig = bindings.get_configuration()

    def file_finder(
        file_reference,
        question=None,
        folder=None,
        package=None,
        filename=None,
        return_nonexistent=False,
        uids=None,
        **kwargs,
    ):
        question = kwargs.pop("_question", question)
        package = kwargs.pop("_package", package)
        return bindings.file_finder(
            file_reference,
            question=question,
            folder=folder,
            package=package,
            filename=filename,
            return_nonexistent=return_nonexistent,
            uids=uids,
        )

    def file_number_finder(
        file_number, filename=None, uids=None, privileged=False, **kwargs
    ):
        return bindings.file_number_finder(
            file_number,
            filename=filename,
            uids=uids,
            privileged=privileged,
        )

    def url_finder(file_reference, options=None, **kwargs):
        normalized = dict(options or {})
        normalized.update(kwargs)
        return bindings.url_finder(file_reference, normalized)

    server.file_finder = file_finder
    server.file_number_finder = file_number_finder
    server.url_finder = url_finder
    server.save_numbered_file = bindings.save_numbered_file


def register_hooks() -> None:
    """Install simulator hooks through either supported docassemble interface."""
    _install_relationship_methods()
    bindings = _SimulatorRuntimeBindings()
    try:
        from docassemble.base.plugin_manager import pm  # noqa: F401
    except ModuleNotFoundError as error:
        if error.name != "docassemble.base.plugin_manager":
            raise
        _register_legacy_runtime_bindings(bindings)
    else:
        _register_pluggy_runtime_bindings(bindings)


_STUBBED = False
_PREPARED = False


def _current_dict():
    """The thread-context user_dict docassemble evaluation expects, or None."""
    from docassemble.base.functions import this_thread

    return getattr(this_thread, "current_dict", None)


def _fake_define(name, value):
    """DB-free define(): write into the thread-context user_dict when one is active."""
    current = _current_dict()
    if current is not None:
        current[name] = value


def _fake_defined(name) -> bool:
    """DB-free defined(): evaluate the name against the thread-context user_dict."""
    current = _current_dict()
    if current is None:
        return False
    try:
        eval(name, current)
    except (
        NameError,
        AttributeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        SyntaxError,
        RuntimeError,
        ImportError,
        LookupError,
        OSError,
    ) as exc:
        logger.debug("defined check for %r failed: %s", name, exc)
        return False
    return True


def _normalize_background_action_mode(mode: str) -> str:
    normalized = str(mode).lower()
    if normalized in {"stub", "off", "none"}:
        normalized = "disabled"
    if normalized not in {"foreground", "disabled"}:
        raise ValueError("background action mode must be foreground or disabled")
    return normalized


def _set_background_action_mode(mode: str | None) -> None:
    """Select local background behavior before the runtime is driven."""
    global _BACKGROUND_ACTION_MODE
    if mode is None:
        return
    normalized = _normalize_background_action_mode(mode)
    _BACKGROUND_ACTION_MODE = normalized
    _ACTIVE_BACKGROUND_ACTION_MODE.set(normalized)


def _active_background_action_mode() -> str:
    if _ACTIVE_ROOT.get() is None:
        return _BACKGROUND_ACTION_MODE
    return _ACTIVE_BACKGROUND_ACTION_MODE.get() or _BACKGROUND_ACTION_MODE


def _continue_background_response_action(response):
    """Follow a response action in the same foreground interview context."""
    if not isinstance(response, dict) or not response.get("action"):
        return SimulatorTask(
            error=RuntimeError("background response action is invalid")
        )
    return _foreground_background_action(
        response["action"],
        **(response.get("arguments") or {}),
    )


def _foreground_background_action(action, ui_notification=None, **arguments):
    """Run a supported event in the current request and return a task value."""
    if _active_background_action_mode() == "disabled":
        return SimulatorTask(ready=False)
    from docassemble.base.functions import this_thread

    interview = getattr(this_thread, "interview", None)
    namespace = getattr(this_thread, "current_dict", None)
    status = getattr(this_thread, "interview_status", None)
    if interview is None or not isinstance(namespace, dict) or status is None:
        return SimulatorTask(
            error=RuntimeError(
                "foreground background_action requires an active interview context"
            )
        )
    if not isinstance(action, str) and not callable(action):
        return SimulatorTask(error=TypeError("unsupported background action name"))

    info = getattr(this_thread, "current_info", {})
    old = {key: info[key] for key in ("action", "arguments") if key in info}
    try:
        info["action"] = action
        info["arguments"] = arguments
        if callable(action):
            from docassemble.base.error import (
                BackgroundResponseActionError,
                BackgroundResponseError,
            )

            try:
                return SimulatorTask(action(**arguments))
            except BackgroundResponseError as error:
                return SimulatorTask(error.backgroundresponse)
            except BackgroundResponseActionError as error:
                return _continue_background_response_action(error.action)

        result = interview.askfor(
            action,
            namespace,
            dict(namespace),
            status,
            seeking=[],
            variable_stack=set(),
            questions_tried={},
        )
        question = result.get("question") if isinstance(result, dict) else None
        question_type = getattr(question, "question_type", None)
        if question_type == "backgroundresponse":
            return SimulatorTask(getattr(question, "backgroundresponse", None))
        if question_type == "backgroundresponseaction":
            return _continue_background_response_action(
                getattr(question, "action", None)
            )
        return SimulatorTask(
            error=RuntimeError(
                "background action did not finish with background_response(); "
                "worker-only behavior is not supported by the simulator"
            )
        )
    except BaseException as error:  # noqa: BLE001 - task must capture authored failures
        return SimulatorTask(error=error)
    finally:
        for key in ("action", "arguments"):
            info.pop(key, None)
        info.update(old)


def _install_background_action_fallback(mode: str | None = None) -> None:
    """Replace Celery dispatch with a foreground task, unless explicitly disabled."""
    global _BACKGROUND_INSTALLED
    _set_background_action_mode(mode)
    if _BACKGROUND_INSTALLED:
        return
    try:
        from docassemble.base import background, functions, util
    except ImportError:
        return
    # functions.background_action delegates through its module-global bg_action;
    # util re-exports the function object, while background.bg_action is useful
    # for runtimes that call the lower-level seam directly.
    functions.bg_action = _foreground_background_action
    functions.background_action = lambda *args, **kwargs: _foreground_background_action(
        *args, **kwargs
    )
    util.background_action = functions.background_action
    background.bg_action = _foreground_background_action
    _BACKGROUND_INSTALLED = True


def apply_session_stubs(*, stub_define_defined: bool = False) -> None:
    """No-op the session bookkeeping calls that hit the server database.

    define()/defined(): only stub on request. Recent docassemble versions
    implement them against the thread-context user_dict without touching the
    database, and blanket-stubbing them breaks interviews that branch on
    defined(). Older setups (as in the original harness) needed them stubbed;
    use --stub-defined for those.
    """
    global _STUBBED
    if _STUBBED:
        return
    _STUBBED = True

    import docassemble.base.functions as dbf
    import docassemble.base.util as dbu

    dbf.set_sessions_data = lambda *a, **k: None
    dbf.set_sessions_title_stage = lambda *a, **k: None
    dbf.cleanup_sessions = lambda *a, **k: None
    dbu.set_sessions_data = lambda *a, **k: None
    dbu.set_sessions_title_stage = lambda *a, **k: None
    dbu.cleanup_sessions = lambda *a, **k: None

    # URL helpers need a live Flask app for url_for(); interviews build UI
    # fragments (buttons, menus) during assembly even though we never render.
    def _fake_url_of(endpoint, **kwargs):
        from urllib.parse import urlencode

        query = urlencode({k: v for k, v in kwargs.items() if v is not None})
        return f"/dasimulator/{endpoint}" + (f"?{query}" if query else "")

    dbf.url_of = _fake_url_of
    dbu.url_of = _fake_url_of

    if stub_define_defined:
        dbf.define = _fake_define
        dbf.defined = _fake_defined
        dbu.define = _fake_define
        dbu.defined = _fake_defined


def neutralize_argv() -> None:
    """Keep our CLI args out of docassemble's config loader.

    docassemble.base.config.load(arguments=sys.argv) treats any positional
    argument naming an existing file as a config filename. Drop everything
    after argv[0] before docassemble is imported.
    """
    sys.argv[:] = [sys.argv[0]]


def bootstrap(
    *,
    config_path: Path | None = None,
    extra_config: dict | None = None,
    stub_define_defined: bool = False,
    background_action_mode: str | None = None,
) -> None:
    """Run every pre-import step in the required order."""
    prepare_environment(config_path=config_path, extra_config=extra_config)
    install_fake_redis()
    neutralize_argv()
    apply_session_stubs(stub_define_defined=stub_define_defined)
    register_hooks()
    install_diagnostic_logging()
    install_attachment_filename_fallback()
    _install_background_action_fallback(background_action_mode)


class SimulatorRuntime:
    """Package-private owner of simulator installation and root activation.

    ``activate`` scopes workspace resources and policy to one operation.  The
    process patches themselves are installed separately through ``install`` so
    the CLI composition root remains the only process-level installer.
    """

    @property
    def active_root(self) -> Path | None:
        return _ACTIVE_ROOT.get()

    @property
    def file_registry(self):
        from docassemble_simulator._artifacts import active_file_registry

        return active_file_registry()

    @property
    def background_actions_enabled(self) -> bool:
        return _active_background_action_mode() == "foreground"

    def install_fake_redis(self) -> None:
        """Install the pre-import Redis adapter owned by this runtime."""
        install_fake_redis()

    @contextmanager
    def activate(
        self,
        root: str | Path,
        *,
        config_path: Path | None = None,
        extra_config: dict | None = None,
        background_action_mode: str | None = None,
        seek_diagnostics: str | None = None,
    ):
        """Activate one root and restore the prior runtime on exit."""
        from docassemble_simulator import _artifacts
        from docassemble_simulator._diagnostics import (
            is_capture_enabled,
            set_capture_enabled,
        )

        root_path = Path(root).resolve()
        effective_config = config_path or (
            root_path / ".simulator" / "config-effective.yml"
        )
        previous_config = os.environ.get("DA_CONFIG_FILE")
        previous_dyld = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH")
        previous_registry = _artifacts.active_file_registry()
        previous_capture = is_capture_enabled()
        root_token = _ACTIVE_ROOT.set(root_path)
        background_token = _ACTIVE_BACKGROUND_ACTION_MODE.set(
            _normalize_background_action_mode(
                background_action_mode or _BACKGROUND_ACTION_MODE
            )
        )
        try:
            prepare_environment(
                config_path=effective_config,
                extra_config=extra_config,
            )
            if seek_diagnostics is not None:
                set_capture_enabled(seek_diagnostics == "capture")
            yield self
        finally:
            _ACTIVE_BACKGROUND_ACTION_MODE.reset(background_token)
            set_capture_enabled(previous_capture)
            if previous_registry is None:
                _artifacts._ACTIVE_REGISTRY.set(None)
            else:
                _artifacts._ACTIVE_REGISTRY.set(previous_registry)
            if previous_config is None:
                os.environ.pop("DA_CONFIG_FILE", None)
            else:
                os.environ["DA_CONFIG_FILE"] = previous_config
            if previous_dyld is None:
                os.environ.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
            else:
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = previous_dyld
            _ACTIVE_ROOT.reset(root_token)

    def install(
        self,
        *,
        config_path: Path | None = None,
        extra_config: dict | None = None,
        stub_define_defined: bool = False,
        background_action_mode: str | None = None,
    ) -> None:
        """Install process hooks after the target runtime is importable."""
        bootstrap(
            config_path=config_path,
            extra_config=extra_config,
            stub_define_defined=stub_define_defined,
            background_action_mode=background_action_mode,
        )


__all__ = [
    "DEFAULT_CONFIG_TEXT",
    "PDF_UNAVAILABLE_MESSAGE",
    "FakeRedis",
    "PDFConversionUnavailable",
    "SimulatorRuntime",
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
