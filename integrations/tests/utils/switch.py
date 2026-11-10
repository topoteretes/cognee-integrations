"""Calling a plugin's ``switch-dataset.py`` across its two generations.

The claude-code switch only starts the sync of the session it leaves and never
waits for it, so it takes no ``force``; the other plugins' switches still run a
blocking pre-switch sync and require ``force`` to say whether to push past a
failed one.
"""

from __future__ import annotations

import inspect


def waits_for_sync(switch) -> bool:
    """True for a switch that still blocks on its pre-switch sync."""
    return "force" in inspect.signature(switch._switch).parameters


def run_switch(switch, host_key: str, rec: dict, target: str, **kwargs):
    """``switch._switch`` with ``force=False`` wherever that switch takes it."""
    if waits_for_sync(switch):
        kwargs.setdefault("force", False)
    return switch._switch(host_key, rec, target, **kwargs)
