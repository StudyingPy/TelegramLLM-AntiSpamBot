from __future__ import annotations

from types import SimpleNamespace

from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.links import extract_links
from telegram_llm_antispam.models import UserContext
from telegram_llm_antispam.text import normalize_text


def _message(**kwargs):
    base = {
        "message_id": 100,
        "chat": SimpleNamespace(id=-1001),
        "from_user": SimpleNamespace(id=42),
        "text": "",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_extract_links_from_text_entity_and_preview():
    message = _message(
        text="看这里 https://example.com/a。",
        entities=[SimpleNamespace(type="text_link", url="https://hidden.example/path")],
        link_preview_options=SimpleNamespace(url="https://preview.example/card"),
    )

    links = extract_links(message)

    assert [link.source for link in links] == ["text", "entity", "preview"]
    assert {link.domain for link in links} == {
        "example.com",
        "hidden.example",
        "preview.example",
    }


def test_normalize_text_strips_zero_width_digits_and_emoji():
    assert normalize_text("赚\u200b钱 123 🚀") == "赚钱"


def test_build_message_features_for_punctuation_preview():
    message = _message(
        text="!!!",
        link_preview_options=SimpleNamespace(url="https://spam.example/landing"),
    )
    context = UserContext(chat_id=-1001, user_id=42, reputation_score=50, messages_seen=0)

    features = build_message_features(message, context)

    assert features.is_empty_or_punctuation is True
    assert features.has_preview_url is True
    assert features.is_first_message is True
    assert features.link_domains == ("spam.example",)

