from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    value = os.getenv(name, "")
    return tuple(part.strip().lower() for part in value.split(",") if part.strip())


def _env_int_tuple(name: str) -> tuple[int, ...]:
    values: list[int] = []
    for item in os.getenv(name, "").split(","):
        item = item.strip()
        if item:
            values.append(int(item))
    return tuple(values)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str | None
    database_path: Path
    log_level: str
    admin_user_ids: tuple[int, ...]
    admin_notify_user_ids: tuple[int, ...]
    allowed_chat_ids: tuple[int, ...]
    require_allowed_chat: bool
    whitelist_domains: tuple[str, ...]

    vote_min_confirmations: int
    vote_timeout_seconds: int
    vote_sweep_interval_seconds: int

    low_reputation_threshold: float
    high_reputation_threshold: float
    reputation_ban_threshold: float
    default_reputation: float
    spam_reputation_penalty: float
    ham_reputation_reward: float

    repeat_window_seconds: int
    repeat_min_distinct_senders: int

    fingerprint_review_weight: float
    fingerprint_ban_weight: float
    llm_fingerprint_initial_weight: float
    vote_confirmed_fingerprint_weight: float
    fingerprint_false_positive_penalty: float
    llm_review_threshold: float
    llm_ban_threshold: float
    newapi_base_url: str | None
    newapi_api_key: str | None
    newapi_model: str
    newapi_timeout_seconds: float
    newapi_temperature: float
    newapi_max_tokens: int

    preview_punctuation_confidence: float
    new_user_link_confidence: float

    og_fetch_enabled: bool
    og_short_text_max_chars: int
    og_fetch_timeout_seconds: float
    og_fetch_max_bytes: int
    og_fetch_max_text_chars: int
    og_fetch_max_redirects: int

    profile_bio_fetch_enabled: bool
    profile_bio_cache_ttl_seconds: int

    @property
    def has_newapi(self) -> bool:
        return bool(self.newapi_base_url and self.newapi_api_key and self.newapi_model)

    @property
    def notify_user_ids(self) -> tuple[int, ...]:
        return self.admin_notify_user_ids or self.admin_user_ids

    @classmethod
    def from_env(cls, env_file: Path | str = ".env") -> "Settings":
        _load_env_file(Path(env_file))
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            database_path=Path(os.getenv("DATABASE_PATH", "data/bot.db")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            admin_user_ids=_env_int_tuple("ADMIN_USER_IDS"),
            admin_notify_user_ids=_env_int_tuple("ADMIN_NOTIFY_USER_IDS"),
            allowed_chat_ids=_env_int_tuple("ALLOWED_CHAT_IDS"),
            require_allowed_chat=_env_bool("REQUIRE_ALLOWED_CHAT", True),
            whitelist_domains=_env_list("WHITELIST_DOMAINS"),
            vote_min_confirmations=_env_int("VOTE_MIN_CONFIRMATIONS", 3),
            vote_timeout_seconds=_env_int("VOTE_TIMEOUT_SECONDS", 1800),
            vote_sweep_interval_seconds=_env_int("VOTE_SWEEP_INTERVAL_SECONDS", 60),
            low_reputation_threshold=_env_float("LOW_REPUTATION_THRESHOLD", 35),
            high_reputation_threshold=_env_float("HIGH_REPUTATION_THRESHOLD", 80),
            reputation_ban_threshold=_env_float("REPUTATION_BAN_THRESHOLD", 20),
            default_reputation=_env_float("DEFAULT_REPUTATION", 50),
            spam_reputation_penalty=_env_float("SPAM_REPUTATION_PENALTY", 35),
            ham_reputation_reward=_env_float("HAM_REPUTATION_REWARD", 8),
            repeat_window_seconds=_env_int("REPEAT_WINDOW_SECONDS", 300),
            repeat_min_distinct_senders=_env_int("REPEAT_MIN_DISTINCT_SENDERS", 3),
            fingerprint_review_weight=_env_float("FINGERPRINT_REVIEW_WEIGHT", 40),
            fingerprint_ban_weight=_env_float("FINGERPRINT_BAN_WEIGHT", 85),
            llm_fingerprint_initial_weight=_env_float("LLM_FINGERPRINT_INITIAL_WEIGHT", 50),
            vote_confirmed_fingerprint_weight=_env_float("VOTE_CONFIRMED_FINGERPRINT_WEIGHT", 85),
            fingerprint_false_positive_penalty=_env_float("FINGERPRINT_FALSE_POSITIVE_PENALTY", 30),
            llm_review_threshold=_env_float("LLM_REVIEW_THRESHOLD", 0.70),
            llm_ban_threshold=_env_float("LLM_BAN_THRESHOLD", 0.92),
            newapi_base_url=os.getenv("NEWAPI_BASE_URLS") or os.getenv("NEWAPI_BASE_URL") or None,
            newapi_api_key=os.getenv("NEWAPI_API_KEYS") or os.getenv("NEWAPI_API_KEY") or None,
            newapi_model=os.getenv("NEWAPI_MODELS") or os.getenv("NEWAPI_MODEL", "gpt-5.4"),
            newapi_timeout_seconds=_env_float("NEWAPI_TIMEOUT_SECONDS", 8),
            newapi_temperature=_env_float("NEWAPI_TEMPERATURE", 0),
            newapi_max_tokens=_env_int("NEWAPI_MAX_TOKENS", 600),
            preview_punctuation_confidence=_env_float("PREVIEW_PUNCTUATION_CONFIDENCE", 0.82),
            new_user_link_confidence=_env_float("NEW_USER_LINK_CONFIDENCE", 0.72),
            og_fetch_enabled=_env_bool("OG_FETCH_ENABLED", True),
            og_short_text_max_chars=_env_int("OG_SHORT_TEXT_MAX_CHARS", 8),
            og_fetch_timeout_seconds=_env_float("OG_FETCH_TIMEOUT_SECONDS", 3),
            og_fetch_max_bytes=_env_int("OG_FETCH_MAX_BYTES", 65_536),
            og_fetch_max_text_chars=_env_int("OG_FETCH_MAX_TEXT_CHARS", 1200),
            og_fetch_max_redirects=_env_int("OG_FETCH_MAX_REDIRECTS", 3),
            profile_bio_fetch_enabled=_env_bool("PROFILE_BIO_FETCH_ENABLED", True),
            profile_bio_cache_ttl_seconds=_env_int("PROFILE_BIO_CACHE_TTL_SECONDS", 604_800),
        )
