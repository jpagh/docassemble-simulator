import pytest

from docassemble_simulator.config import (
    discover_config_files,
    load_config,
    normalize_config,
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
        assert normalize_config(
            {"jinja-data": {"category": {"family": "Family"}}}
        ) == {"jinja data": {"category": {"family": "Family"}}}
