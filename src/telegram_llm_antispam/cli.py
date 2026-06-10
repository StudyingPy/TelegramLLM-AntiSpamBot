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

    subparsers.add_parser(
        "purge-low-entropy-fingerprints",
        help=(
            "Delete every fingerprint whose value is one of the known low-entropy "
            "sentinel hashes (<url>, <mention>, <w>, <email>, common 2-placeholder "
            "combinations, empty-text). These all match too many unrelated messages "
            "to enforce on; once vote-confirmed feedback upgraded any of them to "
            "weight 85 the bot would BAN or WITHDRAW_VOTE on every URL-only / "
            "mention-only / single-word message. Run after deploying the low-entropy "
            "guard so the DB matches the new write-side filter."
        ),
    )

    inspect_fp = subparsers.add_parser(
        "inspect-fingerprint",
        help=(
            "Show every vote_session and action_log row that produced or referenced a "
            "given fingerprint hash. Useful to audit why a content/skeleton fingerprint "
            "was upgraded to high weight — the text_snapshot in action_log metadata "
            "tells you the original message that confirmed it."
        ),
    )
    inspect_fp.add_argument("value", help="the fingerprint hash to inspect")

    whitelist_user = subparsers.add_parser(
        "whitelist-user",
        help=(
            "Mark a user_id as whitelisted: all their messages bypass moderation. "
            "Intended for friendly bots like nmBot / 客服酱 whose moderation "
            "notifications would otherwise get caught by our local rules. Persists "
            "in the whitelisted_users table; survives restart. Use the WHITELISTED_"
            "USER_IDS env var for the same purpose if you want it tracked in .env."
        ),
    )
    whitelist_user.add_argument("user_id", type=int)
    whitelist_user.add_argument(
        "--note", default=None, help="optional human-readable annotation (e.g. 'nmBot 客服酱')"
    )

    unwhitelist_user = subparsers.add_parser(
        "unwhitelist-user",
        help="Remove a user_id from the whitelist table (env-configured ids are unaffected).",
    )
    unwhitelist_user.add_argument("user_id", type=int)

    subparsers.add_parser(
        "list-whitelisted-users",
        help="List every user_id in the whitelisted_users table.",
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

    if args.command == "purge-low-entropy-fingerprints":
        from .fingerprints import LOW_ENTROPY_SKELETON_HASHES, stable_hash

        sentinels = sorted({stable_hash(""), *LOW_ENTROPY_SKELETON_HASHES})
        db = Database.from_settings(settings)
        db.connect()
        try:
            total = 0
            for value in sentinels:
                removed = db.delete_fingerprints_by_value(value)
                if removed:
                    print(f"  removed {removed} row(s) at value={value}")
                    total += removed
            print(f"Total fingerprint rows removed: {total}")
        finally:
            db.close()
        return

    if args.command == "inspect-fingerprint":
        db = Database.from_settings(settings)
        db.connect()
        try:
            fp = db.get_fingerprint(args.value)
            if fp is None:
                print(f"No fingerprint with value={args.value}")
            else:
                print(
                    f"Fingerprint id={fp.id} type={fp.fingerprint_type} "
                    f"weight={fp.weight} hits={fp.hit_count} "
                    f"fp={fp.false_positive_count} source={fp.source}"
                )

            sessions = db.find_vote_sessions_by_hash(args.value)
            if not sessions:
                print("(no vote_sessions referenced this hash)")
            else:
                print(f"\nVote sessions referencing this hash ({len(sessions)}):")
                for s in sessions:
                    print(
                        f"  session_id={s['id']} chat_id={s['chat_id']} "
                        f"user_id={s['suspect_user_id']} status={s['status']} "
                        f"reason={s['reason']!r} created_at={s['created_at']}"
                    )
                    snap = s.get("text_snapshot")
                    if snap:
                        # text_snapshot is in action_log metadata, only present when
                        # the original moderation action stored it (WITHDRAW_VOTE or
                        # BAN paths in actions.py).
                        truncated = snap[:200] + ("..." if len(snap) > 200 else "")
                        print(f"    text: {truncated!r}")
        finally:
            db.close()
        return

    if args.command == "whitelist-user":
        db = Database.from_settings(settings)
        db.connect()
        try:
            db.whitelist_user(args.user_id, args.note, added_by_user_id=None)
            note = f" (note: {args.note})" if args.note else ""
            print(f"Whitelisted user_id={args.user_id}{note}")
        finally:
            db.close()
        return

    if args.command == "unwhitelist-user":
        db = Database.from_settings(settings)
        db.connect()
        try:
            removed = db.unwhitelist_user(args.user_id)
            print(
                f"Removed user_id={args.user_id} from whitelist"
                if removed
                else f"user_id={args.user_id} was not in whitelist"
            )
        finally:
            db.close()
        return

    if args.command == "list-whitelisted-users":
        db = Database.from_settings(settings)
        db.connect()
        try:
            rows = db.list_whitelisted_users()
            env_ids = settings.whitelisted_user_ids
            if env_ids:
                print(f"From WHITELISTED_USER_IDS env: {', '.join(str(i) for i in env_ids)}")
            if not rows:
                print("(no rows in whitelisted_users table)")
                return
            print(f"{'user_id':>14} {'added_at':>12}  note")
            for row in rows:
                print(
                    f"{row['user_id']:>14} {row['added_at']:>12}  {row.get('note') or ''}"
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
    print(f"WHITELISTED_USER_IDS={','.join(str(item) for item in settings.whitelisted_user_ids)}")
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
