"""A configured dataset pins recall, as in the hook-family plugins (SDK-1393).

``COGNEE_PLUGIN_DATASET`` (or the setup wizard's ``dataset``) naming a dataset
other than the default restricts recall to it, reason ``configured_dataset``;
"auto" means the default dataset with auto recall. The conversation's own
choice wins either way: ``action='auto'`` lifts the configured pin for that
conversation, and picking a dataset pins it.
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import FakeBackend, fake_backend  # noqa: E402
from cognee_integration_hermes import config as config_mod  # noqa: E402
from cognee_integration_hermes import dataset_overrides  # noqa: E402
from cognee_integration_hermes import provider as provider_mod  # noqa: E402

_ENV = {"COGNEE_BASE_URL": "", "COGNEE_SERVICE_URL": "", "COGNEE_EMBEDDED": "true"}


def _initialize(fake, dataset=None, session_id="s-1", routes=True, **env):
    """The real initialize() in embedded mode against ``fake``."""
    fake.routes_datasets = lambda **kw: routes
    provider = provider_mod.CogneeMemoryProvider(backend=fake)
    if dataset is not None:
        env["COGNEE_PLUGIN_DATASET"] = dataset
    with mock.patch.dict("os.environ", {**_ENV, **env}, clear=False):
        provider.initialize(session_id)
    return provider


def _call(provider, tool, args):
    return json.loads(provider.handle_tool_call(tool, args))


def _graph_datasets(provider, fake):
    fake.calls.clear()
    _call(provider, "cognee_recall", {"query": "what did we decide?"})
    (kwargs,) = [k for k in fake.kwargs_for("recall") if k["scope"] == ["graph"]]
    return kwargs["datasets"]


def test_a_configured_dataset_pins_recall():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        assert _graph_datasets(provider, fake) == ["work"]
        current = _call(provider, "cognee_switch_dataset", {"action": "current"})
    assert (current["dataset"], current["recall"], current["reason"]) == (
        "work",
        "pinned",
        "configured_dataset",
    )


@pytest.mark.parametrize("dataset", [None, "agent_sessions", " agent_sessions "])
def test_the_default_dataset_leaves_recall_on_auto(dataset):
    with fake_backend() as fake:
        provider = _initialize(fake, dataset)
        assert _graph_datasets(provider, fake) is None
        current = _call(provider, "cognee_switch_dataset", {"action": "current"})
    assert current["recall"] == "auto"
    assert "reason" not in current


def test_the_comparison_uses_the_sanitized_name():
    # "agent.sessions" resolves to the default dataset, so it does not pin;
    # "my.work" resolves to "my_work", which does.
    with fake_backend() as fake:
        provider = _initialize(fake, "agent.sessions")
        assert provider._dataset == "agent_sessions"
        assert _graph_datasets(provider, fake) is None
    with fake_backend() as fake:
        provider = _initialize(fake, "my.work")
        assert _graph_datasets(provider, fake) == ["my_work"]


@pytest.mark.parametrize("value", ["auto", " AUTO ", "Auto"])
def test_a_configured_auto_means_the_default_dataset_with_auto_recall(value):
    with fake_backend() as fake:
        provider = _initialize(fake, value)
        assert provider._dataset == config_mod.DEFAULT_DATASET
        assert _graph_datasets(provider, fake) is None
        _call(provider, "cognee_remember", {"content": "We ship on Fridays."})
        assert fake.wait("remember_permanent")
        saved = fake.kwargs_for("remember_permanent")[0]["dataset"]
    assert saved == config_mod.DEFAULT_DATASET
    assert config_mod.load_config()["dataset"] == config_mod.DEFAULT_DATASET


def test_a_configured_auto_from_the_config_file_is_the_default(tmp_path):
    (tmp_path / "cognee.json").write_text(json.dumps({"dataset": "auto"}))
    with mock.patch.object(config_mod, "resolve_hermes_home", return_value=tmp_path):
        assert config_mod.load_config()["dataset"] == config_mod.DEFAULT_DATASET


def test_session_auto_lifts_the_configured_pin_and_survives_a_resume():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        out = _call(provider, "cognee_switch_dataset", {"action": "auto"})
        assert (out["reason"], out["recall"], out["lifted"]) == (
            "recall_auto",
            "auto",
            "configured_dataset",
        )
        assert _graph_datasets(provider, fake) is None
        # Saves stay in the configured dataset.
        assert provider._dataset == "work"
        resumed = _initialize(FakeBackend(), "work")
    assert dataset_overrides.load_override("s-1")["recall"] == "auto"
    assert resumed._recall_pinned is False


def test_a_new_conversation_gets_the_configured_pin_back():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, "cognee_switch_dataset", {"action": "auto"})
        provider.on_session_switch("s-2")
        current = _call(provider, "cognee_switch_dataset", {"action": "current"})
    assert current["reason"] == "configured_dataset"


def test_reset_goes_back_to_the_configured_pin():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, "cognee_switch_dataset", {"action": "auto"})
        out = _call(provider, "cognee_switch_dataset", {"action": "reset"})
        current = _call(provider, "cognee_switch_dataset", {"action": "current"})
    assert (out["reason"], out["recall"], out["recall_reason"]) == (
        "recall_reset",
        "pinned",
        "configured_dataset",
    )
    assert current["reason"] == "configured_dataset"
    assert dataset_overrides.load_override("s-1")["recall"] == (
        dataset_overrides.RECALL_FOLLOWS_CONFIG
    )


def test_reset_after_a_switch_reports_the_configured_pin():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, "cognee_switch_dataset", {"action": "switch", "dataset": "other"})
        out = _call(provider, "cognee_switch_dataset", {"action": "reset"})
    assert out["switched"] is True
    assert (out["dataset"], out["recall"], out["recall_reason"]) == (
        "work",
        "pinned",
        "configured_dataset",
    )
    assert "note" not in out


def test_picking_the_configured_dataset_records_the_pick():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        out = _call(provider, "cognee_switch_dataset", {"action": "switch", "dataset": "work"})
        current = _call(provider, "cognee_switch_dataset", {"action": "current"})
    assert out["reason"] == "already_active"
    assert current["reason"] == "dataset_picked"
    assert dataset_overrides.load_override("s-1")["recall"] == "pinned"


def test_a_pre_sdk_1393_entry_on_the_configured_dataset_follows_the_config():
    dataset_overrides._save({"s-1": {"dataset": "work", "counter": 2}})
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
    assert provider._recall_pin_reason == "configured_dataset"
    assert provider._session_cognee_id.endswith("__2")


def test_an_unpinned_conversation_on_an_old_server_reports_why():
    with fake_backend() as fake:
        provider = _initialize(fake, routes=False)
        current = _call(provider, "cognee_switch_dataset", {"action": "current"})
    assert (current["recall"], current["reason"]) == ("pinned", "server_cannot_route")


def test_the_credit_names_the_configured_dataset(monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")
    with fake_backend() as fake:
        fake.results["recall"] = [{"text": "Mika likes tennis.", "source": "graph"}]
        provider = _initialize(fake, "work")
        tool = _call(provider, "cognee_recall", {"query": "mika"})
    assert "based on dataset work" in tool["credit"]
