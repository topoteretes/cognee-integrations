"""Per-visitor and global limits on /api/chat and /api/forget, the client IP
behind a proxy, and reading the limits from the environment or a .env file."""

import os
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from cognee_integration_web_widget import server as server_mod
from cognee_integration_web_widget.rate_limit import (
    DAY,
    HOUR,
    RateLimits,
    client_ip,
    env_limit,
    parse_trusted_proxies,
)
from fastapi.testclient import TestClient


class Clock:
    """A clock the test moves by hand, so windows pass without waiting."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def chat_client(fake_client):
    fake_client.recall = AsyncMock(return_value=[])
    server_mod.adapter.client = fake_client
    with TestClient(server_mod.app) as client:
        yield client, fake_client


def use_limits(monkeypatch, clock, **limits):
    monkeypatch.setattr(server_mod, "limits", RateLimits(clock=clock, **limits))


def ask(client, conversation="c1", forwarded=None):
    headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    return client.post(
        "/api/chat", json={"message": "hi", "conversation_id": conversation}, headers=headers
    )


# --- the endpoints -------------------------------------------------------------


def test_one_ip_over_the_hourly_limit_gets_429_and_no_recall(chat_client, monkeypatch, clock):
    client, fake = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=3)

    codes = [ask(client).status_code for _ in range(5)]

    assert codes == [200, 200, 200, 429, 429]
    assert fake.recall.await_count == 3


def test_429_says_which_limit_and_when_to_retry(chat_client, monkeypatch, clock):
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=1)
    ask(client)
    clock.now += 600

    resp = ask(client)

    assert resp.status_code == 429
    body = resp.json()
    assert body["scope"] == "visitor"
    assert body["retry_after"] == 3000
    assert resp.headers["Retry-After"] == "3000"
    assert "50 minutes" in body["message"]


def test_the_limit_is_per_ip_not_per_conversation(chat_client, monkeypatch, clock):
    """A script cannot get round the limit by making up new conversation ids."""
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=2)

    codes = [ask(client, conversation=f"c{i}").status_code for i in range(3)]

    assert codes == [200, 200, 429]


def test_the_daily_limit_applies_under_the_hourly_one(chat_client, monkeypatch, clock):
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=2, per_ip_day=3)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 200
    clock.now += HOUR  # the hourly window is clear again
    assert ask(client).status_code == 200

    resp = ask(client)

    assert resp.status_code == 429
    assert resp.json()["scope"] == "visitor"


def test_the_global_limit_refuses_a_new_ip_too(chat_client, monkeypatch, clock):
    client, fake = chat_client
    use_limits(monkeypatch, clock, global_hour=2)
    monkeypatch.setattr(server_mod, "TRUSTED_PROXIES", "*")
    assert ask(client, forwarded="203.0.113.1").status_code == 200
    assert ask(client, forwarded="203.0.113.2").status_code == 200

    resp = ask(client, forwarded="203.0.113.3")

    assert resp.status_code == 429
    assert resp.json()["scope"] == "global"
    assert fake.recall.await_count == 2


def test_a_limited_client_is_let_back_in_after_the_window(chat_client, monkeypatch, clock):
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=1)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 429

    clock.now += HOUR

    assert ask(client).status_code == 200


def test_requests_refused_while_limited_do_not_extend_the_wait(chat_client, monkeypatch, clock):
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=1)
    ask(client)
    for _ in range(10):
        clock.now += 300
        ask(client)

    clock.now += HOUR - 3000

    assert ask(client).status_code == 200


def test_forget_counts_against_the_visitor_limit_but_not_the_global_one(
    chat_client, monkeypatch, clock
):
    client, fake = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=2, global_hour=1)
    assert client.post("/api/forget", json={"conversation_id": "c1"}).status_code == 200
    assert ask(client).status_code == 200  # the forget left the global hour untouched

    resp = client.post("/api/forget", json={"conversation_id": "c1"})

    assert resp.status_code == 429
    assert resp.json()["scope"] == "visitor"
    assert fake.forget.await_count == 1


def test_the_forget_command_in_chat_is_limited_like_any_message(chat_client, monkeypatch, clock):
    client, fake = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=1)
    ask(client)

    resp = client.post("/api/chat", json={"message": "/forget", "conversation_id": "c1"})

    assert resp.status_code == 429
    fake.forget.assert_not_awaited()


def test_zero_turns_a_limit_off(chat_client, monkeypatch, clock):
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=0, per_ip_day=0, global_hour=0)

    assert all(ask(client).status_code == 200 for _ in range(30))


def test_trusted_proxy_counts_forwarded_ips_separately(chat_client, monkeypatch, clock):
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=1)
    monkeypatch.setattr(server_mod, "TRUSTED_PROXIES", "*")

    assert ask(client, forwarded="203.0.113.1").status_code == 200
    assert ask(client, forwarded="203.0.113.2").status_code == 200
    assert ask(client, forwarded="203.0.113.1").status_code == 429


def test_forwarded_header_is_ignored_without_a_trusted_proxy(chat_client, monkeypatch, clock):
    """Anyone can send X-Forwarded-For; believing it unasked would let one
    client pose as endless visitors."""
    client, _ = chat_client
    use_limits(monkeypatch, clock, per_ip_hour=1)

    assert ask(client, forwarded="203.0.113.1").status_code == 200
    assert ask(client, forwarded="203.0.113.2").status_code == 429


# --- the client IP ---------------------------------------------------------------


def test_client_ip_reads_from_the_right_past_trusted_proxies():
    trusted = parse_trusted_proxies("10.0.0.0/8, 192.0.2.7")
    # The client wrote the leftmost hop itself; the proxies appended the rest.
    forwarded = "1.2.3.4, 198.51.100.9, 10.1.1.1"

    assert client_ip("192.0.2.7", forwarded, trusted) == "198.51.100.9"


def test_client_ip_ignores_the_header_from_an_untrusted_peer():
    trusted = parse_trusted_proxies("10.0.0.0/8")

    assert client_ip("198.51.100.9", "1.2.3.4", trusted) == "198.51.100.9"


def test_client_ip_with_star_takes_the_hop_the_proxy_added():
    assert client_ip("10.0.0.1", "1.2.3.4, 198.51.100.9", "*") == "198.51.100.9"


def test_client_ip_falls_back_to_the_peer_without_a_header():
    assert client_ip("10.0.0.1", "", "*") == "10.0.0.1"


def test_bad_trusted_proxy_value_is_refused():
    with pytest.raises(ValueError, match="WIDGET_TRUSTED_PROXIES"):
        parse_trusted_proxies("10.0.0.0/8, proxy.internal")


# --- configuration ---------------------------------------------------------------


def test_env_limit_defaults_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("WIDGET_RATE_LIMIT_PER_IP_HOUR", raising=False)
    assert env_limit("WIDGET_RATE_LIMIT_PER_IP_HOUR", 20) == 20
    monkeypatch.setenv("WIDGET_RATE_LIMIT_PER_IP_HOUR", " ")
    assert env_limit("WIDGET_RATE_LIMIT_PER_IP_HOUR", 20) == 20


def test_defaults_apply_when_nothing_is_set(monkeypatch):
    for name in (
        "WIDGET_RATE_LIMIT_PER_IP_HOUR",
        "WIDGET_RATE_LIMIT_PER_IP_DAY",
        "WIDGET_RATE_LIMIT_GLOBAL_HOUR",
    ):
        monkeypatch.delenv(name, raising=False)

    limits = RateLimits.from_env()

    assert (limits.ip_hour.limit, limits.ip_day.limit, limits.global_hour.limit) == (20, 100, 500)
    assert (limits.ip_hour.seconds, limits.ip_day.seconds) == (HOUR, DAY)


@pytest.mark.parametrize("value", ["-1", "ten", "2.5"])
def test_env_limit_refuses_a_bad_value_and_names_the_variable(monkeypatch, value):
    monkeypatch.setenv("WIDGET_RATE_LIMIT_GLOBAL_HOUR", value)
    with pytest.raises(ValueError, match="WIDGET_RATE_LIMIT_GLOBAL_HOUR"):
        RateLimits.from_env()


def _limits_seen_by_a_fresh_backend(tmp_path, env):
    """Import the server in a new interpreter, as starting the backend does, and
    report the limits it read. The package reads .env on import, so this cannot
    be done in the test process, which has long since imported it."""
    script = (
        "from cognee_integration_web_widget import server as s;"
        "print(s.limits.ip_hour.limit, s.limits.ip_day.limit, s.limits.global_hour.limit)"
    )
    full_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("WIDGET_RATE_LIMIT_") and k != "WIDGET_ENV_FILE"
    }
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_limits_are_read_from_a_dotenv_file(tmp_path):
    (tmp_path / ".env").write_text(
        "WIDGET_RATE_LIMIT_PER_IP_HOUR=7\nWIDGET_RATE_LIMIT_PER_IP_DAY=70\n"
    )

    result = _limits_seen_by_a_fresh_backend(tmp_path, {})

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["7", "70", "500"]


def test_the_environment_wins_over_the_dotenv_file(tmp_path):
    (tmp_path / ".env").write_text("WIDGET_RATE_LIMIT_PER_IP_HOUR=7\n")

    result = _limits_seen_by_a_fresh_backend(tmp_path, {"WIDGET_RATE_LIMIT_PER_IP_HOUR": "9"})

    assert result.stdout.split()[0] == "9"


def test_a_bad_value_in_dotenv_stops_the_backend_and_names_it(tmp_path):
    (tmp_path / ".env").write_text("WIDGET_RATE_LIMIT_PER_IP_DAY=lots\n")

    result = _limits_seen_by_a_fresh_backend(tmp_path, {})

    assert result.returncode != 0
    assert "WIDGET_RATE_LIMIT_PER_IP_DAY" in result.stderr
