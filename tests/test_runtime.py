import os
from pathlib import Path

from docassemble_simulator._diagnostics import is_capture_enabled
from docassemble_simulator._runtime import SimulatorRuntime


def test_workspace_activation_scopes_resources_and_runtime_policy(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("DA_CONFIG_FILE", raising=False)
    runtime = SimulatorRuntime()
    first = tmp_path / "first"
    second = tmp_path / "second"

    with runtime.activate(
        first,
        extra_config={"timezone": "America/Chicago"},
        background_action_mode="disabled",
        seek_diagnostics="off",
    ):
        assert runtime.active_root == first.resolve()
        assert (
            runtime.file_registry.directory
            == (first / ".simulator" / "files").resolve()
        )
        assert (
            Path(os.environ["DA_CONFIG_FILE"])
            == first / ".simulator" / "config-effective.yml"
        )
        assert not runtime.background_actions_enabled
        assert not is_capture_enabled()

        with runtime.activate(
            second,
            background_action_mode="foreground",
            seek_diagnostics="capture",
        ):
            assert runtime.active_root == second.resolve()
            assert (
                runtime.file_registry.directory
                == (second / ".simulator" / "files").resolve()
            )
            assert runtime.background_actions_enabled
            assert is_capture_enabled()

        assert runtime.active_root == first.resolve()
        assert not runtime.background_actions_enabled
        assert not is_capture_enabled()

    assert runtime.active_root is None
    assert "DA_CONFIG_FILE" not in os.environ
    assert is_capture_enabled()
