"""The contact form: the marker in answers, the /api/contact endpoint and the email.

No SMTP server is involved: the endpoint's ``send_email`` is replaced, so the tests
check what would be sent, and to whom, without sending anything.
"""

from unittest.mock import AsyncMock

import pytest
from cognee_integration_web_widget import server as server_mod
from cognee_integration_web_widget.contact import (
    MAX_TRANSCRIPT_MESSAGES,
    SmtpSettings,
    build_email,
    split_contact_marker,
    trim_transcript,
    valid_email,
)

SETTINGS = SmtpSettings(
    to="team@example.com",
    host="smtp.example.com",
    port=587,
    user="bot@example.com",
    password="secret",
    sender="bot@example.com",
    security="starttls",
)

UNSURE = "The docs don't cover this, so the team is the best place to ask."


def _answer_entry(text):
    return {
        "source": "graph",
        "dataset_name": "web:demo:docs",
        "text": (
            f'{text}\n\nEvidence:\n- chunk 1 of document guide.md (data_id: d1, chunk_id: c1): "x"'
        ),
    }


@pytest.fixture
def client(fake_client, monkeypatch):
    from fastapi.testclient import TestClient

    server_mod.adapter.client = fake_client
    monkeypatch.setattr(server_mod, "CONTACT", SETTINGS)
    sent = []
    monkeypatch.setattr(server_mod, "send_email", lambda settings, msg: sent.append(msg))
    with TestClient(server_mod.app) as test_client:
        yield test_client, fake_client, sent


def _contact(test_client, **overrides):
    body = {
        "conversation_id": "c1",
        "visitor_id": "v1",
        "email": "ada@example.com",
        "message": "Still stuck on this.",
        "page_url": "https://docs.example.com/guides/x",
        "transcript": [
            {"role": "user", "text": "How do I do X?"},
            {"role": "assistant", "text": UNSURE},
        ],
    }
    body.update(overrides)
    return test_client.post("/api/contact", json=body)


# --- The marker ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"{UNSURE}\n\n[[contact_form]]",
        f"{UNSURE} [[contact_form]]",
        f"{UNSURE}\n[[ Contact_Form ]]",
        f"{UNSURE}\n[[contact form]]",
    ],
)
def test_the_marker_is_found_and_removed(text):
    assert split_contact_marker(text) == (UNSURE, True)


def test_an_answer_without_the_marker_is_left_alone():
    assert split_contact_marker(UNSURE) == (UNSURE, False)


def test_chat_offers_the_form_and_hides_the_marker(client):
    test_client, fake, _ = client
    fake.recall = AsyncMock(return_value=[_answer_entry(f"{UNSURE}\n\n[[contact_form]]")])
    body = test_client.post(
        "/api/chat", json={"message": "How do I do X?", "conversation_id": "c1"}
    ).json()
    assert body["answer"] == UNSURE
    assert body["contact_form"] is True


def test_chat_never_offers_the_form_unprompted(client):
    test_client, fake, _ = client
    fake.recall = AsyncMock(return_value=[_answer_entry("Cognee stores memory as a graph.")])
    body = test_client.post(
        "/api/chat", json={"message": "What is cognee?", "conversation_id": "c1"}
    ).json()
    assert body["contact_form"] is False


def test_chat_strips_the_marker_but_offers_no_form_when_contact_is_off(client, monkeypatch):
    test_client, fake, _ = client
    monkeypatch.setattr(server_mod, "CONTACT", None)
    fake.recall = AsyncMock(return_value=[_answer_entry(f"{UNSURE}\n\n[[contact_form]]")])
    body = test_client.post(
        "/api/chat", json={"message": "How do I do X?", "conversation_id": "c1"}
    ).json()
    assert body["answer"] == UNSURE
    assert body["contact_form"] is False


# --- /api/contact -----------------------------------------------------------------


def test_contact_emails_the_team_with_the_conversation(client):
    test_client, _, sent = client
    resp = _contact(test_client)
    assert resp.status_code == 200
    assert resp.json() == {"sent": True}
    [msg] = sent
    assert msg["To"] == "team@example.com"
    assert msg["From"] == "bot@example.com"
    # Replying to the email reaches the visitor, not the bot's mailbox.
    assert msg["Reply-To"] == "ada@example.com"
    body = msg.get_content()
    assert "Still stuck on this." in body
    assert "How do I do X?" in body and UNSURE in body
    assert "https://docs.example.com/guides/x" in body
    assert "web:demo:v1:c1" in body


def test_contact_404s_when_not_configured(client, monkeypatch):
    test_client, _, sent = client
    monkeypatch.setattr(server_mod, "CONTACT", None)
    assert _contact(test_client).status_code == 404
    assert sent == []


@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "ada@example",
        "ada@example.com\nBcc: x@evil.com",
        "Ada <ada@example.com>",
    ],
)
def test_contact_refuses_an_address_that_is_not_one_plain_email(client, email):
    test_client, _, sent = client
    resp = _contact(test_client, email=email)
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_email"
    assert sent == []


def test_contact_refuses_an_overlong_message(client):
    test_client, _, sent = client
    resp = _contact(test_client, message="x" * 5001)
    assert resp.status_code == 400
    assert sent == []


def test_contact_reports_a_failed_send_instead_of_500(client, monkeypatch):
    test_client, _, _ = client

    def refuse(settings, msg):
        raise OSError("connection refused")

    monkeypatch.setattr(server_mod, "send_email", refuse)
    resp = _contact(test_client)
    assert resp.status_code == 502
    assert resp.json()["error"] == "send_failed"


def test_contact_is_rate_limited_like_forget(client, monkeypatch):
    from cognee_integration_web_widget.rate_limit import RateLimits

    test_client, _, sent = client
    monkeypatch.setattr(server_mod, "limits", RateLimits(per_ip_hour=1))
    assert _contact(test_client).status_code == 200
    resp = _contact(test_client)
    assert resp.status_code == 429
    assert len(sent) == 1


# --- Pieces ---------------------------------------------------------------------


def test_transcript_keeps_the_latest_messages_within_the_caps():
    many = [{"role": "user", "text": f"q{i}"} for i in range(MAX_TRANSCRIPT_MESSAGES + 10)]
    kept = trim_transcript(many)
    assert len(kept) == MAX_TRANSCRIPT_MESSAGES
    assert kept[-1]["text"] == f"q{MAX_TRANSCRIPT_MESSAGES + 9}"

    huge = [{"role": "user", "text": "a" * 40000}, {"role": "assistant", "text": "b" * 40000}]
    assert [m["text"][0] for m in trim_transcript(huge)] == ["b"]


def test_email_lists_the_conversation_in_order():
    msg = build_email(
        SETTINGS,
        email="ada@example.com",
        message="",
        transcript=[{"role": "user", "text": "first"}, {"role": "assistant", "text": "second"}],
        page_url="",
        session_id="web:demo:v1:c1",
    )
    body = msg.get_content()
    assert (
        body.index("--- Visitor ---")
        < body.index("first")
        < body.index("--- Assistant ---")
        < body.index("second")
    )
    assert "(no message)" in body


def test_settings_are_off_until_recipient_and_host_are_set(monkeypatch):
    for name in ("WIDGET_CONTACT_TO", "WIDGET_SMTP_HOST"):
        monkeypatch.delenv(name, raising=False)
    assert SmtpSettings.from_env() is None
    monkeypatch.setenv("WIDGET_CONTACT_TO", "team@example.com")
    assert SmtpSettings.from_env() is None
    monkeypatch.setenv("WIDGET_SMTP_HOST", "smtp.example.com")
    settings = SmtpSettings.from_env()
    assert settings.port == 587 and settings.security == "starttls"


def test_settings_refuse_an_unknown_security_mode(monkeypatch):
    monkeypatch.setenv("WIDGET_CONTACT_TO", "team@example.com")
    monkeypatch.setenv("WIDGET_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("WIDGET_SMTP_SECURITY", "tls")
    with pytest.raises(ValueError):
        SmtpSettings.from_env()


def test_valid_email_accepts_ordinary_addresses():
    assert valid_email("ada.lovelace+docs@mail.example.co.uk")
