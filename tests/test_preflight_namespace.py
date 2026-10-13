"""Packaging failures at the CLI envelope, without changing runtime lanes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def namespace_environment(tmp_path):
    package = tmp_path / "docassemble"
    package.mkdir()
    for name in ("base", "webapp"):
        directory = package / name
        directory.mkdir()
        (directory / "__init__.py").write_text("")
    for name, version, files in (
        ("legacy-shim", "1.0", ["docassemble/__init__.py"]),
        ("setuptools", "83.0.0", []),
    ):
        metadata = tmp_path / f"{name.replace('-', '_')}-{version}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        )
        (metadata / "RECORD").write_text("".join(f"{file},,\n" for file in files))
    (tmp_path / "sitecustomize.py").write_text(
        "import sys\n"
        "class BlockPkgResources:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'pkg_resources':\n"
        "            raise ModuleNotFoundError(\"No module named 'pkg_resources'\", name=fullname)\n"
        "sys.meta_path.insert(0, BlockPkgResources())\n"
    )
    return tmp_path


def _check(root, source):
    (root / "docassemble" / "__init__.py").write_text(source)
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), str(Path(__file__).resolve().parents[1] / "src")]
    )
    return subprocess.run(
        [sys.executable, "-m", "docassemble_simulator", "check", "--json", "--offline"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("error_type", ["ModuleNotFoundError", "ImportError"])
def test_namespace_shim_failure_names_owner_and_reproducible_recovery(
    namespace_environment, error_type
):
    blocker = namespace_environment / "sitecustomize.py"
    blocker.write_text(blocker.read_text().replace("ModuleNotFoundError", error_type))
    completed = _check(
        namespace_environment,
        "__import__('pkg_resources').declare_namespace(__name__)\n",
    )
    assert completed.returncode == 1
    error = json.loads(completed.stdout)["error"]
    assert error["kind"] == "input"
    message = error["message"]
    assert "legacy-shim" in message
    assert "83.0.0" in message
    assert "setuptools<81" in message
    assert "override-dependencies" in message
    assert "sync" in message
    assert "native namespaces" in message
    assert "Check native libraries" not in message


def test_missing_pkg_resources_without_namespace_shim_keeps_import_error(
    namespace_environment,
):
    completed = _check(namespace_environment, "import pkg_resources\n")
    assert completed.returncode == 1
    error = json.loads(completed.stdout)["error"]
    assert error["kind"] == "input"
    assert "No module named 'pkg_resources'" in error["message"]
    assert "installed but failed to import" in error["message"]
    assert "namespace packaging conflict" not in error["message"]
    assert "override-dependencies" not in error["message"]


def test_native_import_failure_keeps_existing_error(namespace_environment):
    completed = _check(
        namespace_environment, "raise ImportError('libnative.so: image not found')\n"
    )
    assert completed.returncode == 1
    assert json.loads(completed.stdout)["error"]["message"] == (
        "docassemble.base is installed but failed to import; this is not a "
        "missing-package problem. Check native libraries and the target "
        "environment. (underlying error: libnative.so: image not found)"
    )
