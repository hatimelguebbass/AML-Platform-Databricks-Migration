"""The bundle files against the JSON schema of the pinned Databricks CLI, without a workspace.

`databricks bundle validate` needs a workspace; this catches misspelt keys and wrong types in
every pull request. CI sets REQUIRE_DATABRICKS_CLI so that a missing CLI fails instead of skipping.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.filterwarnings("ignore:Possible nested set:FutureWarning")

BUNDLE_FILES = [ROOT / "databricks.yml", *sorted((ROOT / "resources").glob("*.yml"))]


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    cli = shutil.which("databricks")
    if cli is None:
        if os.environ.get("REQUIRE_DATABRICKS_CLI"):
            pytest.fail("the Databricks CLI is not installed")
        pytest.skip("the Databricks CLI is not installed")
    output = subprocess.run([cli, "bundle", "schema"], check=True, capture_output=True, text=True).stdout  # noqa: S603
    # The interpolation patterns use Unicode classes Python's re lacks; these are their equivalents.
    # The class of both comes first: substituting \p{L} inside it would nest brackets.
    output = output.replace(r"[\\p{L}\\p{N}]", r"[^\\W_]").replace(r"\\p{L}", r"[^\\W\\d_]").replace(r"\\p{N}", r"\\d")
    loaded: dict[str, Any] = json.loads(output)
    return loaded


@pytest.mark.parametrize("path", BUNDLE_FILES, ids=lambda p: p.name)
def test_bundle_file_matches_cli_schema(schema: dict[str, Any], path: Path) -> None:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    jsonschema.validate(config, schema)


def test_cli_version_matches_ci() -> None:
    pinned = yaml.safe_load((ROOT / "databricks.yml").read_text(encoding="utf-8"))["bundle"]["databricks_cli_version"]
    installer = (ROOT / ".github/actions/setup-databricks-cli/action.yml").read_text(encoding="utf-8")
    assert re.search(rf"default: \"?{re.escape(pinned)}\"?", installer), "CI installs another CLI version"


def _daily() -> dict[str, Any]:
    job: dict[str, Any] = yaml.safe_load((ROOT / "resources/aml_daily.yml").read_text(encoding="utf-8"))
    daily: dict[str, Any] = job["resources"]["jobs"]["aml_daily"]
    return daily


def test_daily_job_runs_the_dbt_versions_ci_parses_with() -> None:
    environments = {e["environment_key"]: e["spec"]["dependencies"] for e in _daily()["environments"]}
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    parsed_with = re.search(r"^DBT := uvx --quiet --from (\S+) --with (\S+) dbt$", makefile, re.MULTILINE)
    assert parsed_with is not None
    assert sorted(environments["dbt"]) == sorted(parsed_with.groups())


def test_audits_are_never_retried() -> None:
    for task in _daily()["tasks"]:
        if task["task_key"].startswith("audit_"):
            # Serverless retries a task without a retry policy unless auto optimization is off.
            assert task.get("disable_auto_optimization") is True, task["task_key"]
            assert "max_retries" not in task, task["task_key"]


def test_every_published_layer_has_a_staging_schema() -> None:
    schemas = yaml.safe_load((ROOT / "resources/schemas.yml").read_text(encoding="utf-8"))["resources"]["schemas"]
    layer_lists = {
        parameter.removeprefix("--layers=")
        for task in _daily()["tasks"]
        for parameter in task.get("python_wheel_task", {}).get("parameters", [])
        if parameter.startswith("--layers=")
    }
    assert len(layer_lists) == 1, "open_staging and publish must see the same layers"
    for layer in layer_lists.pop().split(","):
        assert layer in schemas
        assert f"{layer}_staging" in schemas
        # Staging holds unaudited rows: no group reads it.
        assert "grants" not in schemas[f"{layer}_staging"]
