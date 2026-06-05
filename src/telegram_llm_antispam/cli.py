from __future__ import annotations

import argparse

from .config import Settings
from .db import Database
from .logging_config import configure_logging
from .llm import newapi_provider_count


def main() -> None:
    parser = argparse.ArgumentParser(prog="antispam-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create or migrate the SQLite database.")
    subparsers.add_parser("show-config", help="Print effective non-secret configuration.")

    args = parser.parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)

    if args.command == "init-db":
        db = Database.from_settings(settings)
        db.connect()
        db.migrate()
        db.close()
        print(f"Database initialized: {settings.database_path}")
        return

    if args.command == "show-config":
        print(f"DATABASE_PATH={settings.database_path}")
        print(f"LOG_LEVEL={settings.log_level}")
        print(f"ADMIN_USER_IDS={','.join(str(item) for item in settings.admin_user_ids)}")
        print(f"ADMIN_NOTIFY_USER_IDS={','.join(str(item) for item in settings.admin_notify_user_ids)}")
        print(f"ALLOWED_CHAT_IDS={','.join(str(item) for item in settings.allowed_chat_ids)}")
        print(f"REQUIRE_ALLOWED_CHAT={settings.require_allowed_chat}")
        print(f"WHITELIST_DOMAINS={','.join(settings.whitelist_domains)}")
        print(f"VOTE_MIN_CONFIRMATIONS={settings.vote_min_confirmations}")
        print(f"VOTE_TIMEOUT_SECONDS={settings.vote_timeout_seconds}")
        print(f"VOTE_SWEEP_INTERVAL_SECONDS={settings.vote_sweep_interval_seconds}")
        print(f"LOW_REPUTATION_THRESHOLD={settings.low_reputation_threshold}")
        print(f"HIGH_REPUTATION_THRESHOLD={settings.high_reputation_threshold}")
        print(f"REPEAT_WINDOW_SECONDS={settings.repeat_window_seconds}")
        print(f"REPEAT_MIN_DISTINCT_SENDERS={settings.repeat_min_distinct_senders}")
        print(f"NEWAPI_ENABLED={settings.has_newapi}")
        print(f"NEWAPI_PROVIDER_COUNT={newapi_provider_count(settings)}")
        print(f"NEWAPI_BASE_URL={'set' if settings.newapi_base_url else 'unset'}")
        print(f"NEWAPI_MODEL={settings.newapi_model}")
        print(f"OG_FETCH_ENABLED={settings.og_fetch_enabled}")
        print(f"PROFILE_BIO_FETCH_ENABLED={settings.profile_bio_fetch_enabled}")
        return
