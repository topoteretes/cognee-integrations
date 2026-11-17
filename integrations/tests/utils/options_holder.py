"""Plugin options for tests, served the way the Claude Code plugin reads them.

The Claude Code plugin takes its options from the options holder of its session
(``claude-code/scripts/_holder.py``), not from the environment. Tests start the
holder's own server with the values they want and point the plugin at it with
``COGNEE_OPTIONS_HOLDER``, which is how the processes a hook starts find their
holder too.

* ``holder_env(values)``: the environment a subprocess needs to read ``values``.
* ``key_env(suite, key)``: the user's Cognee API key for any suite: the holder
  for claude-code, ``COGNEE_API_KEY`` for the others.
* ``set_options(monkeypatch, **values)`` and ``set_key(monkeypatch, suite, key)``:
  the same for the test's own process, merged over what the test set before.
"""

from __future__ import annotations

import importlib.util
import itertools
import tempfile
from pathlib import Path

from .suites import CLAUDE, Suite

_HOLDER_FILE = CLAUDE.scripts_dir / "_holder.py"
_count = itertools.count()
_module = []
_where = []
HOLDER_VAR = "COGNEE_OPTIONS_HOLDER"


def _holder():
    if not _module:
        spec = importlib.util.spec_from_file_location("_cognee_test_holder", _HOLDER_FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _module.append(module)
    return _module[0]


def _folder() -> Path:
    if not _where:
        _where.append(Path(tempfile.mkdtemp(prefix="cognee-holder-")))
    return _where[0]


def holder_env(values: dict) -> dict:
    """Start a holder answering ``values``; the variable that points at it."""
    state = _folder() / ("test-%d.json" % next(_count))
    _holder().serve({k: str(v) for k, v in values.items()}, state)
    return {HOLDER_VAR: str(state)}


_OPTION_PREFIX = "CLAUDE_PLUGIN_OPTION_"


def apply_options(suite: Suite, env: dict, options: dict | None = None) -> dict:
    """Move the options in ``env`` (written as ``CLAUDE_PLUGIN_OPTION_<NAME>``,
    the way tests express them) and ``options`` to a holder, for claude-code.

    Claude Code's own variables are not read by the plugin any more, so a test's
    option must reach it the way Claude Code's options do. Other suites have no
    options holder; their environment is returned as it is.
    """
    if suite.name != CLAUDE.name:
        return env
    merged = dict(options or {})
    for name in [k for k in env if k.startswith(_OPTION_PREFIX)]:
        merged[name[len(_OPTION_PREFIX) :].lower()] = env.pop(name)
    if merged:
        env.update(holder_env(merged))
    return env


def key_env(suite: Suite, key: str) -> dict:
    """Where a hook of ``suite`` finds the user's Cognee API key."""
    if suite.name == CLAUDE.name:
        return holder_env({"cognee_api_key": key})
    return {"COGNEE_API_KEY": key}


def set_options(monkeypatch, **values) -> None:
    """Give this test's process these options, over the ones it set before."""
    merged = dict(getattr(monkeypatch, "_cognee_options", {}))
    merged.update({k: str(v) for k, v in values.items()})
    monkeypatch._cognee_options = merged
    for name, value in holder_env(merged).items():
        monkeypatch.setenv(name, value)


def set_key(monkeypatch, suite: Suite, key: str) -> None:
    """Give this test's process ``key`` as the user's Cognee API key; "" for none."""
    if suite.name == CLAUDE.name:
        set_options(monkeypatch, cognee_api_key=key)
    elif key:
        monkeypatch.setenv("COGNEE_API_KEY", key)
    else:
        monkeypatch.delenv("COGNEE_API_KEY", raising=False)
