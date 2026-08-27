"""Environment bootstrap: everything that must happen BEFORE any docassemble import.

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
import shutil
import sys
import types
from pathlib import Path

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

_BACKGROUND_ACTION_MODE = "foreground"
_BACKGROUND_INSTALLED = False


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


def prepare_environment(
    *,
    config_path: Path | None = None,
    extra_config: dict | None = None,
) -> None:
    """Set native-library and config-file env vars. Must run before docassemble imports.

    Idempotent: the first call wins. The CLI prepares the environment once in
    main() (with a root-scoped effective-config path when per-package
    overrides exist); bootstrap()'s later call must not clobber DA_CONFIG_FILE
    back to the shared home config.
    """
    global _PREPARED
    if _PREPARED:
        return
    _PREPARED = True

    if sys.platform == "darwin":
        _append_dyld_fallback()

    if config_path is None:
        config_path = Path.cwd() / ".simulator" / "config-effective.yml"

    text = DEFAULT_CONFIG_TEXT
    if extra_config is not None:
        import yaml

        from docassemble_simulator.config import pass_through_config

        merged = yaml.safe_load(text) or {}
        deep_merge(merged, pass_through_config(extra_config))
        text = yaml.safe_dump(merged, sort_keys=False)

    global _SIMULATOR_STORAGE_DIR
    _SIMULATOR_STORAGE_DIR = config_path.parent / "files"
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


def deep_merge(base: dict, override: dict) -> None:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value


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
_SIMULATOR_FILES: dict[int, dict] = {}
_NEXT_SIMULATOR_FILE = 0
_SIMULATOR_STORAGE_DIR = Path.cwd() / ".simulator" / "files"


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


def register_hooks() -> None:
    """Register the webapp hook modules plus a minimal local implementation.

    The webapp default implementations of several hooks raise
    NotImplementedError when no server is running; MinimalHooks overrides
    exactly those.
    """
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
        @hookimpl
        def get_default_voice(self):
            return ""

        @hookimpl
        def get_default_dialect(self):
            return ""

        @hookimpl
        def get_default_language(self):
            return "en"

        @hookimpl
        def get_default_locale(self):
            return "en_US"

        @hookimpl
        def get_default_timezone(self):
            return _configured_timezone()

        @hookimpl
        def get_default_country(self):
            return "US"

        @hookimpl
        def get_hostname(self):
            return "localhost"

        @hookimpl
        def get_debug_status(self):
            return True

        @hookimpl
        def get_main_page_parts(self):
            return {}

        @hookimpl
        def get_button_class_prefix(self):
            return "btn"

        @hookimpl
        def save_numbered_file(
            self, filename, orig_path, yaml_file_name=None, uid=None
        ):
            global _NEXT_SIMULATOR_FILE
            _NEXT_SIMULATOR_FILE += 1
            number = _NEXT_SIMULATOR_FILE
            suffix = Path(filename).suffix or Path(orig_path).suffix
            _SIMULATOR_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
            destination = _SIMULATOR_STORAGE_DIR / f"dasimulator-{number}{suffix}"
            shutil.copyfile(orig_path, destination)
            mimetype = (
                mimetypes.guess_type(str(destination))[0] or "application/octet-stream"
            )
            _SIMULATOR_FILES[number] = {
                "path": str(destination),
                "filename": Path(filename).name,
                "extension": suffix.lstrip("."),
                "mimetype": mimetype,
                "persistent": False,
                "private": True,
            }
            return number, suffix.lstrip("."), mimetype

        @hookimpl
        def file_number_finder(
            self, file_number, filename=None, uids=None, privileged=False
        ):
            return _SIMULATOR_FILES.get(file_number)

        @hookimpl
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

    if pm.get_plugin("dasimulator.minimal") is None:
        pm.register(MinimalHooks(), name="dasimulator.minimal")


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


def _set_background_action_mode(mode: str | None) -> None:
    """Select local background behavior before the runtime is driven."""
    global _BACKGROUND_ACTION_MODE
    if mode is None:
        return
    normalized = str(mode).lower()
    if normalized in {"stub", "off", "none"}:
        normalized = "disabled"
    if normalized not in {"foreground", "disabled"}:
        raise ValueError("background action mode must be foreground or disabled")
    _BACKGROUND_ACTION_MODE = normalized


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
    if _BACKGROUND_ACTION_MODE == "disabled":
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
    install_attachment_filename_fallback()
    _install_background_action_fallback(background_action_mode)
