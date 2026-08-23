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

import os
import sys
import types
from pathlib import Path

DEFAULT_CONFIG_TEXT = """\
db:
  database name: docassemble-simulator
  driver: sqlite
redis: "redis://localhost:6399"
debug: true
"""


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
        config_dir = Path.home() / ".config" / "docassemble-simulator"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yml"

    text = DEFAULT_CONFIG_TEXT
    if extra_config:
        import yaml

        merged = yaml.safe_load(text) or {}
        deep_merge(merged, extra_config)
        text = yaml.safe_dump(merged, sort_keys=False)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists() or extra_config:
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


def register_hooks() -> None:
    """Register the webapp hook modules plus a minimal local implementation.

    The webapp default implementations of several hooks raise
    NotImplementedError when no server is running; MinimalHooks overrides
    exactly those.
    """
    from docassemble.base.plugin_manager import pm
    from docassemble.webapp import main as webapp_main
    from docassemble.webapp.main import hooks as main_hooks
    from docassemble.webapp.interview import hooks as interview_hooks

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
            return ""

        @hookimpl
        def get_default_timezone(self):
            return ""

        @hookimpl
        def get_default_country(self):
            return ""

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
        def get_configuration(self):
            # Return the live server config (not a stub): interviews read
            # `jinja data` and package settings through this hook.
            import docassemble.base.config as da_config_mod

            cfg = dict(getattr(da_config_mod, "daconfig", {}) or {})
            cfg.setdefault("debug", True)
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
    except Exception:
        return False
    return True


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
) -> None:
    """Run every pre-import step in the required order."""
    prepare_environment(config_path=config_path, extra_config=extra_config)
    install_fake_redis()
    neutralize_argv()
    apply_session_stubs(stub_define_defined=stub_define_defined)
    register_hooks()
