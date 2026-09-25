"""The plugins that share ``~/.cognee-plugin/venv`` must agree on what goes in it.

claude-code, codex and openclaw all install cognee into the one shared venv and
each records what it installed in a ready-marker. When their pins differ, every
cold boot by a different plugin flips the venv to *its* version (1.5.4 -> 1.5.3
-> 1.5.4 ...), each time running that release's migrations over a database the
other release wrote. When their marker file names differ, one plugin's marker is
permanently stale to the other, which then reinstalls on every cold boot.

The claude-code/codex pair is asserted equal here, and openclaw's pin now is too:
it was bumped to match when the code-graph field rename forced a 1.5.4 floor.
The ready-marker filename still differs and stays a strict xfail until openclaw
is migrated off ``.venv-ready.json``.

``OPENCLAW_SERVER_TS`` used to resolve one directory too high, so both openclaw
assertions failed on a missing file and xfailed for a reason that had nothing to
do with what they claim to check — which is how the pin drift survived unnoticed.
Now that the path is right, the file has to actually be there: the Windows job
(`.github/workflows/plugin-windows-tests.yml`) sparse-checks out only the three
Python plugins and `integrations/tests`, so the openclaw checks skip there rather
than erroring on a tree that was never fetched. They still run on every full
checkout, which is where the cross-plugin guard is worth having.
"""

from __future__ import annotations

import re

import pytest
from utils.suites import ALL_SUITES, Suite

# scripts_dir is integrations/<suite>/scripts, so parents[1] is integrations/.
OPENCLAW_SERVER_TS = ALL_SUITES[0].scripts_dir.parents[1] / "openclaw" / "src" / "server.ts"

#: Skipped, never failed, when openclaw was not fetched — a sparse checkout says
#: nothing about whether the pins agree. Absence is only tolerated here; on a full
#: checkout a missing server.ts still fails these tests loudly.
requires_openclaw_tree = pytest.mark.skipif(
    not OPENCLAW_SERVER_TS.is_file(),
    reason="openclaw is outside this checkout (the Windows job sparse-checks out "
    "only the Python plugins and the shared suite)",
)


def _python_pin(suite: Suite) -> str:
    source = (suite.scripts_dir / "session-start.py").read_text(encoding="utf-8")
    match = re.search(r'^_PINNED_COGNEE_VERSION = "([^"]+)"', source, re.M)
    assert match, f"{suite.name}: no _PINNED_COGNEE_VERSION"
    return match.group(1)


def _python_marker(suite: Suite) -> str:
    source = (suite.scripts_dir / "_plugin_common.py").read_text(encoding="utf-8")
    match = re.search(r'^_VENV_READY_MARKER = _SHARED_PLUGIN_ROOT / "([^"]+)"', source, re.M)
    assert match, f"{suite.name}: no _VENV_READY_MARKER"
    return match.group(1)


def _openclaw(pattern: str) -> str:
    source = OPENCLAW_SERVER_TS.read_text(encoding="utf-8")
    match = re.search(pattern, source, re.M)
    assert match, f"openclaw server.ts: {pattern!r} not found"
    return match.group(1)


def test_claude_code_and_codex_pin_the_same_cognee():
    pins = {suite.name: _python_pin(suite) for suite in ALL_SUITES}
    assert len(set(pins.values())) == 1, pins


def test_claude_code_and_codex_use_the_same_ready_marker():
    markers = {suite.name: _python_marker(suite) for suite in ALL_SUITES}
    assert set(markers.values()) == {"venv-ready.json"}, markers


@requires_openclaw_tree
def test_openclaw_pins_the_same_cognee_as_the_python_plugins():
    assert _openclaw(r"COGNEE_VERSION = '([^']+)'") == _python_pin(ALL_SUITES[0])


@requires_openclaw_tree
@pytest.mark.xfail(strict=True, reason="openclaw writes .venv-ready.json until its bump PR lands")
def test_openclaw_uses_the_same_ready_marker():
    assert _openclaw(r"READY_MARKER = os.path.join\(BASE, '([^']+)'\)") == "venv-ready.json"
