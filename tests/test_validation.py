import json
from pathlib import Path

import pytest

from moex_bot.validation import validate_project_configs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONFIGS = {
    "geo_sources.json",
    "ownership_disclosures.json",
    "replay.json",
    "runtime.json",
    "services.json",
    "shadow.json",
    "universe.json",
}


def test_config_directory_contains_only_documented_files() -> None:
    assert {path.name for path in (PROJECT_ROOT / "config").glob("*.json")} == EXPECTED_CONFIGS


def test_all_committed_project_configs_are_valid() -> None:
    checked = validate_project_configs(PROJECT_ROOT)
    assert len(checked) == 10


def test_invalid_runtime_environment_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "project"
    import shutil

    shutil.copytree(PROJECT_ROOT / "config", root / "config")
    shutil.copytree(PROJECT_ROOT / "examples", root / "examples")
    (root / "config" / "runtime.json").write_text(
        json.dumps({"t_invest_environment": "paper"}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        validate_project_configs(root)
