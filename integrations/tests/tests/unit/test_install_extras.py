"""Tests for the install-extras detection in ``session-start.py`` (#232).

A bare ``cognee==<pin>`` install carries none of Postgres/Neo4j/Ollama/
fastembed's drivers, so the plugins' local server crashed on first use of any
non-default backend. The fix has two halves, both asserted here for BOTH
suites (claude-code and codex share one venv, so they must agree):

1. ``_detect_required_extras`` / ``_cognee_install_spec`` — read the same
   provider env vars cognee's own config classes read and feed exactly the
   needed extras to the install.
2. ``_venv_missing_extras`` — ground-truth probe of the venv for one sentinel
   distribution per required extra, gating skip-at-pin: the venv being at the
   pinned version says nothing about drivers when a provider was configured
   after the venv was built.

Migrated from claude-code/tests/test_install_extras.py (PR #271), whose
``ss is None`` guard reported skipped tests as PASS and whose module load read
the developer's real state; the isolated ``hook_module`` import supplies a
temp HOME. The provider env vars are NOT in the harness's scrubbed prefixes
(COGNEE_*/LLM_* ...), so the fixture clears them explicitly — a developer's
exported DB_PROVIDER must never leak into an assertion.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROVIDER_ENV_VARS = (
    "DB_PROVIDER",
    "VECTOR_DB_PROVIDER",
    "GRAPH_DATABASE_PROVIDER",
    "EMBEDDING_PROVIDER",
    "LLM_PROVIDER",
)


@pytest.fixture
def ss(suite, hook_module, monkeypatch):
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return hook_module(suite, "session-start.py")


# --- _detect_required_extras / _cognee_install_spec ---------------------------


def test_no_providers_configured_needs_no_extras(ss):
    assert ss._detect_required_extras() == []
    assert ss._cognee_install_spec() == f"cognee=={ss._PINNED_COGNEE_VERSION}"


def test_postgres_and_neo4j_detected(ss, monkeypatch):
    monkeypatch.setenv("DB_PROVIDER", "postgres")
    monkeypatch.setenv("GRAPH_DATABASE_PROVIDER", "neo4j")
    extras = ss._detect_required_extras()
    assert "postgres-binary" in extras
    assert "neo4j" in extras
    joined = ",".join(extras)
    assert ss._cognee_install_spec() == f"cognee[{joined}]=={ss._PINNED_COGNEE_VERSION}"


def test_pgvector_maps_to_postgres_binary_not_a_separate_extra(ss, monkeypatch):
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    assert ss._detect_required_extras() == ["postgres-binary"]


def test_postgres_and_pgvector_together_dont_duplicate_the_extra(ss, monkeypatch):
    monkeypatch.setenv("DB_PROVIDER", "postgres")
    monkeypatch.setenv("VECTOR_DB_PROVIDER", "pgvector")
    assert ss._detect_required_extras() == ["postgres-binary"]


def test_fastembed_and_ollama_detected(ss, monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fastembed")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    extras = ss._detect_required_extras()
    assert "fastembed" in extras
    assert "ollama" in extras


def test_provider_values_are_case_insensitive(ss, monkeypatch):
    monkeypatch.setenv("DB_PROVIDER", "POSTGRES")
    assert "postgres-binary" in ss._detect_required_extras()


def test_unrecognized_provider_value_is_ignored_not_an_error(ss, monkeypatch):
    monkeypatch.setenv("DB_PROVIDER", "sqlite")  # cognee's default — needs no extra
    assert ss._detect_required_extras() == []


def test_every_mapped_extra_has_a_probe_sentinel(ss):
    """The detection map and the probe map must stay in lockstep: an extra the
    detector can request but the probe can't verify would crash the skip-at-pin
    gate with a KeyError on the next cold boot after that provider appears."""
    mapped = {extra for _var, mapping in ss._PROVIDER_EXTRAS for extra in mapping.values()}
    assert mapped <= set(ss._EXTRA_SENTINEL_DISTS)


# --- _venv_missing_extras (the skip-at-pin ground-truth probe) -----------------


def test_no_required_extras_means_nothing_missing_and_no_probe(ss, monkeypatch):
    # With nothing required, the answer is [] before any probe: a subprocess
    # attempt would raise here, and must not be reached.
    def _boom(*args, **kwargs):
        raise AssertionError("probe must not run when no extras are required")

    monkeypatch.setattr(ss.subprocess, "run", _boom)
    assert ss._venv_missing_extras([]) == []


def test_absent_venv_reports_all_required_extras_missing(ss, monkeypatch):
    monkeypatch.setattr(ss, "_VENV_PYTHON", Path("/nonexistent/python"))
    assert ss._venv_missing_extras(["neo4j", "fastembed"]) == ["neo4j", "fastembed"]


def test_probe_separates_present_from_missing(ss, monkeypatch):
    # The running test interpreter is a real "venv": pytest's metadata is
    # visible to it, a made-up distribution's is not.
    monkeypatch.setattr(ss, "_VENV_PYTHON", Path(sys.executable))
    monkeypatch.setattr(
        ss,
        "_EXTRA_SENTINEL_DISTS",
        {"neo4j": "cognee-test-no-such-dist", "fastembed": "pytest"},
    )
    assert ss._venv_missing_extras(["neo4j", "fastembed"]) == ["neo4j"]


def test_probe_failure_fails_soft(ss, monkeypatch):
    """A broken probe must not force an install loop: a venv broken enough to
    fail it also fails the version probe, which the install path handles."""
    monkeypatch.setattr(ss, "_VENV_PYTHON", Path(sys.executable))

    def _boom(*args, **kwargs):
        raise OSError("probe blew up")

    monkeypatch.setattr(ss.subprocess, "run", _boom)
    assert ss._venv_missing_extras(["neo4j"]) == []
