"""The recall mode: configured, chosen by a conversation, and stored (SDK-1393).

``recall_mode`` (``COGNEE_PLUGIN_RECALL_MODE``, or the setup wizard's
``recall_mode`` in cognee.json) is "auto" by default for everyone: graph recall
names no dataset and the server picks. "pinned" keeps recall on the active
dataset (reason ``configured_pin``). The configured dataset alone never pins,
and ``COGNEE_PLUGIN_DATASET=auto`` only means the default dataset. The
conversation's own choice wins: ``action='auto'``, ``action='pin'`` or a switch;
``reset`` goes back to the configured dataset and mode. A mode chosen without a
switch is stored as the mode only, so it applies to whatever dataset is active.
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


def _initialize(fake, dataset=None, session_id="s-1", recall_mode=None, **env):
    """The real initialize() in embedded mode against ``fake``."""
    provider = provider_mod.CogneeMemoryProvider(backend=fake)
    if dataset is not None:
        env["COGNEE_PLUGIN_DATASET"] = dataset
    if recall_mode is not None:
        env["COGNEE_PLUGIN_RECALL_MODE"] = recall_mode
    with mock.patch.dict("os.environ", {**_ENV, **env}, clear=False):
        provider.initialize(session_id)
    return provider


def _call(provider, args):
    return json.loads(provider.handle_tool_call("cognee_switch_dataset", args))


def _graph_datasets(provider, fake):
    fake.calls.clear()
    provider.handle_tool_call("cognee_recall", {"query": "what did we decide?"})
    (kwargs,) = [k for k in fake.kwargs_for("recall") if k["scope"] == ["graph"]]
    return kwargs["datasets"]


# -- the configured mode -------------------------------------------------------


@pytest.mark.parametrize("dataset", [None, "agent_sessions", "work", "my.work"])
def test_recall_is_auto_by_default_whatever_the_dataset(dataset):
    with fake_backend() as fake:
        provider = _initialize(fake, dataset)
        assert _graph_datasets(provider, fake) is None
        current = _call(provider, {"action": "current"})
    assert current["recall"] == "auto"
    assert "reason" not in current


def test_a_configured_pinned_mode_pins_recall_to_the_configured_dataset():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        assert _graph_datasets(provider, fake) == ["work"]
        current = _call(provider, {"action": "current"})
    assert (current["dataset"], current["recall"], current["reason"]) == (
        "work",
        "pinned",
        "configured_pin",
    )


def test_a_configured_pinned_mode_pins_the_default_dataset_too():
    with fake_backend() as fake:
        provider = _initialize(fake, recall_mode=" Pinned ")
        assert _graph_datasets(provider, fake) == [config_mod.DEFAULT_DATASET]


@pytest.mark.parametrize("value", ["", "bogus", "pin", "true", "AUTO"])
def test_an_unknown_recall_mode_is_auto(value):
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode=value)
        assert _graph_datasets(provider, fake) is None
    with mock.patch.dict("os.environ", {"COGNEE_PLUGIN_RECALL_MODE": value}):
        assert config_mod.load_config()["recall_mode"] == "auto"


def test_cognee_json_sets_the_mode_over_the_env(tmp_path):
    # As for every key, cognee.json layers over the environment.
    (tmp_path / "cognee.json").write_text(json.dumps({"recall_mode": "pinned"}))
    with (
        mock.patch.object(config_mod, "resolve_hermes_home", return_value=tmp_path),
        mock.patch.dict("os.environ", {"COGNEE_PLUGIN_RECALL_MODE": "auto"}),
    ):
        assert config_mod.load_config()["recall_mode"] == "pinned"
    (tmp_path / "cognee.json").write_text(json.dumps({"recall_mode": "auto"}))
    with (
        mock.patch.object(config_mod, "resolve_hermes_home", return_value=tmp_path),
        mock.patch.dict("os.environ", {"COGNEE_PLUGIN_RECALL_MODE": "pinned"}),
    ):
        assert config_mod.load_config()["recall_mode"] == "auto"


def test_the_env_sets_the_mode_without_cognee_json(tmp_path):
    with (
        mock.patch.object(config_mod, "resolve_hermes_home", return_value=tmp_path),
        mock.patch.dict("os.environ", {"COGNEE_PLUGIN_RECALL_MODE": "pinned"}),
    ):
        assert config_mod.load_config()["recall_mode"] == "pinned"


@pytest.mark.parametrize("value", ["auto", " AUTO ", "Auto"])
def test_a_configured_auto_dataset_is_the_default_and_leaves_the_mode_alone(value):
    with fake_backend() as fake:
        provider = _initialize(fake, value)
        assert provider._dataset == config_mod.DEFAULT_DATASET
        assert _graph_datasets(provider, fake) is None
        provider.handle_tool_call("cognee_remember", {"content": "We ship on Fridays."})
        assert fake.wait("remember_permanent")
        saved = fake.kwargs_for("remember_permanent")[0]["dataset"]
    assert saved == config_mod.DEFAULT_DATASET
    # "auto" as the dataset says nothing about the mode: pinned still pins.
    with fake_backend() as fake:
        provider = _initialize(fake, value, recall_mode="pinned")
        assert _graph_datasets(provider, fake) == [config_mod.DEFAULT_DATASET]


def test_a_configured_auto_from_the_config_file_is_the_default(tmp_path):
    (tmp_path / "cognee.json").write_text(json.dumps({"dataset": "auto"}))
    with mock.patch.object(config_mod, "resolve_hermes_home", return_value=tmp_path):
        assert config_mod.load_config()["dataset"] == config_mod.DEFAULT_DATASET


def test_the_config_schema_offers_the_recall_mode():
    schema = provider_mod.CogneeMemoryProvider(backend=FakeBackend()).get_config_schema()
    (entry,) = [row for row in schema if row["key"] == "recall_mode"]
    assert entry["default"] == "auto"
    assert entry["choices"] == ["auto", "pinned"]
    assert entry["env_var"] == "COGNEE_PLUGIN_RECALL_MODE"
    assert "pinned keeps recall on the active dataset" in entry["description"]


@pytest.mark.parametrize(
    ("current", "answer", "saved"),
    [
        ({}, "", "auto"),
        ({"recall_mode": "pinned"}, "", "pinned"),
        ({}, "pinned", "pinned"),
        ({"recall_mode": "pinned"}, "auto", "auto"),
        ({}, "nonsense", "auto"),
    ],
)
def test_the_setup_wizard_asks_for_the_recall_mode(current, answer, saved):
    written = {}

    def prompt(label, default=""):
        if label.startswith("Recall mode"):
            return answer or default
        return {"Mode": "remote"}.get(label, default)

    with (
        mock.patch.object(provider_mod, "load_config", return_value=current),
        mock.patch.object(provider_mod, "_prompt", side_effect=prompt),
        mock.patch.object(provider_mod, "_prompt_secret", return_value=""),
        mock.patch.object(provider_mod, "_prompt_bool", side_effect=lambda label, default: default),
        mock.patch.object(provider_mod.runtime_env, "local_ready", return_value=True),
        mock.patch.object(
            provider_mod,
            "save_plugin_config",
            side_effect=lambda values, home: written.update(values),
        ),
        mock.patch.object(provider_mod, "save_env_vars"),
    ):
        provider_mod.CogneeMemoryProvider(backend=FakeBackend()).post_setup("/tmp/hh", {})
    assert written["recall_mode"] == saved


# -- the conversation's own choice ---------------------------------------------


def test_conversation_auto_lifts_a_configured_pin_and_survives_a_resume():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        out = _call(provider, {"action": "auto"})
        assert (out["reason"], out["recall"], out["lifted"]) == (
            "recall_auto",
            "auto",
            "configured_pin",
        )
        assert _graph_datasets(provider, fake) is None
        # Saves stay in the configured dataset.
        assert provider._dataset == "work"
        resumed = _initialize(FakeBackend(), "work", recall_mode="pinned")
    assert dataset_overrides.load_override("s-1")["recall"] == "auto"
    assert resumed._recall_pinned is False


def test_conversation_auto_wins_over_a_later_configured_pin():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        out = _call(provider, {"action": "auto"})
        assert out["reason"] == "already_auto"
        resumed = _initialize(fake, "work", recall_mode="pinned")
        assert _graph_datasets(resumed, fake) is None
    stored = dataset_overrides.load_override("s-1")
    assert stored["recall"] == "auto"
    assert "dataset" not in stored


def test_a_new_conversation_gets_the_configured_pin_back():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        _call(provider, {"action": "auto"})
        provider.on_session_switch("s-2")
        current = _call(provider, {"action": "current"})
    assert current["reason"] == "configured_pin"


def test_reset_goes_back_to_the_configured_mode():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        _call(provider, {"action": "auto"})
        out = _call(provider, {"action": "reset"})
        current = _call(provider, {"action": "current"})
    assert (out["reason"], out["recall"], out["recall_reason"]) == (
        "recall_reset",
        "pinned",
        "configured_pin",
    )
    assert current["reason"] == "configured_pin"
    # Following the config without a switch leaves nothing to store.
    assert dataset_overrides.load_override("s-1") == {}


def test_reset_follows_a_changed_config():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, {"action": "pin"})
        _call(provider, {"action": "reset"})
        resumed = _initialize(fake, "work", recall_mode="pinned")
        assert _graph_datasets(resumed, fake) == ["work"]
        current = _call(resumed, {"action": "current"})
    assert current["reason"] == "configured_pin"


def test_reset_after_a_switch_reports_the_configured_mode():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        _call(provider, {"action": "switch", "dataset": "other"})
        out = _call(provider, {"action": "reset"})
    assert out["switched"] is True
    assert (out["dataset"], out["recall"], out["recall_reason"]) == (
        "work",
        "pinned",
        "configured_pin",
    )
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, {"action": "switch", "dataset": "other"})
        out = _call(provider, {"action": "reset"})
    assert (out["dataset"], out["recall"]) == ("work", "auto")
    assert "recall_reason" not in out


# -- action='pin' --------------------------------------------------------------


def test_pin_pins_the_active_dataset_without_a_new_session():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        fake.calls.clear()
        out = _call(provider, {"action": "pin"})
        assert fake.calls == []
        assert _graph_datasets(provider, fake) == ["work"]
        again = _call(provider, {"action": "pin"})
    assert (out["switched"], out["reason"], out["recall"], out["recall_reason"]) == (
        False,
        "recall_restricted",
        "pinned",
        "dataset_picked",
    )
    assert again["reason"] == "already_active"
    assert provider._session_cognee_id == "hermes_s-1"
    stored = dataset_overrides.load_override("s-1")
    assert stored["recall"] == "pinned"
    assert "dataset" not in stored and "counter" not in stored


def test_pin_wins_over_a_configured_pin_and_a_later_configured_auto():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        out = _call(provider, {"action": "pin"})
        assert (out["reason"], out["recall_reason"]) == ("already_active", "dataset_picked")
        resumed = _initialize(fake, "work")
        assert _graph_datasets(resumed, fake) == ["work"]
        assert _call(resumed, {"action": "current"})["reason"] == "dataset_picked"


def test_after_a_config_dataset_change_a_pin_follows_the_new_active_dataset():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, {"action": "pin"})
        resumed = _initialize(fake, "research")
        assert resumed._dataset == "research"
        assert _graph_datasets(resumed, fake) == ["research"]
        current = _call(resumed, {"action": "current"})
    assert (current["dataset"], current["recall"], current["reason"]) == (
        "research",
        "pinned",
        "dataset_picked",
    )


def test_switching_to_the_dataset_in_use_is_a_pin():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        fake.calls.clear()
        switched = _call(provider, {"action": "switch", "dataset": "work"})
        assert fake.calls == []
    with fake_backend() as fake:
        pinned = _call(_initialize(fake, "work", session_id="s-2"), {"action": "pin"})
    assert switched == pinned
    assert dataset_overrides.load_override("s-1")["recall"] == "pinned"
    assert "dataset" not in dataset_overrides.load_override("s-1")


def test_a_switch_entry_keeps_its_dataset_and_mode():
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
        _call(provider, {"action": "switch", "dataset": "other"})
        _call(provider, {"action": "auto"})
        stored = dataset_overrides.load_override("s-1")
        assert (stored["dataset"], stored["counter"], stored["recall"]) == ("other", 1, "auto")
        resumed = _initialize(fake, "research", recall_mode="pinned")
        assert resumed._dataset == "other"
        assert _graph_datasets(resumed, fake) is None


# -- legacy entries ------------------------------------------------------------


def test_a_pre_sdk_1393_entry_on_the_configured_dataset_follows_the_config():
    dataset_overrides._save({"s-1": {"dataset": "work", "counter": 2}})
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
    assert provider._recall_pin_reason == "configured_pin"
    assert provider._session_cognee_id.endswith("__2")
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
    assert provider._recall_pinned is False


def test_a_pre_sdk_1393_switch_entry_stays_pinned():
    dataset_overrides._save({"s-1": {"dataset": "other", "counter": 2}})
    with fake_backend() as fake:
        provider = _initialize(fake, "work")
    assert (provider._dataset, provider._recall_pin_reason) == ("other", "dataset_picked")


# -- one message per result ----------------------------------------------------


def test_every_result_carries_the_message_for_its_mode_and_reason():
    with fake_backend() as fake:
        provider = _initialize(fake, "work", recall_mode="pinned")
        results = {
            "current_configured": _call(provider, {"action": "current"}),
            "auto": _call(provider, {"action": "auto"}),
            "pin": _call(provider, {"action": "pin"}),
            "switch": _call(provider, {"action": "switch", "dataset": "other"}),
            "reset": _call(provider, {"action": "reset"}),
            "current": _call(provider, {"action": "current"}),
        }
    for name, out in results.items():
        reason = out.get("recall_reason") or out.get("reason", "")
        if out["recall"] == "auto":
            reason = ""
        expected = provider_mod._recall_message(out["recall"], reason, out["dataset"])
        assert out["message"] == expected, name
    assert "configured recall mode" in results["current_configured"]["message"]
    assert "Recall is auto" in results["auto"]["message"]
    assert "own choice" in results["pin"]["message"]
    assert "'other'" in results["switch"]["message"]
    assert "configured recall mode" in results["reset"]["message"]


def test_the_messages_differ_by_mode_and_reason():
    auto = provider_mod._recall_message("auto", "", "work")
    picked = provider_mod._recall_message("pinned", "dataset_picked", "work")
    configured = provider_mod._recall_message("pinned", "configured_pin", "work")
    assert len({auto, picked, configured}) == 3
    for text in (auto, picked, configured):
        assert text.startswith("Saves go to dataset 'work'.")


def test_the_credit_names_the_dataset_when_the_config_pins(monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "on")
    with fake_backend() as fake:
        fake.results["recall"] = [{"text": "Mika likes tennis.", "source": "graph"}]
        provider = _initialize(fake, "work", recall_mode="pinned")
        tool = json.loads(provider.handle_tool_call("cognee_recall", {"query": "mika"}))
    assert "based on dataset work" in tool["credit"]
