"""E2e: the cognee-forget.sh wrapper runs as a subprocess against the mock server.

The wrapper ships in both suites (claude-code and codex), identical except for
the per-suite state dir, so the tests run against both via the parametrized
``suite`` fixture. Covered: credential resolution (env key, the shared-root
``api_key.json`` fallback, and the exit-2 no-key path), the ``X-Api-Key``
header on every API call, the forget payload shape, the ``HTTP <status>``
trailer (including 404 pass-through), and argument/usage errors.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from utils.isolation import build_env
from utils.mock_cognee import DEFAULT_DATA_ID, DEFAULT_DATASET_ID
from utils.suites import PLUGIN_DIR_NAME


def _usable_bash() -> str | None:
    """Path to a bash that actually runs scripts, or None.

    ``shutil.which("bash")`` alone is not enough: on Windows it resolves to
    ``C:\\Windows\\System32\\bash.exe`` — the WSL launcher — which, with no distro
    installed, prints "Windows Subsystem for Linux has no installed
    distributions" (as UTF-16) and exits 1. The wrapper is a POSIX script that
    also needs ``python3`` and ``curl`` on the shell's PATH, so it is not
    exercised on Windows at all; elsewhere the probe guards against any stub.
    """
    if sys.platform == "win32":
        return None
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        probe = subprocess.run(
            [bash, "-c", "echo __ok__"], capture_output=True, encoding="utf-8", timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bash if probe.returncode == 0 and "__ok__" in probe.stdout else None


BASH = _usable_bash()

pytestmark = pytest.mark.skipif(
    BASH is None, reason="requires a working POSIX bash (wrapper is not run on Windows)"
)


def run_forget(suite, *args, home, service_url=None, api_key=None):
    script = suite.scripts_dir / "cognee-forget.sh"
    env = build_env(suite, home, service_url=service_url, api_key=api_key)
    return subprocess.run(
        [BASH, str(script), *args],
        env=env,
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )


def test_datasets_sends_env_api_key_and_status_trailer(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = run_forget(
        suite, "datasets", home=temp_home, service_url=mock_server.url, api_key="k-env"
    )
    assert result.returncode == 0, result.stderr
    assert "agent_sessions" in result.stdout
    assert result.stdout.rstrip().endswith("HTTP 200")
    call = mock_server.assert_called("GET", "/api/v1/datasets")
    assert call["headers"].get("X-Api-Key") == "k-env"


def test_api_key_falls_back_to_cached_key(suite, temp_home, mock_server, assert_clean_real_home):
    cache_dir = temp_home / PLUGIN_DIR_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "api_key.json").write_text(json.dumps({"api_key": "k-cached"}))
    result = run_forget(suite, "datasets", home=temp_home, service_url=mock_server.url)
    assert result.returncode == 0, result.stderr
    call = mock_server.assert_called("GET", "/api/v1/datasets")
    assert call["headers"].get("X-Api-Key") == "k-cached"


def test_no_key_anywhere_exits_2_without_calling_server(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = run_forget(suite, "datasets", home=temp_home, service_url=mock_server.url)
    assert result.returncode == 2
    assert "no API key resolved" in result.stderr
    assert mock_server.calls == []


def test_data_lists_dataset_items(suite, temp_home, mock_server, assert_clean_real_home):
    mock_server.set_dataset_data(
        [{"id": DEFAULT_DATA_ID, "name": "session_doc", "datasetId": DEFAULT_DATASET_ID}]
    )
    result = run_forget(
        suite,
        "data",
        DEFAULT_DATASET_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 0, result.stderr
    assert DEFAULT_DATA_ID in result.stdout
    assert result.stdout.rstrip().endswith("HTTP 200")
    call = mock_server.assert_called("GET", f"/api/v1/datasets/{DEFAULT_DATASET_ID}/data")
    assert call["headers"].get("X-Api-Key") == "k"


def test_raw_prints_stored_content(suite, temp_home, mock_server, assert_clean_real_home):
    mock_server.set_raw_content("Session ID: s1\n\nwe talked about tennis")
    result = run_forget(
        suite,
        "raw",
        DEFAULT_DATASET_ID,
        DEFAULT_DATA_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 0, result.stderr
    assert "we talked about tennis" in result.stdout
    assert result.stdout.rstrip().endswith("HTTP 200")


def test_raw_404_passes_status_through_and_exits_zero(
    suite, temp_home, mock_server, assert_clean_real_home
):
    path = f"/api/v1/datasets/{DEFAULT_DATASET_ID}/data/{DEFAULT_DATA_ID}/raw"
    mock_server.force_response("GET", path, 404, {"detail": "not found"})
    result = run_forget(
        suite,
        "raw",
        DEFAULT_DATASET_ID,
        DEFAULT_DATA_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    # 404 means "already deleted" to the skill — the helper must surface the
    # status in the trailer, not turn it into a non-zero exit.
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("HTTP 404")


def test_forget_posts_dataset_and_data_id(suite, temp_home, mock_server, assert_clean_real_home):
    result = run_forget(
        suite,
        "forget",
        DEFAULT_DATASET_ID,
        DEFAULT_DATA_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip().endswith("HTTP 200")
    call = mock_server.assert_called(
        "POST", "/api/v1/forget", datasetId=DEFAULT_DATASET_ID, dataId=DEFAULT_DATA_ID
    )
    assert call["headers"].get("X-Api-Key") == "k"


def test_env_prints_exports_without_calling_server(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = run_forget(suite, "env", home=temp_home, service_url=mock_server.url, api_key="k-env")
    assert result.returncode == 0, result.stderr
    assert f"export COGNEE_BASE_URL={mock_server.url}" in result.stdout
    assert "export COGNEE_API_KEY=k-env" in result.stdout
    assert mock_server.calls == []


def test_unknown_command_exits_1_with_usage(suite, temp_home, mock_server, assert_clean_real_home):
    result = run_forget(
        suite, "obliterate", home=temp_home, service_url=mock_server.url, api_key="k"
    )
    assert result.returncode == 1
    assert "unknown command" in result.stderr
    assert "Usage:" in result.stderr


def test_forget_without_data_id_exits_1(suite, temp_home, mock_server, assert_clean_real_home):
    result = run_forget(
        suite,
        "forget",
        DEFAULT_DATASET_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 1
    assert "forget requires" in result.stderr
    assert mock_server.assert_not_called("POST", "/api/v1/forget") is None


def test_crafted_id_cannot_inject_json_fields(
    suite, temp_home, mock_server, assert_clean_real_home
):
    # The payload was once built by string interpolation, so an id containing a
    # quote could close the JSON string and append fields — `everything: true`
    # is checked first by the server and deletes every dataset the user owns,
    # defeating this helper's single-document-only guarantee.
    result = run_forget(
        suite,
        "forget",
        'x", "everything": true, "z": "y',
        DEFAULT_DATA_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 1
    assert "invalid id (expected a UUID)" in result.stderr
    mock_server.assert_not_called("POST", "/api/v1/forget")


def test_forget_payload_has_exactly_the_two_expected_fields(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = run_forget(
        suite,
        "forget",
        DEFAULT_DATASET_ID,
        DEFAULT_DATA_ID,
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 0, result.stderr
    call = mock_server.assert_called("POST", "/api/v1/forget")
    assert call["json"] == {"datasetId": DEFAULT_DATASET_ID, "dataId": DEFAULT_DATA_ID}


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../../etc/passwd",  # would resolve to another endpoint after curl normalizes it
        "agent_sessions",  # a dataset name where an id is required
        "",  # empty (caught by the arity check)
        "11111111-1111-5111-8111-11111111111",  # one hex digit short
    ],
)
def test_non_uuid_ids_are_refused_before_any_request(
    suite, temp_home, mock_server, bad_id, assert_clean_real_home
):
    result = run_forget(
        suite, "data", bad_id, home=temp_home, service_url=mock_server.url, api_key="k"
    )
    assert result.returncode == 1
    assert mock_server.calls == []


def test_uppercase_uuid_is_accepted(suite, temp_home, mock_server, assert_clean_real_home):
    # str(UUID) is lowercase, but a hand-typed or upstream-uppercased id is
    # still a valid UUID and must not be rejected.
    result = run_forget(
        suite,
        "data",
        DEFAULT_DATASET_ID.upper(),
        home=temp_home,
        service_url=mock_server.url,
        api_key="k",
    )
    assert result.returncode == 0, result.stderr
    mock_server.assert_called("GET", f"/api/v1/datasets/{DEFAULT_DATASET_ID.upper()}/data")
