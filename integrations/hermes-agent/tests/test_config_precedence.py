"""Config precedence tests for the Cognee Hermes plugin (#3559).

Documents the ACTUAL current behavior, not an idealized one: hermes builds the
config dict from environment variables first, then merges HERMES_HOME/cognee.json
on top via dict.update() -- so the **config file wins over env** here. See
load_config() in cognee_integration_hermes/config.py. This file only pins that
behavior down; it changes no runtime logic.
"""

import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_file_overrides_env(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    monkeypatch.setenv("COGNEE_DATASET", "from_env")
    save_config({"dataset": "from_file"}, tmp_path)

    # File is the last layer merged, so it wins over the env base.
    assert load_config(tmp_path)["dataset"] == "from_file"


def test_env_used_when_no_file(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config

    monkeypatch.setenv("COGNEE_DATASET", "from_env")

    # tmp_path has no cognee.json, so the file layer is skipped and env stands.
    assert load_config(tmp_path)["dataset"] == "from_env"


def test_default_when_neither(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config

    monkeypatch.delenv("COGNEE_DATASET", raising=False)

    assert load_config(tmp_path)["dataset"] == "hermes"


def test_base_url_prefers_canonical_over_alias(tmp_path):
    from cognee_integration_hermes.config import load_config

    # COGNEE_BASE_URL is canonical; COGNEE_SERVICE_URL is a deprecated alias.
    env = {"COGNEE_BASE_URL": "https://canonical", "COGNEE_SERVICE_URL": "https://legacy"}
    with mock.patch.dict("os.environ", env, clear=False):
        assert load_config(tmp_path)["service_url"] == "https://canonical"


def test_empty_string_overrides_plain_field(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    # Plain string field: the file merge drops only None (not ""), so an empty
    # string in the config file DOES override the env/default layer.
    monkeypatch.delenv("COGNEE_DATASET", raising=False)
    save_config({"dataset": ""}, tmp_path)

    assert load_config(tmp_path)["dataset"] == ""


def test_empty_string_rejected_by_coerced_field(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    # Coerced fields run through str_to_int(), where "" fails to parse and falls
    # back to the default -- so an empty string does NOT override here, unlike a
    # plain string field.
    monkeypatch.delenv("COGNEE_TOP_K", raising=False)
    monkeypatch.delenv("COGNEE_LOCAL_PORT", raising=False)
    save_config({"top_k": "", "local_port": ""}, tmp_path)

    config = load_config(tmp_path)
    assert config["top_k"] == 5
    assert config["local_port"] == 8000


def test_local_port_clamped(tmp_path, monkeypatch):
    from cognee_integration_hermes.config import load_config, save_config

    monkeypatch.delenv("COGNEE_LOCAL_PORT", raising=False)
    save_config({"local_port": 999999}, tmp_path)

    assert load_config(tmp_path)["local_port"] == 65535
