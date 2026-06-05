#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-git@github.com:StudyingPy/TelegramLLM-AntiSpamBot.git}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/opt/telegram-llm-antispam-bot}"
APP_USER="${APP_USER:-antispambot}"
SERVICE_NAME="${SERVICE_NAME:-telegram-llm-antispam-bot}"
APP_HOME="${APP_HOME:-/var/lib/$SERVICE_NAME}"
DEPLOY_KEY_PATH="${DEPLOY_KEY_PATH:-}"
MODE="${1:-${DEPLOY_MODE:-install}}"
ENV_FILE="$APP_DIR/.env"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
DEPENDENCY_FINGERPRINT_FILE="$APP_DIR/.venv/.dependency-fingerprint"

case "$MODE" in
  install|update) ;;
  -h|--help|help)
    printf 'Usage: %s [install|update]\n' "${0##*/}"
    printf '  install: first-time interactive deployment\n'
    printf '  update: pull latest code, keep .env, skip Python reinstall when dependencies are unchanged\n'
    exit 0
    ;;
  *)
    printf 'Usage: %s [install|update]\n' "${0##*/}" >&2
    exit 2
    ;;
esac

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  if [[ -f "$0" ]]; then
    exec sudo -E bash "$0" "$@"
  fi
  printf 'Please run this installer with sudo or as root.\n' >&2
  exit 1
fi

log() {
  printf '\n[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
}

sanitize_env() {
  printf '%s' "$1" | tr -d '\r\n'
}

prompt() {
  local name="$1"
  local default_value="${2:-}"
  local value
  if [[ -n "$default_value" ]]; then
    read -r -p "$name [$default_value]: " value
    printf '%s' "${value:-$default_value}"
  else
    read -r -p "$name: " value
    printf '%s' "$value"
  fi
}

prompt_required_secret() {
  local name="$1"
  local value=""
  while [[ -z "$value" ]]; do
    read -r -s -p "$name: " value
    printf '\n' >&2
    value="$(sanitize_env "$value")"
    if [[ -z "$value" ]]; then
      printf 'Value is required.\n' >&2
    fi
  done
  printf '%s' "$value"
}

prompt_secret() {
  local name="$1"
  local value
  read -r -s -p "$name (leave empty to disable): " value
  printf '\n' >&2
  sanitize_env "$value"
}

prompt_bool() {
  local name="$1"
  local default_value="$2"
  local value
  read -r -p "$name [$default_value]: " value
  value="${value:-$default_value}"
  case "${value,,}" in
    y|yes|true|1|on) printf 'true' ;;
    *) printf 'false' ;;
  esac
}

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
      git openssh-client python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git openssh-clients python3 python3-pip
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git openssh-clients python3 python3-pip
  else
    log "No supported package manager detected; assuming git and python3 are installed."
  fi
}

ensure_app_user() {
  if id "$APP_USER" >/dev/null 2>&1; then
    APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
    DEPLOY_KEY_PATH="${DEPLOY_KEY_PATH:-$APP_HOME/.ssh/deploy_key}"
    return
  fi
  mkdir -p "$APP_HOME"
  useradd --system --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
  chown "$APP_USER:$APP_USER" "$APP_HOME"
  DEPLOY_KEY_PATH="${DEPLOY_KEY_PATH:-$APP_HOME/.ssh/deploy_key}"
}

uses_ssh_repo() {
  [[ "$REPO_URL" == git@* || "$REPO_URL" == ssh://* ]]
}

git_ssh_command() {
  if uses_ssh_repo; then
    printf 'ssh -i %q -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new' "$DEPLOY_KEY_PATH"
  else
    printf ''
  fi
}

run_as_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- "$@"
  else
    su -s /bin/bash "$APP_USER" -c "$(printf '%q ' "$@")"
  fi
}

git_as_user() {
  local ssh_command
  ssh_command="$(git_ssh_command)"
  if [[ -n "$ssh_command" ]]; then
    run_as_user env GIT_SSH_COMMAND="$ssh_command" git "$@"
  else
    run_as_user git "$@"
  fi
}

prepare_deploy_key() {
  if [[ "$MODE" == "install" ]]; then
    configure_deploy_key
    return
  fi

  if ! uses_ssh_repo; then
    log "Update mode: repository URL is not SSH; skipping deploy key setup."
    return
  fi

  if [[ ! -f "$DEPLOY_KEY_PATH" ]]; then
    printf 'Update mode requires an existing deploy key at %s for SSH repositories.\n' "$DEPLOY_KEY_PATH" >&2
    printf 'Run install mode first, set DEPLOY_KEY_PATH, or set REPO_URL to an HTTPS remote.\n' >&2
    exit 1
  fi

  log "Update mode: using existing deploy key at $DEPLOY_KEY_PATH"
  run_as_user mkdir -p "$APP_HOME/.ssh"
  run_as_user ssh-keyscan -H github.com >>"$APP_HOME/.ssh/known_hosts" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$APP_HOME/.ssh/known_hosts" || true
  chmod 600 "$APP_HOME/.ssh/known_hosts" || true
}

configure_deploy_key() {
  if ! uses_ssh_repo; then
    log "Repository URL is not SSH; skipping deploy key setup."
    return
  fi

  log "Preparing deploy key for private repository access"
  install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$(dirname "$DEPLOY_KEY_PATH")"
  if [[ ! -f "$DEPLOY_KEY_PATH" ]]; then
    run_as_user ssh-keygen \
      -t ed25519 \
      -C "$SERVICE_NAME deploy key" \
      -f "$DEPLOY_KEY_PATH" \
      -N ""
  fi
  chown "$APP_USER:$APP_USER" "$DEPLOY_KEY_PATH" "$DEPLOY_KEY_PATH.pub"
  chmod 600 "$DEPLOY_KEY_PATH"
  chmod 644 "$DEPLOY_KEY_PATH.pub"

  run_as_user mkdir -p "$APP_HOME/.ssh"
  run_as_user ssh-keyscan -H github.com >>"$APP_HOME/.ssh/known_hosts" 2>/dev/null || true
  chown "$APP_USER:$APP_USER" "$APP_HOME/.ssh/known_hosts" || true
  chmod 600 "$APP_HOME/.ssh/known_hosts" || true

  if git_as_user ls-remote "$REPO_URL" "$BRANCH" >/dev/null 2>&1; then
    log "Deploy key already has access to the repository."
    return
  fi

  printf '\nAdd this public key to GitHub as a read-only deploy key:\n\n'
  cat "$DEPLOY_KEY_PATH.pub"
  printf '\nGitHub path: repository Settings -> Deploy keys -> Add deploy key\n'
  printf 'Title suggestion: %s on %s\n' "$SERVICE_NAME" "$(hostname)"
  printf 'Leave "Allow write access" unchecked.\n\n'
  read -r -p "Press Enter after adding the deploy key to GitHub..."

  log "Testing deploy key access"
  if ! git_as_user ls-remote "$REPO_URL" "$BRANCH" >/dev/null; then
    printf 'Could not access %s branch %s with the deploy key.\n' "$REPO_URL" "$BRANCH" >&2
    printf 'Check that the key was added to the private repo and try again.\n' >&2
    exit 1
  fi
}

sync_repo() {
  mkdir -p "$(dirname "$APP_DIR")"

  if [[ "$MODE" == "update" && ! -d "$APP_DIR/.git" ]]; then
    printf 'Update mode requires an existing git checkout at %s.\n' "$APP_DIR" >&2
    printf 'Run install mode first, or choose install mode to create a new checkout.\n' >&2
    exit 1
  fi

  if [[ -d "$APP_DIR" ]]; then
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  else
    install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
  fi

  if [[ -d "$APP_DIR/.git" ]]; then
    git_as_user -C "$APP_DIR" fetch origin "$BRANCH"
    git_as_user -C "$APP_DIR" checkout "$BRANCH"
    git_as_user -C "$APP_DIR" pull --ff-only origin "$BRANCH"
  else
    if [[ -n "$(find "$APP_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
      printf '%s exists but is not a git checkout. Move it aside or set APP_DIR.\n' "$APP_DIR" >&2
      exit 1
    fi
    git_as_user clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
  fi
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

hash_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

run_as_app() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$APP_USER" -- bash -c 'cd "$1" && shift && "$@"' bash "$APP_DIR" "$@"
  else
    su -s /bin/bash "$APP_USER" -c "cd '$APP_DIR' && $(printf '%q ' "$@")"
  fi
}

write_env_file() {
  if [[ "$MODE" == "update" ]]; then
    if [[ -f "$ENV_FILE" ]]; then
      log "Update mode: keeping existing $ENV_FILE"
      return
    fi
    printf 'Update mode requires an existing %s. Run install mode first.\n' "$ENV_FILE" >&2
    exit 1
  fi

  if [[ -f "$ENV_FILE" ]]; then
    local overwrite
    overwrite="$(prompt_bool "Existing .env found. Regenerate it?" "false")"
    if [[ "$overwrite" != "true" ]]; then
      log "Keeping existing $ENV_FILE"
      return
    fi
  fi

  log "Generating $ENV_FILE"
  local telegram_token database_path whitelist_domains log_level
  local admin_user_ids admin_notify_user_ids allowed_chat_ids require_allowed_chat
  local vote_min vote_timeout vote_sweep
  local enable_newapi newapi_bases newapi_keys newapi_models newapi_timeout
  local enable_og enable_profile_bio

  telegram_token="$(prompt_required_secret "Telegram bot token")"
  database_path="$(sanitize_env "$(prompt "SQLite database path" "data/bot.db")")"
  admin_user_ids="$(sanitize_env "$(prompt "Admin user IDs, comma-separated" "")")"
  admin_notify_user_ids="$(sanitize_env "$(prompt "Admin notify user IDs, comma-separated (empty = admin IDs)" "")")"
  allowed_chat_ids="$(sanitize_env "$(prompt "Allowed group chat IDs, comma-separated" "")")"
  require_allowed_chat="$(prompt_bool "Require /allow_chat before moderating groups?" "true")"
  whitelist_domains="$(sanitize_env "$(prompt "Whitelist domains, comma-separated" "")")"
  log_level="$(sanitize_env "$(prompt "Log level" "INFO")")"
  vote_min="$(sanitize_env "$(prompt "Vote minimum confirmations" "3")")"
  vote_timeout="$(sanitize_env "$(prompt "Vote timeout seconds" "1800")")"
  vote_sweep="$(sanitize_env "$(prompt "Vote sweep interval seconds" "60")")"

  enable_newapi="$(prompt_bool "Enable NewAPI LLM judgement?" "false")"
  if [[ "$enable_newapi" == "true" ]]; then
    newapi_bases="$(sanitize_env "$(prompt "NewAPI base URLs, comma-separated" "https://your-newapi-host")")"
    newapi_keys="$(prompt_secret "NewAPI API keys, comma-separated")"
    newapi_models="$(sanitize_env "$(prompt "NewAPI models, comma-separated" "gpt-5.4")")"
    newapi_timeout="$(sanitize_env "$(prompt "NewAPI timeout seconds" "8")")"
  else
    newapi_bases=""
    newapi_keys=""
    newapi_models="gpt-5.4"
    newapi_timeout="8"
  fi

  enable_og="$(prompt_bool "Enable guarded OG fetch for short preview messages?" "true")"
  enable_profile_bio="$(prompt_bool "Enable best-effort user bio fetch?" "true")"

  cat >"$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$telegram_token
DATABASE_PATH=$database_path
LOG_LEVEL=$log_level
ADMIN_USER_IDS=$admin_user_ids
ADMIN_NOTIFY_USER_IDS=$admin_notify_user_ids
ALLOWED_CHAT_IDS=$allowed_chat_ids
REQUIRE_ALLOWED_CHAT=$require_allowed_chat
WHITELIST_DOMAINS=$whitelist_domains

VOTE_MIN_CONFIRMATIONS=$vote_min
VOTE_TIMEOUT_SECONDS=$vote_timeout
VOTE_SWEEP_INTERVAL_SECONDS=$vote_sweep
LOW_REPUTATION_THRESHOLD=35
HIGH_REPUTATION_THRESHOLD=80
REPUTATION_BAN_THRESHOLD=20
REPEAT_WINDOW_SECONDS=300
REPEAT_MIN_DISTINCT_SENDERS=3
LLM_FINGERPRINT_INITIAL_WEIGHT=50
VOTE_CONFIRMED_FINGERPRINT_WEIGHT=85
FINGERPRINT_FALSE_POSITIVE_PENALTY=30

NEWAPI_BASE_URLS=$newapi_bases
NEWAPI_API_KEYS=$newapi_keys
NEWAPI_MODELS=$newapi_models
NEWAPI_TIMEOUT_SECONDS=$newapi_timeout
NEWAPI_TEMPERATURE=0
NEWAPI_MAX_TOKENS=600

OG_FETCH_ENABLED=$enable_og
OG_SHORT_TEXT_MAX_CHARS=8
OG_FETCH_TIMEOUT_SECONDS=3
OG_FETCH_MAX_BYTES=65536
OG_FETCH_MAX_TEXT_CHARS=1200
OG_FETCH_MAX_REDIRECTS=3

PROFILE_BIO_FETCH_ENABLED=$enable_profile_bio
PROFILE_BIO_CACHE_TTL_SECONDS=604800
EOF

  chown "$APP_USER:$APP_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}

install_python_app() {
  local dependency_fingerprint stored_fingerprint
  dependency_fingerprint="$(hash_file "$APP_DIR/pyproject.toml")"

  if [[ "$MODE" == "update" \
    && -x "$APP_DIR/.venv/bin/python" \
    && -x "$APP_DIR/.venv/bin/antispam-admin" \
    && -f "$DEPENDENCY_FINGERPRINT_FILE" ]]; then
    stored_fingerprint="$(cat "$DEPENDENCY_FINGERPRINT_FILE")"
    if [[ "$stored_fingerprint" == "$dependency_fingerprint" ]]; then
      log "Dependencies unchanged; skipping Python reinstall."
      run_as_app .venv/bin/antispam-admin init-db
      return
    fi
  fi

  run_as_app python3 -m venv .venv
  run_as_app .venv/bin/python -m pip install --upgrade pip
  run_as_app .venv/bin/python -m pip install -e .
  printf '%s\n' "$dependency_fingerprint" >"$DEPENDENCY_FINGERPRINT_FILE"
  chown "$APP_USER:$APP_USER" "$DEPENDENCY_FINGERPRINT_FILE"
  chmod 600 "$DEPENDENCY_FINGERPRINT_FILE"
  run_as_app .venv/bin/antispam-admin init-db
}

install_systemd_service() {
  cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Telegram LLM Anti-Spam Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/antispam-bot
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$APP_DIR
UMask=0077

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  if [[ "$MODE" == "update" ]]; then
    systemctl restart "$SERVICE_NAME"
  else
    systemctl enable --now "$SERVICE_NAME"
  fi
}

main() {
  log "Deploy mode: $MODE"
  if [[ "$MODE" == "update" ]]; then
    log "Update mode: skipping OS package install."
  else
    log "Installing dependencies"
    install_packages
  fi

  log "Preparing service user"
  ensure_app_user

  prepare_deploy_key

  log "Syncing repository $REPO_URL ($BRANCH)"
  sync_repo

  write_env_file

  log "Installing Python application"
  install_python_app

  log "Installing systemd service"
  install_systemd_service

  log "Deployment complete ($MODE)"
  systemctl --no-pager --full status "$SERVICE_NAME" || true
  printf '\nUseful commands:\n'
  printf '  journalctl -u %s -f\n' "$SERVICE_NAME"
  printf '  systemctl restart %s\n' "$SERVICE_NAME"
  printf '  sudo bash %s/deploy/install.sh update\n' "$APP_DIR"
  printf '  cd %s && sudo -u %s .venv/bin/antispam-admin show-config\n' "$APP_DIR" "$APP_USER"
}

main "$@"
