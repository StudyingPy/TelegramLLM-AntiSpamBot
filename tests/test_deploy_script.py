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

