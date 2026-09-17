import pytest

from docassemble_simulator.config import (
    discover_config_files,
    load_config,
    normalize_config,
    redact_config,
    resolve_configuration,
    simulator_settings,
)


class TestConfigDiscovery:
    def test_walks_up_with_nearest_and_local_precedence(self, tmp_path):
        parent = tmp_path / "parent"
        package = parent / "package"
        package.mkdir(parents=True)
        (parent / "simulator.toml").write_text('timezone = "UTC"\n', encoding="utf-8")
        (package / ".config" / "simulator").mkdir(parents=True)
        (package / ".config" / "simulator" / "config.toml").write_text(
            'timezone = "America/Chicago"\n', encoding="utf-8"
        )
        (package / "simulator.local.toml").write_text(
            'timezone = "America/Los_Angeles"\n', encoding="utf-8"
        )
        global_file = tmp_path / "global.toml"
        global_file.write_text(
            'timezone = "Europe/London"\n[global]\nvalue = true\n', encoding="utf-8"
        )

        files = discover_config_files(package, global_path=global_file)

        assert files == [
            package / "simulator.local.toml",
            package / ".config" / "simulator" / "config.toml",
            parent / "simulator.toml",
        ]
        loaded = load_config(package, global_path=global_file)
        assert loaded["timezone"] == "America/Los_Angeles"
        assert loaded["global"]["value"] is True

    def test_malformed_lower_priority_file_fails_loudly(self, tmp_path):
        package = tmp_path / "package"
        package.mkdir()
        (tmp_path / "simulator.toml").write_text("not = [valid\n", encoding="utf-8")
        (package / ".config" / "simulator").mkdir(parents=True)
        (package / ".config" / "simulator" / "config.toml").write_text(
            'timezone = "UTC"\n', encoding="utf-8"
        )

        with pytest.raises(ValueError, match="simulator.toml"):
            load_config(package, global_path=tmp_path / "missing.toml")

    def test_normalizes_jinja_data_table(self):
        assert normalize_config({"jinja-data": {"category": {"family": "Family"}}}) == {
            "jinja data": {"category": {"family": "Family"}}
        }

    def test_resolved_configuration_owns_override_and_command_precedence(
        self, tmp_path
    ):
        override = tmp_path / "override.yml"
        override.write_text(
            "timezone: America/Chicago\nsimulator:\n  background_actions: disabled\n",
            encoding="utf-8",
        )

        resolved = resolve_configuration(
            tmp_path,
            override_path=override,
            base={"timezone": "UTC"},
            command_overrides={"background_actions": "foreground"},
        )

        assert resolved.pass_through["timezone"] == "America/Chicago"
        assert resolved.simulator["background_actions"] == "foreground"
        assert resolved.effective_path == (
            tmp_path / ".simulator" / "config-effective.yml"
        )
        assert resolved.report()["config_override"] == str(override.resolve())

    def test_resolved_configuration_fingerprint_tracks_effective_values(self, tmp_path):
        first = resolve_configuration(
            tmp_path,
            base={"jinja-data": {"category": {"family": "Family", "b": "B"}}},
        )
        reordered = resolve_configuration(
            tmp_path,
            base={"jinja-data": {"category": {"b": "B", "family": "Family"}}},
        )
        other = resolve_configuration(
            tmp_path,
            base={
                "jinja-data": {"category": {"family": "Family", "miscellaneous": "M"}}
            },
        )

        assert resolve_configuration(tmp_path).fingerprint == ""
        assert first.fingerprint
        assert first.fingerprint == reordered.fingerprint
        assert first.fingerprint != other.fingerprint

    def test_simulator_settings_and_secret_redaction(self):
        settings = simulator_settings(
            {
                "timezone": "America/Chicago",
                "simulator": {
                    "background_actions": "disabled",
                    "render_bindings": {"x": "clients[0]"},
                },
            }
        )
        assert settings["background_actions"] == "disabled"
        assert settings["render_bindings"]["x"] == "clients[0]"
        assert settings["seek_diagnostics"] == "capture"
        assert redact_config(
            {"password": "dont-print", "nested": {"token": "secret"}}
        ) == {
            "password": "<redacted>",
            "nested": {"token": "<redacted>"},
        }

    def test_seek_diagnostics_mode_normalization_and_validation(self):
        assert (
            simulator_settings({"simulator": {"seek_diagnostics": "off"}})[
                "seek_diagnostics"
            ]
            == "off"
        )
        assert (
            simulator_settings({"simulator": {"seek-diagnostics": "disabled"}})[
                "seek_diagnostics"
            ]
            == "off"
        )
        assert (
            simulator_settings({"simulator": {"seek_diagnostics": "true"}})[
                "seek_diagnostics"
            ]
            == "capture"
        )
        with pytest.raises(ValueError, match="seek_diagnostics"):
            simulator_settings({"simulator": {"seek_diagnostics": "sometimes"}})
