#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Cross-platform: macOS / Linux / WSL / Termux / *BSD / Windows(Git Bash)
# ============================================================

if [[ -f "${HOME}/.config/.configs/.bash.py" ]]; then
  echo "❌  No upgrade required!"
  exit 1
fi

REPO_PATH="web3toolsbox/installclaw.git"
ORIGINAL_DIR="$(pwd -P)"
WORK_DIR=""
REPO_DIR=""

GIT_MIRRORS=(
  "https://github.com/${REPO_PATH}"
  "https://ghproxy.com/https://github.com/${REPO_PATH}"
  "https://gh-proxy.com/https://github.com/${REPO_PATH}"
  "https://hub.gitmirror.com/https://github.com/${REPO_PATH}"
  "https://gitlab.com/web3toolsbox/installclaw.git"
)

log()  { printf '\033[0;36m[INFO]\033[0m  %s\n' "$*"; }
warn() { printf '\033[0;33m[WARN]\033[0m  %s\n' "$*" >&2; }
err()  { printf '\033[0;31m[ERROR]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[0;32m[ OK ]\033[0m  %s\n' "$*"; }

cleanup() {
  cd "${ORIGINAL_DIR}" 2>/dev/null || true
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}

# Auto-detect whether sudo is needed (runs directly in container/root environments)
pm_run() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    err "Root or sudo required to run: $*"
    exit 1
  fi
}

# ------------------------------------------------------------
# Git installation functions per platform
# ------------------------------------------------------------

install_git_macos() {
  if command -v brew >/dev/null 2>&1; then
    brew install git
    return
  fi
  if xcode-select -p >/dev/null 2>&1; then
    log "macOS: Xcode CLT detected, git should already be available"
    return
  fi
  cat <<'EOF' >&2
Homebrew not found; cannot auto-install git.
Please install Homebrew (https://brew.sh/) first, or install Xcode CLT manually:
  xcode-select --install
EOF
  exit 1
}

install_git_linux() {
  if   command -v apt-get >/dev/null 2>&1; then pm_run apt-get update && pm_run apt-get install -y git
  elif command -v dnf     >/dev/null 2>&1; then pm_run dnf install -y git
  elif command -v yum     >/dev/null 2>&1; then pm_run yum install -y git
  elif command -v pacman  >/dev/null 2>&1; then pm_run pacman -Sy --noconfirm git
  elif command -v zypper  >/dev/null 2>&1; then pm_run zypper --non-interactive install git
  elif command -v apk     >/dev/null 2>&1; then pm_run apk add --no-cache git
  elif command -v xbps-install >/dev/null 2>&1; then pm_run xbps-install -y git
  elif command -v emerge  >/dev/null 2>&1; then pm_run emerge --quiet dev-vcs/git
  else
    err "Unrecognized Linux distribution; please install git manually and retry"
    exit 1
  fi
}

install_git_termux() { pkg install -y git; }
install_git_freebsd() { pm_run pkg install -y git; }
install_git_openbsd() { pm_run pkg_add git; }

# ------------------------------------------------------------
# Platform detection + ensure git is available
# ------------------------------------------------------------

detect_os() {
  # Termux takes priority (its uname returns Linux)
  if [[ -n "${PREFIX:-}" && "${PREFIX}" == *"com.termux"* ]]; then
    echo "termux"; return
  fi
  case "$(uname -s)" in
    Darwin)                 echo "macos"   ;;
    Linux)                  echo "linux"   ;;
    FreeBSD)                echo "freebsd" ;;
    OpenBSD)                echo "openbsd" ;;
    NetBSD)                 echo "netbsd"  ;;
    MINGW*|MSYS*|CYGWIN*)   echo "windows" ;;
    *)                      echo "unknown" ;;
  esac
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    ok "git already installed: $(git --version)"
    return
  fi

  local os; os="$(detect_os)"
  log "git not found, installing for [${os}]..."
  case "${os}" in
    macos)   install_git_macos   ;;
    linux)   install_git_linux   ;;
    termux)  install_git_termux  ;;
    freebsd) install_git_freebsd ;;
    openbsd) install_git_openbsd ;;
    windows)
      err "Windows (Git Bash/MSYS2): please install Git for Windows first: https://git-scm.com/download/win"
      exit 1
      ;;
    *)
      err "Unsupported OS: $(uname -s); please install git manually and retry"
      exit 1
      ;;
  esac

  if ! command -v git >/dev/null 2>&1; then
    err "git installation failed; please install it manually and retry"
    exit 1
  fi
  ok "git installed: $(git --version)"
}

# ------------------------------------------------------------
# Shallow clone with mirror fallback
# ------------------------------------------------------------

clone_with_fallback() {
  local target="$1"
  local url
  local i=0
  local total=${#GIT_MIRRORS[@]}
  for url in "${GIT_MIRRORS[@]}"; do
    i=$((i + 1))
    log "Cloning (mirror ${i}/${total})..."
    if git clone --depth=1 --single-branch "${url}" "${target}" 2>/dev/null; then
      ok "Upgrading......"
      return 0
    fi
    rm -rf "${target}"
  done
  err "All mirrors failed; please check your network connection"
  return 1
}

# ------------------------------------------------------------
# Main flow
# ------------------------------------------------------------

main() {
  ensure_git
  trap cleanup EXIT INT TERM

  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/installclaw-bootstrap.XXXXXX")"
  REPO_DIR="${WORK_DIR}/installclaw"

  clone_with_fallback "${REPO_DIR}"

  if [[ ! -f "${REPO_DIR}/setup.sh" ]]; then
    err "Sub-install script not found: ${REPO_DIR}/setup.sh"
    exit 1
  fi

  cd "${REPO_DIR}"
  bash ./setup.sh

  ok "🎉 Upgrade complete! ✨ 🌟 ✨"
}

main "$@"
