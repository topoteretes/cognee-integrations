"""Runtime posture for embedding cognee as a fast single-user local engine.

Every setting here was earned the hard way while building the Spotlight
integration; apply them before the first ``import cognee`` (or export them in
the launcher script) whenever an integration runs cognee in-process for one
user:

- ``DATA/SYSTEM/CACHE_ROOT_DIRECTORY`` — **force** isolated storage. Sharing a
  database directory with another cognee install (a dev checkout, another
  tool) fails at migration time with unknown alembic revisions, and developer
  shells often export these globally — so defaults with ``:-`` are not enough.
- ``CACHING=false`` — cognee's session memory rewrites every query through an
  LLM round-trip before retrieval. Right for a chat agent, ruinous for
  interactive search (seconds per keystroke).
- ``ENABLE_BACKEND_ACCESS_CONTROL=false`` — with multi-tenant access control
  on (cognee's default), every search opens and tears down a per-dataset DB
  worker process (~4s/query). Single-user mode keeps one warm engine for the
  process lifetime (~0.25s/query). Only valid when the process serves exactly
  one principal; note the posture must match at *index* time and *search*
  time — data written under one posture is not visible under the other.

With the single warm engine owning the store, run the cognify pipeline
**in the same process** — a separate cognify process would fight the engine
for the store lock.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union


def single_user_runtime(root: Union[str, Path], apply: bool = True) -> dict[str, str]:
    """Environment for a fast, isolated, single-user in-process cognee.

    Returns the variables; also applies them to ``os.environ`` unless
    ``apply=False``. Must run before cognee is first imported.
    """
    base = Path(root).expanduser()
    env = {
        "DATA_ROOT_DIRECTORY": str(base / "data"),
        "SYSTEM_ROOT_DIRECTORY": str(base / "system"),
        "CACHE_ROOT_DIRECTORY": str(base / "cache"),
        "CACHING": "false",
        "ENABLE_BACKEND_ACCESS_CONTROL": "false",
    }
    if apply:
        os.environ.update(env)
    return env
