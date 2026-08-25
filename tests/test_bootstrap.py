import sys
import types

from docassemble_simulator.bootstrap import _configured_timezone, deep_merge, prepare_environment


class TestBootstrapConfig:
    def test_configured_timezone_uses_docassemble_config(self, monkeypatch):
        da = types.ModuleType("docassemble")
        base = types.ModuleType("docassemble.base")
        config = types.ModuleType("docassemble.base.config")
        config.daconfig = {"timezone": "America/Chicago"}
        base.__path__ = []
        da.base = base
        base.config = config
        for name, module in {
            "docassemble": da,
            "docassemble.base": base,
            "docassemble.base.config": config,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)

        assert _configured_timezone() == "America/Chicago"

    def test_prepare_environment_writes_effective_yaml(self, tmp_path, monkeypatch):
        import docassemble_simulator.bootstrap as bootstrap

        monkeypatch.setattr(bootstrap, "_PREPARED", False)
        target = tmp_path / ".simulator" / "config-effective.yml"
        prepare_environment(
            config_path=target,
            extra_config={
                "timezone": "America/Chicago",
                "jinja data": {"category": {"family": "Family"}},
            },
        )

        import yaml

        loaded = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert loaded["timezone"] == "America/Chicago"
        assert loaded["jinja data"]["category"]["family"] == "Family"


class TestDeepMerge:
    def test_nested_merge_preserves_unrelated_keys(self):
        base = {"a": {"x": 1, "y": 2}, "keep": True}
        deep_merge(base, {"a": {"y": 3}})
        assert base == {"a": {"x": 1, "y": 3}, "keep": True}

    def test_scalar_override_replaces_dict(self):
        base = {"a": {"x": 1}}
        deep_merge(base, {"a": "flat"})
        assert base == {"a": "flat"}

    def test_dict_override_replaces_scalar(self):
        base = {"a": "flat"}
        deep_merge(base, {"a": {"x": 1}})
        assert base == {"a": {"x": 1}}

    def test_new_key_added(self):
        base = {}
        deep_merge(base, {"redis": "redis://localhost:6399"})
        assert base == {"redis": "redis://localhost:6399"}

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        deep_merge(base, {"a": {"b": {"d": 9}}})
        assert base == {"a": {"b": {"c": 1, "d": 9}}}
