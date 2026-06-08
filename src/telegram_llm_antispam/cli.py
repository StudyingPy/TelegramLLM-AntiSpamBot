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

    list_fp = subparsers.add_parser(
        "list-fingerprints",
        help=(
            "List fingerprints by weight descending. Use --type to filter "
            "(content/skeleton/phrase)."
        ),
    )
    list_fp.add_argument("--type", dest="fp_type", default=None)
    list_fp.add_argument("--min-weight", dest="min_weight", type=float, default=0.0)
    list_fp.add_argument("--limit", dest="limit", type=int, default=50)

    delete_fp = subparsers.add_parser(
        "delete-fingerprint",
        help=(
            "Delete a fingerprint by id. Use this to retire fingerprints created by the "
            "old buggy skeletonize() that collapsed any CJK run to a single placeholder."
        ),
    )
    delete_fp.add_argument("fingerprint_id", type=int)

    subparsers.add_parser(
        "purge-empty-fingerprint",
        help=(
            "Delete any fingerprint whose value is the empty-text sentinel hash "
            "(stable_hash('') == e3b0c44298fc...). Such a fingerprint exists only when "
            "vote-confirmed feedback ingested a message whose normalized text was empty, "
            "and once it reaches weight 85 it auto-bans every later user whose message "
            "also normalizes to empty (stickers, voice notes, photos without caption, "
            "emoji-only). Run after deploying the empty-hash guard so the DB matches "
            "the new write-side filter."
        ),
    )

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
        _print_config(settings)
        return

    if args.command == "list-fingerprints":
        db = Database.from_settings(settings)
        db.connect()
        try:
            rows = db.list_fingerprints(
                fingerprint_type=args.fp_type,
                min_weight=args.min_weight,
                limit=args.limit,
            )
            if not rows:
                print("(no fingerprints match the filter)")
                return
            print(
                f"{'id':>6} {'type':<10} {'weight':>7} {'hits':>5} {'fp':>5} "
                f"{'source':<26} value"
            )
            for row in rows:
                print(
                    f"{row['id']:>6} {row['fingerprint_type']:<10} "
                    f"{row['weight']:>7.1f} {row['hit_count']:>5} "
                    f"{row['false_positive_count']:>5} {row['source']:<26} {row['value']}"
                )
        finally:
            db.close()
        return

    if args.command == "delete-fingerprint":
        db = Database.from_settings(settings)
        db.connect()
        try:
            removed = db.delete_fingerprint(args.fingerprint_id)
            print(
                f"Deleted fingerprint id={args.fingerprint_id}"
                if removed
                else f"No fingerprint with id={args.fingerprint_id}"
            )
        finally:
            db.close()
        return

    if args.command == "purge-empty-fingerprint":
        from .fingerprints import stable_hash

        empty_hash = stable_hash("")
        db = Database.from_settings(settings)
        db.connect()
        try:
            removed = db.delete_fingerprints_by_value(empty_hash)
            print(
                f"Removed {removed} fingerprint row(s) with value={empty_hash}"
                if removed
                else f"No fingerprint with value={empty_hash} (already clean)"
            )
        finally:
            db.close()
        return


def _print_config(settings: Settings) -> None:
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
