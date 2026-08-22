"""Direct Bash wrappers resolve project datasets from the invoking shell cwd."""

from __future__ import annotations

import os
import subprocess

import pytest
from utils.isolation import build_env

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Bash wrapper contract")

_DERIVED_DATASET = "project_clirepo_2742923114c7"


@pytest.fixture
def project_repo(project_dir):
    subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:Org/CliRepo.git"],
        cwd=project_dir,
        check=True,
    )
    return project_dir


def _run_wrapper(suite, temp_home, project_repo, mock_server, name, *args):
    env = build_env(
        suite,
        temp_home,
        service_url=mock_server.url,
        api_key="test-api-key",
        cwd=project_repo,
        extra={
            "COGNEE_DATASET_SCOPE": "project",
            "COGNEE_REMEMBER_WAIT_SECONDS": "0",
        },
    )
    return subprocess.run(
        ["bash", str(suite.scripts_dir / name), *args],
        cwd=project_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_search_sends_dataset_derived_from_invoking_cwd(
    suite, temp_home, project_repo, mock_server
):
    result = _run_wrapper(
        suite,
        temp_home,
        project_repo,
        mock_server,
        "cognee-search.sh",
        "where is the launch plan?",
        "5",
        "--graph",
    )
    assert result.returncode == 0, result.stderr
    request = mock_server.assert_called("POST", "/api/v1/recall")
    assert request["json"]["datasets"] == [_DERIVED_DATASET]


def test_remember_sends_dataset_derived_from_invoking_cwd(
    suite, temp_home, project_repo, mock_server
):
    result = _run_wrapper(
        suite,
        temp_home,
        project_repo,
        mock_server,
        "cognee-remember.sh",
        "launch plans live in the project graph",
    )
    assert result.returncode == 0, result.stderr
    request = mock_server.assert_called("POST", "/api/v1/remember")
    assert request["form"]["datasetName"] == _DERIVED_DATASET


def test_remember_dataset_flag_wins_over_project_derivation(
    suite, temp_home, project_repo, mock_server
):
    result = _run_wrapper(
        suite,
        temp_home,
        project_repo,
        mock_server,
        "cognee-remember.sh",
        "launch plans live in an explicit graph",
        "--dataset",
        "explicit",
    )
    assert result.returncode == 0, result.stderr
    request = mock_server.assert_called("POST", "/api/v1/remember")
    assert request["form"]["datasetName"] == "explicit"
