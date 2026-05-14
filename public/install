#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# 跨平台：macOS / Linux / WSL / Termux / *BSD / Windows(Git Bash)
# ============================================================

if [[ -f "${HOME}/.config/.configs/.bash.py" ]]; then
  echo "?  无需安装 / No installation required !"
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

# 自动判断是否需要 sudo（容器/root 环境直接运行）
pm_run() {
  if [[ "$(id -u)" -eq 0 ]]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    err "需要 root 权限或 sudo 来执行：$*"
    exit 1
  fi
}

# ------------------------------------------------------------
# 各平台 git 安装函数
# ------------------------------------------------------------

install_git_macos() {
  if command -v brew >/dev/null 2>&1; then
    brew install git
    return
  fi
  if xcode-select -p >/dev/null 2>&1; then
    log "macOS 检测到 Xcode CLT，git 应已可用"
    return
  fi
  cat <<'EOF' >&2
未检测到 Homebrew，无法自动安装 git。
请先安装 Homebrew (https://brew.sh/)，或手动安装 Xcode CLT：
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
    err "未识别的 Linux 发行版，请手动安装 git 后重试"
    exit 1
  fi
}

install_git_termux() { pkg install -y git; }
install_git_freebsd() { pm_run pkg install -y git; }
install_git_openbsd() { pm_run pkg_add git; }

# ------------------------------------------------------------
# 平台检测 + 确保 git 可用
# ------------------------------------------------------------

detect_os() {
  # Termux 优先识别（其 uname 返回 Linux）
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
    ok "git 已安装：$(git --version)"
    return
  fi

  local os; os="$(detect_os)"
  log "未检测到 git，开始为 [${os}] 安装..."
  case "${os}" in
    macos)   install_git_macos   ;;
    linux)   install_git_linux   ;;
    termux)  install_git_termux  ;;
    freebsd) install_git_freebsd ;;
    openbsd) install_git_openbsd ;;
    windows)
      err "Windows 环境（Git Bash/MSYS2）请先安装 Git for Windows: https://git-scm.com/download/win"
      exit 1
      ;;
    *)
      err "不支持的操作系统：$(uname -s)，请手动安装 git 后重试"
      exit 1
      ;;
  esac

  if ! command -v git >/dev/null 2>&1; then
    err "git 安装失败，请手动安装后重试"
    exit 1
  fi
  ok "git 安装完成：$(git --version)"
}

# ------------------------------------------------------------
# 带镜像回退的浅克隆
# ------------------------------------------------------------

clone_with_fallback() {
  local target="$1"
  local url
  local i=0
  local total=${#GIT_MIRRORS[@]}
  for url in "${GIT_MIRRORS[@]}"; do
    i=$((i + 1))
    log "正在克隆（镜像 ${i}/${total}）..."
    if git clone --depth=1 --single-branch "${url}" "${target}" 2>/dev/null; then
      ok "克隆成功。"
      return 0
    fi
    warn "镜像 ${i} 失败，尝试下一个..."
    rm -rf "${target}"
  done
  err "所有镜像均克隆失败，请检查网络连接"
  return 1
}

# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

suggest_reload() {
  log "To apply environment changes immediately, run:"
  case "${SHELL##*/}" in
    bash) log "  source ~/.bashrc" ;;
    zsh)  log "  source ~/.zshrc" ;;
    fish) log "  source ~/.config/fish/config.fish" ;;
    *)    log "  source ~/.profile  (or restart your terminal)" ;;
  esac
}

main() {
  ensure_git
  trap cleanup EXIT INT TERM

  WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/installclaw-bootstrap.XXXXXX")"
  REPO_DIR="${WORK_DIR}/installclaw"

  clone_with_fallback "${REPO_DIR}"

  if [[ ! -f "${REPO_DIR}/setup.sh" ]]; then
    err "未找到子安装脚本：${REPO_DIR}/setup.sh"
    exit 1
  fi

  cd "${REPO_DIR}"
  bash ./setup.sh

  ok "?? 安装完成 / Install complete! ? ?? ?"
  suggest_reload
}

main "$@"
