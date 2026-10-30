"""E2e: Cursor's ``cognee-remember.sh`` heads a successful save with the receipt instruction.

A recall ends the answer with a credit line led by the Cognee logo; a save ends
it with a receipt the same way, naming the dataset it went to. The instruction
is the first line of the script's output, above the server's JSON. A failed
save gets none, and ``COGNEE_PLUGIN_CITE_MEMORY=off`` gets the off note instead.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from utils.isolation import build_env, usable_bash

BASH = usable_bash()

pytestmark = pytest.mark.skipif(
    BASH is None, reason="requires a working POSIX bash (wrapper is not run on Windows)"
)


@pytest.fixture(autouse=True)
def _only_cursor(suite):
    if suite.name != "cursor":
        pytest.skip(f"{suite.name}: the save prints its result only")


def _remember(suite, home, mock_server, extra=None):
    env = build_env(
        suite,
        home,
        service_url=mock_server.url,
        api_key="k",
        extra={"COGNEE_REMEMBER_WAIT_SECONDS": "0", **(extra or {})},
    )
    return subprocess.run(
        [
            BASH,
            str(suite.scripts_dir / "cognee-remember.sh"),
            "Yuna likes Yukki",
            "--dataset",
            "notes",
        ],
        env=env,
        cwd=str(home),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


def test_a_save_starts_with_the_receipt_instruction(
    suite, temp_home, mock_server, assert_clean_real_home
):
    result = _remember(suite, temp_home, mock_server)
    assert result.returncode == 0, result.stderr
    header, _, rest = result.stdout.partition("\n")
    assert header.startswith("Saved to Cognee (JSON on the next line).")
    assert "Saved to the notes dataset:" in header and "[​](https://www.cognee.ai)" in header
    assert json.loads(rest)["ok"] is True


def test_opt_out_says_credits_are_off(suite, temp_home, mock_server, assert_clean_real_home):
    result = _remember(suite, temp_home, mock_server, extra={"COGNEE_PLUGIN_CITE_MEMORY": "off"})
    assert result.returncode == 0, result.stderr
    header, _, rest = result.stdout.partition("\n")
    assert "Cognee credits are off" in header and "cognee.ai)" not in header
    assert json.loads(rest)["ok"] is True
