def test_imports():
    from dify_plugin import DifyPluginEnv, Plugin

    assert Plugin is not None
    assert DifyPluginEnv is not None
