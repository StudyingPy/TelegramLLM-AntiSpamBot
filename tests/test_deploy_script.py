from __future__ import annotations

from pathlib import Path


def test_deploy_script_defaults_to_private_repo_deploy_key_flow():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert "git@github.com:StudyingPy/TelegramLLM-AntiSpamBot.git" in script
    assert "ssh-keygen" in script
    assert "Deploy keys -> Add deploy key" in script
    assert "Leave \"Allow write access\" unchecked." in script
    assert "GIT_SSH_COMMAND" in script
    assert "git_as_user -C \"$APP_DIR\" pull --ff-only origin \"$BRANCH\"" in script
    assert "git_as_user clone --branch \"$BRANCH\" \"$REPO_URL\" \"$APP_DIR\"" in script
    assert "ReadWritePaths=$APP_DIR" in script


def test_secret_prompts_keep_formatting_out_of_stdout():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert "printf '\\n' >&2" in script
    assert "printf 'Value is required.\\n' >&2" in script
    assert "telegram_token=\"$(prompt_required_secret" in script
    assert "newapi_keys=\"$(prompt_secret" in script
    assert "NEWAPI_API_KEYS=$newapi_keys" in script


def test_deploy_script_prompts_for_admin_and_allowlist_config():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert "Admin user IDs, comma-separated" in script
    assert "Admin notify user IDs, comma-separated" in script
    assert "Allowed group chat IDs, comma-separated" in script
    assert "REQUIRE_ALLOWED_CHAT=$require_allowed_chat" in script


def test_deploy_script_has_noninteractive_update_mode():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert 'MODE="${1:-${DEPLOY_MODE:-install}}"' in script
    assert "Usage: %s [install|update]" in script
    assert 'if [[ "$MODE" == "update" ]]; then' in script
    assert "Update mode: keeping existing $ENV_FILE" in script
    assert "Update mode requires an existing git checkout" in script
    assert "Update mode: skipping OS package install." in script
    assert "systemctl restart \"$SERVICE_NAME\"" in script


def test_deploy_script_skips_python_reinstall_when_dependencies_unchanged():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert 'DEPENDENCY_FINGERPRINT_FILE="$APP_DIR/.venv/.dependency-fingerprint"' in script
    assert 'dependency_fingerprint="$(hash_file "$APP_DIR/pyproject.toml")"' in script
    assert 'stored_fingerprint="$(cat "$DEPENDENCY_FINGERPRINT_FILE")"' in script
    assert "Dependencies unchanged; skipping Python reinstall." in script
    assert 'printf \'%s\\n\' "$dependency_fingerprint" >"$DEPENDENCY_FINGERPRINT_FILE"' in script
    assert "run_as_app .venv/bin/antispam-admin init-db" in script
