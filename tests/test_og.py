from __future__ import annotations

from types import SimpleNamespace

import pytest

from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.models import UserContext
from telegram_llm_antispam.og import UnsafeURL, _validate_public_http_url, parse_og_html, should_fetch_og
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
