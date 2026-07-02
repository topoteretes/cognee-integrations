from cognee_integration_aider.config import AiderCogneeConfig
from cognee_integration_aider.session import build_session_id, project_id_from_path


def test_project_id_is_stable_for_path(tmp_path):
    project = tmp_path / "my-service"
    project.mkdir()
    (project / ".git").mkdir()

    assert project_id_from_path(project) == project_id_from_path(project)
    assert project_id_from_path(project).startswith("my-service-")


def test_build_session_id_uses_config_project_id():
    config = AiderCogneeConfig(session_prefix="aider", project_id="api")

    assert build_session_id(config) == "aider:api:default"
    assert build_session_id(config, session_id="feature/login") == "aider:api:feature-login"
