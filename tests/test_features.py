from __future__ import annotations

from types import SimpleNamespace

from telegram_llm_antispam.features import build_message_features
from telegram_llm_antispam.fingerprints import skeleton_hash, skeletonize
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


def test_skeletonize_keeps_cjk_distinct_per_message():
    """Regression: the old skeletonize() collapsed every contiguous CJK run to a
    single `<zh>` placeholder, so unrelated short Chinese messages all hashed to the
    same skeleton. Once a single vote-confirmed spam upgraded that hash to weight 85,
    every short Chinese sentence by a normal-reputation user was auto-banned.

    Concrete case from production: after some Chinese ad was confirmed via vote, the
    next user typing "不清楚" or "而且赔钱机场也是这样" got insta-banned with reason
    `known_high_weight_fingerprint / 95%`. This test pins the fix.
    """

    samples = [
        "不清楚",
        "而且赔钱机场也是这样",
        "没用啊？换了很多次不同地区节点",
        "好的",
        "收到",
        "怎么用",
    ]
    hashes = {sample: skeleton_hash(sample) for sample in samples}

    # Every unrelated CJK sentence must have a distinct skeleton hash now.
    assert len(set(hashes.values())) == len(samples), hashes

    # And the skeleton itself should retain the CJK content, not be a placeholder.
    assert "<zh>" not in skeletonize("不清楚")
    assert "不清楚" in skeletonize("不清楚")


def test_skeletonize_still_collapses_repeated_carriers_and_latin_words():
    """The skeleton fix only changed CJK behavior. URL/email/@mention placeholders and
    Latin word generalization should still work — those carriers are what we WANT to
    fold so paraphrased English/URL spam still collides."""

    skeleton_a = skeletonize("buy now https://promo-a.example for 50% off")
    skeleton_b = skeletonize("buy now https://promo-b.example for 90% off")

    # numbers stripped, words generalized to <w>, urls to <url> → identical skeleton
    assert skeleton_a == skeleton_b
    assert "<url>" in skeleton_a
    assert "<w>" in skeleton_a

