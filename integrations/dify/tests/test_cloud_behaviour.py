"""Behaviour needed for Cognee Cloud tenants: redirects and background builds."""

import httpx
import pytest
from conftest import Runtime, form_fields, invoke, text_of, variables


def test_the_client_follows_a_trailing_slash_redirect(monkeypatch):
    # The cloud gateway answers /api/v1/datasets with a 307 to the trailing-slash URL.
    import tools.cognee_client as cognee_client

    seen = []

    def gateway(request):
        seen.append(str(request.url))
        if request.url.path == "/api/v1/datasets":
            return httpx.Response(307, headers={"location": "https://t.cloud/api/v1/datasets/"})
        return httpx.Response(200, json=[{"id": "ds-1", "name": "notes"}])

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(gateway), **kw)
    )
    with cognee_client.make_client() as client:
        response = client.get("https://t.cloud/api/v1/datasets")
    assert response.status_code == 200
    assert seen == ["https://t.cloud/api/v1/datasets", "https://t.cloud/api/v1/datasets/"]


def test_remember_always_submits_a_background_run():
    # A blocking remember can exceed the cloud gateway's HTTP timeout; the
    # plugin submits in the background and waits itself.
    from tools.remember import build_form

    assert build_form({"text_data": "t", "dataset_name": "n"})["run_in_background"] == "true"


def test_text_item_name_matches_the_server_convention():
    from tools.remember import text_item_name

    # md5("abc") is well known; the server stores raw text as text_<md5>.txt
    assert text_item_name("abc") == "text_900150983cd24fb0d6963f7d28e17f72.txt"


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _Server:
    """Scripted answers for the status routes the wait uses; the last value repeats."""

    def __init__(self, runs, statuses, items=(), summary_error=False, summary_status=409):
        self._runs = list(runs)
        self._statuses = list(statuses)
        self._items = list(items)
        self._summary_error = summary_error
        self._summary_status = summary_status
        self.calls = []

    @staticmethod
    def _step(script):
        return script.pop(0) if len(script) > 1 else script[0]

    def get(self, url, **kwargs):
        self.calls.append(url)
        request = httpx.Request("GET", url)
        if url.endswith("/graph-summary"):
            if self._summary_error:
                return httpx.Response(self._summary_status, json={"error": "nope"}, request=request)
            run = self._step(self._runs)
            ds = kwargs["params"]["dataset_ids"]
            rows = [] if run is None else [{"datasetId": ds, "pipelineRunId": run}]
            return httpx.Response(200, json=rows, request=request)
        if url.endswith("/datasets/status"):
            return httpx.Response(
                200, json={kwargs["params"]["dataset"]: self._step(self._statuses)}, request=request
            )
        if url.endswith("/data"):
            return httpx.Response(200, json=[{"name": n} for n in self._items], request=request)
        if url.endswith("/api/v1/datasets"):
            return httpx.Response(200, json=[{"id": "ds-1", "name": "notes"}], request=request)
        raise AssertionError(url)


def test_wait_rejects_the_previous_run_and_accepts_the_new_one():
    from tools.remember import wait_for_build

    # Old run "r1" still completed; then the new run "r2" appears, runs, completes.
    server = _Server(
        runs=["r1", "r1", "r2", "r2"],
        statuses=["DATASET_PROCESSING_STARTED", "DATASET_PROCESSING_COMPLETED"],
    )
    clock = _Clock()
    status = wait_for_build(server, "http://x", {}, "ds-1", "r1", sleep=clock.sleep, now=clock.now)
    assert status == "completed"
    # status was only consulted once the run id had changed
    assert sum(u.endswith("/datasets/status") for u in server.calls) == 2


def test_wait_on_a_new_dataset_accepts_the_first_run():
    from tools.remember import wait_for_build

    server = _Server(runs=[None, "r1"], statuses=["DATASET_PROCESSING_COMPLETED"])
    clock = _Clock()
    status = wait_for_build(server, "http://x", {}, "ds-1", None, sleep=clock.sleep, now=clock.now)
    assert status == "completed"


def test_wait_reports_errored_runs():
    from tools.remember import wait_for_build

    server = _Server(runs=["r2"], statuses=["DATASET_PROCESSING_ERRORED"])
    assert wait_for_build(server, "http://x", {}, "ds-1", "r1", sleep=lambda s: None) == "errored"


def test_wait_never_guesses_when_nothing_changes():
    from tools.remember import wait_for_build

    # The run id never changes: no evidence, so the result is running, not completed.
    server = _Server(runs=["r1"], statuses=["DATASET_PROCESSING_COMPLETED"])
    clock = _Clock()
    status = wait_for_build(
        server, "http://x", {}, "ds-1", "r1", sleep=clock.sleep, now=clock.now, timeout=30
    )
    assert status == "running"
    assert not any(u.endswith("/datasets/status") for u in server.calls)


def test_wait_tolerates_the_transient_409_from_graph_summary():
    from tools.remember import wait_for_build

    server = _Server(runs=[], statuses=["DATASET_PROCESSING_COMPLETED"], summary_error=True)
    clock = _Clock()
    status = wait_for_build(
        server, "http://x", {}, "ds-1", None, sleep=clock.sleep, now=clock.now, timeout=10
    )
    assert status == "running"


@pytest.mark.parametrize("code", [500, 403, 401])
def test_wait_fails_fast_on_persistent_graph_summary_errors(code):
    from tools.remember import wait_for_build

    server = _Server(runs=[], statuses=[], summary_error=True, summary_status=code)
    clock = _Clock()
    with pytest.raises(httpx.HTTPStatusError):
        wait_for_build(server, "http://x", {}, "ds-1", None, sleep=clock.sleep, now=clock.now)
    assert len(server.calls) == 1  # no polling loop on a real error


def test_wait_explains_a_server_without_graph_summary():
    from tools.remember import wait_for_build

    server = _Server(runs=[], statuses=[], summary_error=True, summary_status=404)
    with pytest.raises(RuntimeError, match="no GET /api/v1/datasets/graph-summary route"):
        wait_for_build(server, "http://x", {}, "ds-1", None, sleep=lambda s: None)


def test_pre_call_lookups():
    from tools.remember import has_text_item, latest_run_id, resolve_dataset_id, text_item_name

    server = _Server(runs=["r9"], statuses=[], items=[text_item_name("same text"), "other.txt"])
    assert resolve_dataset_id(server, "http://x", {}, "notes") == "ds-1"
    assert resolve_dataset_id(server, "http://x", {}, "missing") == ""
    assert latest_run_id(server, "http://x", {}, "ds-1") == "r9"
    assert has_text_item(server, "http://x", {}, "ds-1", "same text") is True
    assert has_text_item(server, "http://x", {}, "ds-1", "new text") is False


def test_summaries_are_honest():
    from tools.remember import summarize

    base = {"dataset_name": "n", "dataset_id": "d"}
    done = {**base, "status": "completed"}
    running = {**base, "status": "running"}
    assert "already remembered" in summarize(done, waited=False, deduplicated=True)
    assert "not ready to recall yet" in summarize(running, waited=True, deduplicated=False)
    assert "Poll GET /api/v1/datasets/status" in summarize(
        running, waited=False, deduplicated=False
    )
    errored = {**base, "status": "errored", "error": "boom"}
    assert "Remember failed" in summarize(errored, waited=True, deduplicated=False)
    assert "ready to recall" in summarize(done, waited=True, deduplicated=False)


def test_remember_invocation_submits_waits_and_reports(fake_cognee, monkeypatch):
    """Submission then polling, as it happens against a cloud tenant."""
    import tools.remember as remember_module
    from tools.remember import RememberTool

    monkeypatch.setattr(remember_module, "POLL_INTERVAL", 0)
    runs = iter(["r1", "r1", "r2"])
    statuses = iter(["DATASET_PROCESSING_COMPLETED"])

    fake_cognee.on("GET", "/api/v1/datasets", json_body=[{"id": "ds-1", "name": "notes"}])
    fake_cognee.on("GET", "/api/v1/datasets/ds-1/data", json_body=[{"name": "other.txt"}])
    fake_cognee.on(
        "GET",
        "/api/v1/datasets/graph-summary",
        lambda r: (200, [{"datasetId": "ds-1", "pipelineRunId": next(runs)}]),
    )
    fake_cognee.on("GET", "/api/v1/datasets/status", lambda r: (200, {"ds-1": next(statuses)}))
    fake_cognee.on(
        "POST",
        "/api/v1/remember",
        json_body={"status": "running", "dataset_id": "ds-1", "dataset_name": "notes"},
    )

    messages = invoke(RememberTool, Runtime(), {"text_data": "new text", "dataset_name": "notes"})

    submitted = form_fields(fake_cognee.calls("POST", "/api/v1/remember")[0])
    assert submitted["run_in_background"] == ["true"]
    assert submitted["raw_data"] == ["new text"]
    assert len(fake_cognee.calls("GET", "/api/v1/datasets/graph-summary")) == 3
    assert len(fake_cognee.calls("GET", "/api/v1/datasets/status")) == 1

    out = variables(messages)
    assert out["status"] == "completed"
    assert out["dataset_id"] == "ds-1"
    assert "ready to recall" in text_of(messages)


def test_remember_invocation_detects_deduplicated_text(fake_cognee):
    from tools.remember import RememberTool, text_item_name

    fake_cognee.on(
        "GET", "/api/v1/datasets/ds-1/data", json_body=[{"name": text_item_name("same")}]
    )
    fake_cognee.on(
        "GET",
        "/api/v1/datasets/graph-summary",
        json_body=[{"datasetId": "ds-1", "pipelineRunId": "r1"}],
    )
    fake_cognee.on(
        "POST",
        "/api/v1/remember",
        json_body={"status": "running", "dataset_id": "ds-1", "dataset_name": "n"},
    )
    messages = invoke(RememberTool, Runtime(), {"text_data": "same", "dataset_id": "ds-1"})
    assert fake_cognee.calls("GET", "/api/v1/datasets/status") == []
    assert variables(messages)["status"] == "completed"
    assert "already remembered" in text_of(messages)


def test_remember_invocation_does_not_wait_in_background_mode(fake_cognee):
    from tools.remember import RememberTool

    fake_cognee.on(
        "POST",
        "/api/v1/remember",
        json_body={"status": "running", "dataset_id": "ds-1", "dataset_name": "n"},
    )
    messages = invoke(
        RememberTool,
        Runtime(),
        {"text_data": "t", "dataset_name": "n", "run_in_background": "true"},
    )
    assert [r.url.path for r in fake_cognee.requests] == ["/api/v1/remember"]
    assert variables(messages)["status"] == "running"
    assert "started in the background" in text_of(messages)
