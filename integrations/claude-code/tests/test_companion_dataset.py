import os
import json
import asyncio
import sys
import pathlib
from unittest.mock import patch, MagicMock, AsyncMock

# Add scripts directory to sys.path
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Mock cognee module to avoid ModuleNotFoundError
sys.modules['cognee'] = MagicMock()
sys.modules['cognee.infrastructure'] = MagicMock()
sys.modules['cognee.infrastructure.session'] = MagicMock()
sys.modules['cognee.infrastructure.session.get_session_manager'] = MagicMock()

# Import the modules we need to test
import scripts.config as config
import scripts._plugin_common as plugin_common
import scripts._recall_http as recall_http

def test_env_var_boolean_coercion():
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "false"
    cfg = config.load_config()
    assert str(cfg.get("session_companion_dataset", "")).lower() not in ("true", "1", "yes")

    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    cfg = config.load_config()
    assert str(cfg.get("session_companion_dataset", "")).lower() in ("true", "1", "yes")

    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "1"
    cfg = config.load_config()
    assert str(cfg.get("session_companion_dataset", "")).lower() in ("true", "1", "yes")


@patch("scripts.config.ensure_dataset_ready", new_callable=AsyncMock)
@patch("scripts.config._load_bridge_state", return_value={})
@patch("cognee.infrastructure.session.get_session_manager.get_session_manager")
@patch("cognee.remember", new_callable=AsyncMock)
def test_local_mode_remember_routes_to_companion(mock_remember, mock_gsm, mock_load, mock_ensure):
    # Setup mocks
    mock_session = MagicMock()
    mock_session.is_available = True
    mock_session.get_session = AsyncMock(return_value=[{"question": "test", "answer": "test"}])
    mock_session.get_agent_trace_feedback = AsyncMock(return_value=[])
    mock_gsm.return_value = mock_session
    
    user = MagicMock()
    user.id = "user1"
    
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    
    # Run async function
    asyncio.run(config.persist_session_cache_to_graph("my_dataset", "session1", user))
    
    # Assert
    mock_ensure.assert_called_with("my_dataset-agent_sessions", user)
    mock_remember.assert_called_once()
    kwargs = mock_remember.call_args[1]
    assert kwargs["dataset_name"] == "my_dataset-agent_sessions"


@patch("scripts.config.ensure_dataset_ready", new_callable=AsyncMock)
@patch("scripts.config._load_bridge_state", return_value={})
@patch("cognee.infrastructure.session.get_session_manager.get_session_manager")
@patch("cognee.remember", new_callable=AsyncMock)
def test_companion_dataset_name_resolution(mock_remember, mock_gsm, mock_load, mock_ensure):
    # Setup mocks
    mock_session = MagicMock()
    mock_session.is_available = True
    mock_session.get_session = AsyncMock(return_value=[{"question": "test", "answer": "test"}])
    mock_session.get_agent_trace_feedback = AsyncMock(return_value=[])
    mock_gsm.return_value = mock_session
    
    user = MagicMock()
    user.id = "user1"
    
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    
    # Should not double-suffix if dataset is "agent_sessions"
    asyncio.run(config.persist_session_cache_to_graph("agent_sessions", "session1", user))
    
    mock_ensure.assert_called_with("agent_sessions", user)
    kwargs = mock_remember.call_args[1]
    assert kwargs["dataset_name"] == "agent_sessions"


@patch("scripts._plugin_common._post_remember_document")
@patch("urllib.request.urlopen")
def test_cloud_mode_remember_routes_to_companion(mock_urlopen, mock_post):
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    
    # Setup mocks
    mock_response = MagicMock()
    mock_response.status = 200
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    mock_post.return_value = {"ok": True}
    
    # Needs to bypass other checks in the function
    with patch("scripts._plugin_common._local_api_url", return_value="http://local"), \
         patch("scripts._plugin_common._backend_reachable", return_value=True), \
         patch("scripts._plugin_common._api_key", return_value="key"), \
         patch("scripts._plugin_common._format_cached_bridge_document", return_value=("qa_doc", "trace_doc")), \
         patch("scripts._plugin_common._bridge_file", return_value="bridge.json"), \
         patch("scripts._plugin_common._load_json_file", return_value={}):
         
        plugin_common.persist_session_cache_to_graph_via_http("my_dataset", "session1")
    
    # Assert
    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "http://local/api/v1/datasets"
    assert json.loads(req.data.decode("utf-8")) == {"name": "my_dataset-agent_sessions"}
    
    # Check _post_remember_document was called with the companion dataset
    mock_post.assert_any_call("http://local", "key", "my_dataset-agent_sessions", "qa_doc", "user_sessions_from_cache", 30.0)


@patch("scripts._plugin_common._post_remember_document")
@patch("urllib.request.urlopen")
def test_companion_provisioning_fallback(mock_urlopen, mock_post):
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    
    # Setup mocks to simulate provisioning failure
    mock_response = MagicMock()
    mock_response.status = 500
    mock_urlopen.return_value.__enter__.return_value = mock_response
    
    mock_post.return_value = {"ok": True}
    
    # Needs to bypass other checks in the function
    with patch("scripts._plugin_common._local_api_url", return_value="http://local"), \
         patch("scripts._plugin_common._backend_reachable", return_value=True), \
         patch("scripts._plugin_common._api_key", return_value="key"), \
         patch("scripts._plugin_common._format_cached_bridge_document", return_value=("qa_doc", "trace_doc")), \
         patch("scripts._plugin_common._bridge_file", return_value="bridge.json"), \
         patch("scripts._plugin_common._load_json_file", return_value={}), \
         patch("scripts._plugin_common.hook_log") as mock_log:
         
        plugin_common.persist_session_cache_to_graph_via_http("my_dataset", "session1")
    
    # Assert
    mock_log.assert_any_call("companion_provisioning_failed", {"error": "HTTP 500"})
    
    # Check fallback to primary dataset
    mock_post.assert_any_call("http://local", "key", "my_dataset", "qa_doc", "user_sessions_from_cache", 30.0)


class MockResponse:
    def read(self):
        return b"[]"
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

def test_recall_queries_both_datasets():
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    
    mock_opener = MagicMock()
    mock_opener.return_value = MockResponse()
    
    recall_http.do_recall("http://local", "key", "test", "session1", "auto", 5, "my_dataset", opener=mock_opener)
    
    # Assert
    mock_opener.assert_called_once()
    req = mock_opener.call_args[0][0]
    body = json.loads(req.data.decode("utf-8"))
    assert "datasets" in body
    assert body["datasets"] == ["my_dataset", "my_dataset-agent_sessions"]

