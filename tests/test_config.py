import pytest

from docassemble_simulator.config import (
    DOCASSEMBLE_DEFAULTS,
    discover_config_files,
    load_config,
    normalize_config,
    pass_through_config,
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

    def test_isolated_handoff_keeps_original_discovery_root_and_precedence(
        self, tmp_path
    ):
        original = tmp_path / "project"
        isolated = tmp_path / "temporary" / "project"
        original.mkdir()
        isolated.mkdir(parents=True)
        (tmp_path / "simulator.toml").write_text(
            'timezone = "UTC"\n[jinja-data]\nancestor = "yes"\n',
            encoding="utf-8",
        )
        (original / "simulator.toml").write_text(
            'timezone = "America/Chicago"\n[jinja-data]\nproject = "yes"\n',
            encoding="utf-8",
        )
        resolved = resolve_configuration(
            isolated,
            discovery_root=original,
            base=load_config(original),
            mapping_overrides={"timezone": "America/Los_Angeles"},
            command_overrides={"background_actions": "disabled"},
        )

        assert resolved.values["timezone"] == "America/Los_Angeles"
        assert resolved.values["jinja data"] == {
            "ancestor": "yes",
            "project": "yes",
        }
        assert resolved.simulator["background_actions"] == "disabled"
        assert resolved.files == (
            tmp_path / "simulator.toml",
            original / "simulator.toml",
        )
        assert (
            str(tmp_path / "simulator.toml")
            in resolved.session_context()["config_files"]
        )
        assert resolved.effective_path == (
            isolated / ".simulator" / "config-effective.yml"
        )

    def test_ephemeral_handoff_layer_is_not_saved_as_a_resume_input(self, tmp_path):
        handoff = tmp_path / "runner-config.yml"
        handoff.write_text("timezone: UTC\n", encoding="utf-8")

        resolved = resolve_configuration(
            tmp_path,
            additional_override_paths=(handoff,),
        )

        assert resolved.report()["config_overrides"] == [str(handoff)]
        assert str(handoff) not in resolved.session_context()["config_files"]
        assert "config_overrides" not in resolved.session_context()

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

    def test_config_report_exposes_session_fingerprint(self, tmp_path):
        configured = resolve_configuration(
            tmp_path, base={"jinja-data": {"category": {"family": "Family"}}}
        )
        unset = resolve_configuration(tmp_path)

        assert configured.report()["config_fingerprint"] == configured.fingerprint
        assert unset.report()["config_fingerprint"] == ""

    def test_config_report_redacts_effective_and_shadowed_scalar_secrets(
        self, tmp_path, monkeypatch
    ):
        package = tmp_path / "package"
        package.mkdir()
        (tmp_path / "simulator.toml").write_text(
            'api_key = "SYNTHETIC_ANCESTOR_API"\n'
            'password = "SYNTHETIC_ANCESTOR_PASSWORD"\n'
            'safe_label = "ancestor-visible"\n'
            "[service]\n"
            'api_key = "SYNTHETIC_ANCESTOR_NESTED_API"\n'
            'tokens = ["SYNTHETIC_ANCESTOR_TOKEN"]\n',
            encoding="utf-8",
        )
        (package / "simulator.toml").write_text(
            'api_key = "SYNTHETIC_PROJECT_API"\n'
            'password = "SYNTHETIC_PROJECT_PASSWORD"\n'
            'safe_label = "project-visible"\n'
            "[service]\n"
            'api_key = "SYNTHETIC_PROJECT_NESTED_API"\n'
            'tokens = ["SYNTHETIC_PROJECT_TOKEN"]\n',
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "DOCASSEMBLE_SIMULATOR_CONFIG", str(tmp_path / "missing-global.toml")
        )

        provenance = resolve_configuration(package).report()["provenance"]

        for key in ("api_key", "password"):
            assert provenance[key]["value"] == "<redacted>"
            assert provenance[key]["shadowed"][-1]["value"] == "<redacted>"
            assert all(
                entry["value"] == "<redacted>"
                for entry in provenance[key]["layers"]
                if entry["source"] != "built-in default"
            )
        assert provenance["service"]["value"] == {
            "api_key": "<redacted>",
            "tokens": "<redacted>",
        }
        assert provenance["safe_label"]["value"] == "project-visible"
        rendered = repr(provenance)
        assert "SYNTHETIC_" not in rendered
        assert "project-visible" in rendered

    def test_config_report_exposes_layers_and_shadowed_values(
        self, tmp_path, monkeypatch
    ):
        package = tmp_path / "package"
        package.mkdir()
        (tmp_path / "simulator.toml").write_text('timezone = "UTC"\n', encoding="utf-8")
        (package / ".config" / "simulator").mkdir(parents=True)
        local = package / ".config" / "simulator" / "config.toml"
        local.write_text('timezone = "America/Chicago"\n', encoding="utf-8")
        monkeypatch.setenv(
            "DOCASSEMBLE_SIMULATOR_CONFIG", str(tmp_path / "missing-global.toml")
        )

        resolved = resolve_configuration(package)
        provenance = resolved.report()["provenance"]["timezone"]

        assert provenance["source"] == str(local)
        assert provenance["value"] == "America/Chicago"
        assert provenance["shadowed"][-1] == {
            "source": str(tmp_path / "simulator.toml"),
            "value": "UTC",
        }
        assert provenance["layers"][-1]["value"] == "America/Chicago"
        assert resolved.report()["provenance"]["debug"]["source"] == "built-in default"

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

    def test_no_server_database_default_and_assemblyline_metadata_opt_out(self):
        assert "db" not in DOCASSEMBLE_DEFAULTS
        assert DOCASSEMBLE_DEFAULTS["assembly line"] == {
            "update session metadata": False
        }

    def test_project_config_can_opt_into_assemblyline_session_metadata(self):
        passed_through = pass_through_config(
            {"assembly line": {"update session metadata": True}}
        )

        assert passed_through == {"assembly line": {"update session metadata": True}}
