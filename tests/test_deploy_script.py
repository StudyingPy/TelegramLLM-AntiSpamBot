from __future__ import annotations

from pathlib import Path


def test_deploy_script_defaults_to_private_repo_deploy_key_flow():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert "git@github.com:StudyingPy/TelegramLLM-AntiSpamBot.git" in script
    assert "ssh-keygen" in script
    assert "Deploy keys -> Add deploy key" in script
    assert "Leave \"Allow write access\" unchecked." in script
    assert "GIT_SSH_COMMAND" in script
    assert "ReadWritePaths=$APP_DIR" in script


def test_secret_prompts_keep_formatting_out_of_stdout():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert "printf '\\n' >&2" in script
    assert "printf 'Value is required.\\n' >&2" in script
    assert "telegram_token=\"$(prompt_required_secret" in script
    assert "newapi_key=\"$(prompt_secret" in script


def test_deploy_script_prompts_for_admin_and_allowlist_config():
    script = Path("deploy/install.sh").read_text(encoding="utf-8")

    assert "Admin user IDs, comma-separated" in script
    assert "Admin notify user IDs, comma-separated" in script
    assert "Allowed group chat IDs, comma-separated" in script
    assert "REQUIRE_ALLOWED_CHAT=$require_allowed_chat" in script
