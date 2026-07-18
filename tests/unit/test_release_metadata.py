"""Release metadata must have one runtime version and portable defaults."""

from __future__ import annotations

from pathlib import Path

from querygate import __version__
from querygate.core.config import AppConfig


def test_runtime_version_matches_application_default():
    assert AppConfig(_env_file=None).app_version == __version__


def test_default_example_config_paths_work_outside_repository_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    config = AppConfig(_env_file=None)

    assert Path(config.connections_file).is_file()
    assert Path(config.policy_file).is_file()
    assert Path(config.connections_file).name == "connections.example.yaml"
    assert Path(config.policy_file).name == "policy.example.yaml"
