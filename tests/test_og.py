from __future__ import annotations

from types import SimpleNamespace

import pytest

from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import UserContext
from telegram_llm_antispam.og import (
    UnsafeURL,
    _pinned_connection,
    _resolve_public_endpoint,
    _validate_public_http_url,
    parse_og_html,
    should_fetch_og,
)
from test_llm import _settings


def test_parse_og_html_extracts_preview_text():
    preview = parse_og_html(
        """
        <html>
          <head>
            <meta property="og:title" content="CRTV成人版">
            <meta property="og:description" content="看片就选择 CRTV">
            <meta property="og:image:alt" content="preview alt">
          </head>
        </html>
        """,
        original_url="https://example.com/a",
        final_url="https://example.com/a",
        max_text_chars=200,
    )

    assert preview.title == "CRTV成人版"
    assert preview.description == "看片就选择 CRTV"
    assert "CRTV成人版" in preview.text
    assert "看片就选择 CRTV" in preview.text


def test_should_fetch_og_for_dot_with_preview_url():
    message = SimpleNamespace(
        message_id=1,
        chat=SimpleNamespace(id=-1001),
        from_user=SimpleNamespace(id=42),
        text=".",
        link_preview_options=SimpleNamespace(url="https://preview.example/card"),
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=0)
    features = build_message_features(message, context)

    assert should_fetch_og(features, _settings()) is True


def test_validate_public_http_url_blocks_non_http_and_private_hosts():
    with pytest.raises(UnsafeURL):
        _validate_public_http_url("file:///etc/passwd")

    with pytest.raises(UnsafeURL):
        _validate_public_http_url("http://127.0.0.1/")

    with pytest.raises(UnsafeURL):
        _validate_public_http_url("http://localhost/")


def test_og_connection_uses_pinned_resolved_ip(monkeypatch):
    captured: dict[str, object] = {}

    def fake_getaddrinfo(host, port, type):  # noqa: A002, ANN001
        assert host == "example.com"
        return [(None, None, None, "", ("93.184.216.34", port))]

    class FakeSocket:
        def getpeername(self):
            return ("93.184.216.34", 80)

    def fake_create_connection(address, timeout, source_address=None):  # noqa: ANN001
        captured["address"] = address
        captured["timeout"] = timeout
        captured["source_address"] = source_address
        return FakeSocket()

    monkeypatch.setattr("telegram_llm_antispam.og.socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(
        "telegram_llm_antispam.og.socket.create_connection",
        fake_create_connection,
    )

    endpoint = _resolve_public_endpoint("http://example.com/path?q=1")
    connection = _pinned_connection(endpoint, timeout=2)
    connection.connect()

    assert endpoint.connect_host == "93.184.216.34"
    assert captured["address"] == ("93.184.216.34", 80)
