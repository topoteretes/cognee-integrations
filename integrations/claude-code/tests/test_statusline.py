"""Unit tests for the compact statusline functionality in cognee_statusline_render.py.

We patch the path constants and configuration loaders to test all resolution scenarios:
- Local vs Cloud mode
- URL fallback
- API key sources (env vs cached files)
- Version sources (server-ready vs venv-ready)
"""

import json
import os
import pathlib
import sys
import tempfile
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import cognee_statusline_render as csr

# Create an isolated temporary directory for plugin configuration paths
_TMP = tempfile.mkdtemp(prefix="cognee-status-test-")
csr._SHARED_ROOT = pathlib.Path(_TMP)
csr._CONFIG_PATH = csr._SHARED_ROOT / "claude-code" / "config.json"
csr._SERVER_READY_PATH = csr._SHARED_ROOT / "server-ready.json"
csr._BREAKER_PATH = csr._SHARED_ROOT / "recall-breaker.json"

@pytest.fixture(autouse=True)
def clean_state():
    # Reset files in temp directory
    for p in csr._SHARED_ROOT.rglob("*"):
        if p.is_file():
            p.unlink()
    # Reset environments
    if "COGNEE_BASE_URL" in os.environ:
        del os.environ["COGNEE_BASE_URL"]
    if "COGNEE_API_KEY" in os.environ:
        del os.environ["COGNEE_API_KEY"]
    yield

def test_resolved_default_local():
    assert csr._active_mode() == "local"
    assert csr._active_url() == "http://localhost:8011"
    assert csr._active_key() == ""
    assert csr._active_version() == ""

def test_resolved_env_vars():
    os.environ["COGNEE_BASE_URL"] = "https://my-custom.cognee.ai"
    os.environ["COGNEE_API_KEY"] = "my-env-api-key"
    
    assert csr._active_mode() == "cloud"
    assert csr._active_url() == "https://my-custom.cognee.ai"
    assert csr._active_key() == "my-env-api-key"

def test_resolved_config_file():
    csr._CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    csr._CONFIG_PATH.write_text(json.dumps({
        "base_url": "https://config-url.ai",
        "api_key": "config-api-key"
    }), encoding="utf-8")
    
    assert csr._active_mode() == "cloud"
    assert csr._active_url() == "https://config-url.ai"
    assert csr._active_key() == "config-api-key"

def test_resolved_cached_api_key():
    csr._SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    (csr._SHARED_ROOT / "api_key.json").write_text(json.dumps({
        "api_key": "cached-api-key"
    }), encoding="utf-8")
    
    assert csr._active_key() == "cached-api-key"

def test_resolved_version_server_ready():
    csr._SERVER_READY_PATH.parent.mkdir(parents=True, exist_ok=True)
    csr._SERVER_READY_PATH.write_text(json.dumps({
        "version": "1.3.0-server"
    }), encoding="utf-8")
    
    assert csr._active_version() == "1.3.0-server"

def test_resolved_version_venv_ready():
    csr._SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    (csr._SHARED_ROOT / "venv-ready.json").write_text(json.dumps({
        "cognee_version": "1.2.2.dev0"
    }), encoding="utf-8")
    
    assert csr._active_version() == "1.2.2.dev0"

def test_main_compact(capsys):
    os.environ["COGNEE_BASE_URL"] = "http://localhost:8011"
    os.environ["COGNEE_API_KEY"] = "test-key"
    csr._SHARED_ROOT.mkdir(parents=True, exist_ok=True)
    (csr._SHARED_ROOT / "venv-ready.json").write_text(json.dumps({
        "cognee_version": "1.2.2.dev0"
    }), encoding="utf-8")
    
    sys.argv = ["cognee_statusline_render.py", "--compact"]
    with pytest.raises(SystemExit) as excinfo:
        csr.main()
        
    assert excinfo.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == "mode=local url=http://localhost:8011 key=test-key version=1.2.2.dev0\n"
