from cognee_integration_browser_use import sanitize_url, sanitize_urls
from cognee_integration_browser_use.sanitize import cap_text, clean_for_memory, redact_text


def test_url_credentials_and_secret_params_are_removed():
    assert sanitize_url("https://user:pw@shop.example/a?b=1&token=abc&page=2") == (
        "https://shop.example/a?b=1&page=2"
    )
    assert sanitize_url("https://shop.example/x?access_token=1&SIGNATURE=2&q=ok") == (
        "https://shop.example/x?q=ok"
    )
    assert sanitize_url("https://shop.example:8443/p#frag") == "https://shop.example:8443/p"
    assert sanitize_url("javascript:alert(1)") == ""
    assert sanitize_url("not a url") == ""
    assert sanitize_url("file:///etc/passwd") == ""


def test_sanitize_urls_dedupes_and_caps():
    urls = [
        "https://a.example/?token=1",
        "https://a.example/",
        None,
        "about:blank",
        "https://b.example/",
    ]
    assert sanitize_urls(urls, 10) == ["https://a.example/", "https://b.example/"]
    assert sanitize_urls(urls, 1) == ["https://a.example/"]


def test_text_redaction_and_capping():
    text = "api_key=sk-abcdefghijklmnopqrstuvwxyz and password: hunter2 and AKIAABCDEFGHIJKLMNOP"
    redacted = redact_text(text)
    assert "hunter2" not in redacted
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in redacted
    capped = cap_text("x" * 100, 20)
    assert len(capped) <= 20
    assert capped.endswith("[truncated]")
    assert clean_for_memory("  hello\r\nworld  ", 100) == "hello\nworld"
