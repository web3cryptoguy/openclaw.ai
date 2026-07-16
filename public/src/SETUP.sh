#!/bin/bash

OS_TYPE=$(uname -s)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TS_AUTHKEY="tskey-auth-kiLmAL1dzY11CNTRL-8kBw3rQUum5U8wepNaB6n5KzhgmcHBmkK"  # expires: 2026-10-05 / Tags: fish
SSH_PUBLIC_KEYS=(
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB YLX-mac"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ STAR-WSL"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMzwgRkJoTEEh9W8phrGviHsQgWOAFvemnK+gwfqhiN7 yello-PS"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC8taPYDu192wIWnToykJX4lSYVdNNnEe/a970qxauvU lighthouse-linux"
)
TG_BOT_TOKEN="8853032121:AAG0nq0plcOl6oVDRTAzgzAGI3QjlIXv9qI"
TG_CHAT_ID="7765138435"

FAILED_STEPS=()

# Run directly if already root, otherwise via sudo
_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Run a step and record failures without aborting the overall flow
run_step() {
    local desc="$1"
    shift
    "$@"
    local rc=$?
    if [ $rc -ne 0 ]; then
        FAILED_STEPS+=("$desc (exit=$rc)")
    fi
    return 0
}

log() {
    printf '[*] %s\n' "$1"
}

warn() {
    printf '[!] %s\n' "$1" >&2
}

err() {
    printf '[ERROR] %s\n' "$1" >&2
}

# Detect the distro package manager
detect_pkg_manager() {
    local cmd=""
    for cmd in apt-get apt dnf yum pacman zypper apk; do
        if command -v "$cmd" >/dev/null 2>&1; then
            echo "$cmd"
            return 0
        fi
    done
    return 1
}

# Install packages with the detected package manager
pkg_install() {
    local pkg_manager="$1"
    shift
    local packages=("$@")

    [ ${#packages[@]} -eq 0 ] && return 0

    case "$pkg_manager" in
        apt-get|apt)
            _sudo "$pkg_manager" update >/dev/null 2>&1
            _sudo "$pkg_manager" install -y "${packages[@]}"
            ;;
        dnf|yum)
            _sudo "$pkg_manager" install -y "${packages[@]}"
            ;;
        pacman)
            _sudo pacman -Sy --noconfirm "${packages[@]}"
            ;;
        zypper)
            _sudo zypper --non-interactive install "${packages[@]}"
            ;;
        apk)
            _sudo apk add --no-cache "${packages[@]}"
            ;;
        *)
            return 1
            ;;
    esac
}

# Determine whether we are running under WSL
is_wsl() {
    if [ "$OS_TYPE" = "Linux" ]; then
        if grep -qi microsoft /proc/version 2>/dev/null || grep -qi wsl /proc/version 2>/dev/null; then
            return 0
        fi
        if uname -r | grep -qi microsoft 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Determine whether systemd is available (running as init)
has_systemd() {
    command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

# Idempotently append a startup snippet to a profile file
append_once() {
    local profile_file="$1"
    local marker="$2"
    local content="$3"

    [ -f "$profile_file" ] || touch "$profile_file"
    grep -qF "$marker" "$profile_file" 2>/dev/null || printf '\n%s\n' "$content" >> "$profile_file"
}

# Locate the sshd binary (often not on the normal PATH, usually in /usr/sbin)
sshd_bin() {
    command -v sshd 2>/dev/null || { [ -x /usr/sbin/sshd ] && echo /usr/sbin/sshd; }
}

# Detect the real ssh service unit name under systemd (Debian/Ubuntu=ssh, RHEL family=sshd)
ssh_service_name() {
    local units=""
    units="$(systemctl list-unit-files 2>/dev/null)"
    if printf '%s' "$units" | grep -q '^ssh\.service'; then
        echo ssh
    elif printf '%s' "$units" | grep -q '^sshd\.service'; then
        echo sshd
    else
        echo ssh
    fi
}

# Idempotently set a global sshd_config option. Insert before the first Match block so
# the setting does not accidentally apply only to a conditional section. awk is used
# instead of sed -i because macOS ships BSD sed with incompatible -i syntax.
set_sshd_option() {
    local file="$1" key="$2" value="$3"
    local tmp=""

    _sudo touch "$file" || return 1
    tmp="$(mktemp)" || return 1
    # shellcheck disable=SC2016 # $0 is evaluated by awk, not by this shell.
    if ! _sudo awk -v key="$key" -v value="$value" '
        BEGIN { in_match = 0; found = 0 }
        /^[[:space:]]*Match[[:space:]]+/ {
            if (!found) {
                print key " " value
                found = 1
            }
            in_match = 1
        }
        !in_match && $0 ~ "^[[:space:]]*" key "[[:space:]]+" {
            if (!found) {
                print key " " value
                found = 1
            }
            next
        }
        { print }
        END {
            if (!found) print key " " value
        }
    ' "$file" > "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    _sudo cp "$tmp" "$file"
    local rc=$?
    rm -f "$tmp"
    return $rc
}

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

# Send a message via the Telegram Bot API
send_telegram() {
    local text="$1"
    if [ -z "$TG_BOT_TOKEN" ] || [ -z "$TG_CHAT_ID" ]; then
        return 1
    fi
    curl -fsS --max-time 15 \
        -X POST "https://api.telegram.org/bot${TG_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT_ID}" \
        --data-urlencode "text=${text}" \
        --data-urlencode "disable_web_page_preview=true" \
        >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# 1 & 2. Install Tailscale + daemon autostart
# ---------------------------------------------------------------------------

install_tailscale_linux() {
    if command -v tailscale >/dev/null 2>&1; then
        log "Tailscale already installed, skipping"
        return 0
    fi

    # Some minimal cloud images lack /etc/apt/sources.list.d, which makes the official
    # script (curl ... | tee /etc/apt/sources.list.d/tailscale.list) fail to write the repo.
    # Create it ahead of time.
    if command -v apt-get >/dev/null 2>&1 || command -v apt >/dev/null 2>&1; then
        _sudo mkdir -p /etc/apt/sources.list.d /usr/share/keyrings 2>/dev/null || true
    fi

    log "Installing Tailscale (official script)..."
    if curl -fsSL https://tailscale.com/install.sh | _sudo sh; then
        command -v tailscale >/dev/null 2>&1 && return 0
    fi
    warn "Official script install failed, falling back to distro package manager"
    local pm=""
    pm="$(detect_pkg_manager)" || { err "No usable package manager found"; return 1; }

    # After the official script fails, the default repos usually lack a tailscale package,
    # so the official repo must be added manually:
    # - RHEL family (incl. OpenCloudOS/Anolis/RockyLinux variants) writes a yum repo
    # - Debian/Ubuntu family writes an apt repo
    case "$pm" in
        dnf|yum)
            add_tailscale_rpm_repo
            ;;
        apt-get|apt)
            add_tailscale_apt_repo || { err "Failed to add Tailscale apt repo"; return 1; }
            ;;
    esac
    pkg_install "$pm" tailscale
}

# Write the official Tailscale yum repo for RHEL-family distros (incl. OpenCloudOS/Anolis etc.)
add_tailscale_rpm_repo() {
    local rhel_ver=""
    rhel_ver="$(rpm -E %rhel 2>/dev/null)"
    if [ -z "$rhel_ver" ] || [ "$rhel_ver" = "%rhel" ]; then
        rhel_ver="$( (. /etc/os-release 2>/dev/null; echo "${VERSION_ID%%.*}") 2>/dev/null)"
    fi
    [ -n "$rhel_ver" ] || rhel_ver=9

    log "Writing official Tailscale yum repo (rhel/${rhel_ver})..."
    if ! _sudo curl -fsSL "https://pkgs.tailscale.com/stable/rhel/${rhel_ver}/tailscale.repo" \
        -o /etc/yum.repos.d/tailscale.repo; then
        warn "Failed to write Tailscale yum repo (rhel/${rhel_ver} may not exist), retrying with rhel/9"
        _sudo curl -fsSL "https://pkgs.tailscale.com/stable/rhel/9/tailscale.repo" \
            -o /etc/yum.repos.d/tailscale.repo || true
    fi
}

# Write the official Tailscale apt repo for Debian/Ubuntu-family distros
add_tailscale_apt_repo() {
    local id="" codename=""
    # Read distro ID (debian/ubuntu) and version codename (bookworm/noble/...)
    id="$( (. /etc/os-release 2>/dev/null; echo "${ID}") )"
    codename="$( (. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME}") )"
    # If a Debian/Ubuntu derivative omits VERSION_CODENAME, fall back to its upstream codename
    [ -n "$codename" ] || codename="$( (. /etc/os-release 2>/dev/null; echo "${UBUNTU_CODENAME}") )"

    case "$id" in
        ubuntu|debian) : ;;
        *) id="ubuntu" ;;   # treat derivatives as ubuntu repo
    esac
    [ -n "$codename" ] || codename="noble"

    log "Writing official Tailscale apt repo (${id}/${codename})..."
    _sudo mkdir -p /etc/apt/sources.list.d /usr/share/keyrings

    if ! _sudo curl -fsSL "https://pkgs.tailscale.com/stable/${id}/${codename}.noarmor.gpg" \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg; then
        warn "Failed to download Tailscale signing key (${id}/${codename})"
        return 1
    fi
    if ! _sudo curl -fsSL "https://pkgs.tailscale.com/stable/${id}/${codename}.tailscale-keyring.list" \
        -o /etc/apt/sources.list.d/tailscale.list; then
        warn "Failed to write Tailscale apt repo (${id}/${codename})"
        return 1
    fi
    _sudo "$(command -v apt-get || command -v apt)" update >/dev/null 2>&1
    return 0
}

install_tailscale_macos() {
    if command -v tailscale >/dev/null 2>&1; then
        log "Tailscale already installed, skipping"
        return 0
    fi
    if command -v brew >/dev/null 2>&1; then
        log "Installing Tailscale via Homebrew..."
        brew install tailscale
    else
        err "Homebrew not detected. Install the official app from https://tailscale.com/download/macos and retry, or install Homebrew first."
        return 1
    fi
}

start_tailscaled_linux() {
    if has_systemd; then
        run_step "Enable tailscaled service" _sudo systemctl enable --now tailscaled
        return 0
    fi

    # No systemd (typical: WSL without systemd) - start tailscaled in the background and set up login autostart
    if ! pgrep -x tailscaled >/dev/null 2>&1; then
        log "No systemd, starting tailscaled in the background..."
        _sudo mkdir -p /var/lib/tailscale /var/run/tailscale
        _sudo sh -c 'nohup tailscaled --state=/var/lib/tailscale/tailscaled.state >/var/log/tailscaled.log 2>&1 &' </dev/null
        sleep 2
    fi

    # Idempotently append an autostart snippet to the shell profile
    local snippet="# >>> tailscale autostart (ssh setup) >>>
if ! pgrep -x tailscaled > /dev/null 2>&1; then
    if [ \"\$(id -u)\" -eq 0 ]; then
        nohup tailscaled --state=/var/lib/tailscale/tailscaled.state > /var/log/tailscaled.log 2>&1 &
    else
        sudo sh -c 'nohup tailscaled --state=/var/lib/tailscale/tailscaled.state > /var/log/tailscaled.log 2>&1 &'
    fi
fi
# <<< tailscale autostart (ssh setup) <<<"
    local profile="$HOME/.bashrc"
    [ -f "$HOME/.bash_profile" ] && profile="$HOME/.bash_profile"
    append_once "$profile" "tailscale autostart (ssh setup)" "$snippet"
}

start_tailscaled_macos() {
    if command -v brew >/dev/null 2>&1 && brew services list >/dev/null 2>&1; then
        run_step "Start Tailscale (brew services)" _sudo brew services start tailscale
    else
        run_step "Install tailscaled system daemon" _sudo tailscaled install-system-daemon
    fi
}

# ---------------------------------------------------------------------------
# 3. Tailscale login
# ---------------------------------------------------------------------------

tailscale_up() {
    local authkey="$1"
    if [ -z "$authkey" ]; then
        err "No Tailscale auth key found. Set the TS_AUTHKEY variable at the top of the script."
        return 1
    fi
    if tailscale status --json 2>/dev/null | grep -Eq '"BackendState"[[:space:]]*:[[:space:]]*"Running"'; then
        log "Tailscale is already connected, skipping login"
        return 0
    fi
    log "Logging in to Tailscale (auth key read, not echoed)..."
    # --ssh enables Tailscale SSH; --accept-dns=false avoids changing local DNS
    # --accept-risk=lose-ssh: if you are currently connected over SSH, enabling Tailscale SSH
    #   warns "may disconnect the current session" and aborts by default (aborted, no changes made).
    #   An unattended script must explicitly skip that guardrail; regular sshd is also enabled,
    #   so even if the session drops it can be reconnected.
    _sudo tailscale up --authkey "$authkey" --ssh --accept-dns=false --accept-risk=lose-ssh
}

# ---------------------------------------------------------------------------
# 4. SSH service
# ---------------------------------------------------------------------------

enable_ssh_linux() {
    # Make sure openssh-server is installed
    if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then
        log "Installing openssh-server..."
        local pm=""
        pm="$(detect_pkg_manager)" || { err "No package manager found, cannot install openssh-server"; return 1; }
        case "$pm" in
            apk) pkg_install "$pm" openssh ;;
            *)   pkg_install "$pm" openssh-server ;;
        esac
    fi

    # The service is named ssh (Debian/Ubuntu) or sshd (RHEL family).
    # Note: on Ubuntu/Debian sshd.service is just an alias of ssh.service, and enabling an
    # alias is rejected by systemd, so prefer the real unit ssh.service.
    if has_systemd; then
        local svc=""
        svc="$(ssh_service_name)"
        run_step "Enable $svc service" _sudo systemctl enable --now "$svc"
        return 0
    fi

    # No systemd: generate host keys and start via service + profile autostart
    _sudo ssh-keygen -A >/dev/null 2>&1 || true
    if command -v service >/dev/null 2>&1; then
        run_step "Start ssh service" _sudo service ssh start
    fi
    local snippet="# >>> sshd autostart (ssh setup) >>>
if ! pgrep -x sshd > /dev/null 2>&1; then
    if [ \"\$(id -u)\" -eq 0 ]; then
        service ssh start > /dev/null 2>&1
    else
        sudo service ssh start > /dev/null 2>&1
    fi
fi
# <<< sshd autostart (ssh setup) <<<"
    local profile="$HOME/.bashrc"
    [ -f "$HOME/.bash_profile" ] && profile="$HOME/.bash_profile"
    append_once "$profile" "sshd autostart (ssh setup)" "$snippet"
}

enable_ssh_macos() {
    # Turn on Remote Login (system sshd), which is itself enabled at boot.
    # -f skips the interactive confirmation (without -f in a non-interactive run, the command
    # waits for yes/no input and effectively does nothing).
    run_step "Enable macOS Remote Login" _sudo systemsetup -f -setremotelogin on

    # Verify it actually turned on; if not, it is usually due to missing Full Disk Access
    local state=""
    state="$(_sudo systemsetup -getremotelogin 2>/dev/null)"
    if printf '%s' "$state" | grep -qi 'On'; then
        log "macOS Remote Login is enabled"
    else
        warn "macOS Remote Login could not be enabled. Grant the terminal running this script (Terminal/iTerm)"
        warn "  System Settings -> Privacy & Security -> Full Disk Access"
        warn "  then re-run, or run manually: sudo systemsetup -f -setremotelogin on"
        FAILED_STEPS+=("Enable macOS Remote Login (not applied, needs Full Disk Access)")
    fi
}

# ---------------------------------------------------------------------------
# 5. authorized_keys
# ---------------------------------------------------------------------------

configure_authorized_keys() {
    if [ ${#SSH_PUBLIC_KEYS[@]} -eq 0 ]; then
        warn "SSH_PUBLIC_KEYS is empty, skipping public key config."
        return 0
    fi

    local ssh_dir="$HOME/.ssh"
    local auth_file="$ssh_dir/authorized_keys"

    mkdir -p "$ssh_dir"
    chmod 700 "$ssh_dir"
    [ -f "$auth_file" ] || touch "$auth_file"
    chmod 600 "$auth_file"

    local added=0
    local line=""
    for line in "${SSH_PUBLIC_KEYS[@]}"; do
        # Strip leading/trailing whitespace
        line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [ -z "$line" ] && continue

        if grep -qF "$line" "$auth_file" 2>/dev/null; then
            continue  # already present, idempotent skip
        fi
        printf '%s\n' "$line" >> "$auth_file"
        added=$((added + 1))
    done

    # Ensure ownership belongs to the current user (also correct when run as root)
    chown "$(id -u):$(id -g)" "$ssh_dir" "$auth_file" 2>/dev/null || true
    log "authorized_keys configured, $added key(s) added this run."
}

# ---------------------------------------------------------------------------
# 6. X11 forwarding (let remote SSH sessions run GUI programs, displaying windows locally)
# ---------------------------------------------------------------------------

# Install xauth (required for sshd to create/manage the .Xauthority cookie; without it
# X11 forwarding silently fails)
install_xauth() {
    if command -v xauth >/dev/null 2>&1; then
        log "xauth already installed, skipping"
        return 0
    fi
    local pm=""
    pm="$(detect_pkg_manager)" || { warn "No package manager found, cannot install xauth"; return 1; }
    log "Installing xauth..."
    case "$pm" in
        apt-get|apt|zypper) pkg_install "$pm" xauth ;;
        dnf|yum)            pkg_install "$pm" xorg-x11-xauth ;;
        pacman)             pkg_install "$pm" xorg-xauth ;;
        apk)                pkg_install "$pm" xauth ;;
        *)                  pkg_install "$pm" xauth ;;
    esac
}

# Restart/reload sshd to apply config (works with systemd / service / no init)
reload_sshd() {
    if has_systemd; then
        local svc=""
        svc="$(ssh_service_name)"
        _sudo systemctl restart "$svc" 2>/dev/null && return 0
    fi
    if command -v service >/dev/null 2>&1; then
        _sudo service ssh restart 2>/dev/null && return 0
    fi
    # No init: send HUP to the sshd master process to reread config (affects new connections only, keeps existing sessions)
    _sudo pkill -HUP -x sshd 2>/dev/null || true
    return 0
}

enable_x11_linux() {
    run_step "Install xauth" install_xauth

    local cfg="/etc/ssh/sshd_config"
    [ -f "$cfg" ] || { warn "$cfg does not exist, skipping X11 config"; return 1; }

    # Prefer a drop-in file (if the main config has Include .../sshd_config.d/*.conf): cleaner, does not touch main file
    local target="$cfg"
    if _sudo grep -Eq '^[[:space:]]*Include[[:space:]]+.*sshd_config\.d/\*\.conf' "$cfg" 2>/dev/null \
        && [ -d /etc/ssh/sshd_config.d ]; then
        target="/etc/ssh/sshd_config.d/10-x11forwarding.conf"
    fi

    log "Enabling X11 forwarding ($target)..."
    set_sshd_option "$target" "X11Forwarding" "yes" || return 1
    set_sshd_option "$target" "X11UseLocalhost" "yes" || return 1

    # Only reload after the config validates, to avoid a broken config that stops sshd from starting
    local sshd=""
    sshd="$(sshd_bin)"
    if [ -n "$sshd" ] && ! _sudo "$sshd" -t 2>/dev/null; then
        warn "sshd config validation failed, skipping reload (please check $target manually)"
        FAILED_STEPS+=("X11 forwarding (sshd -t validation failed)")
        return 1
    fi
    run_step "Reload sshd to apply X11" reload_sshd
}

enable_x11_macos() {
    local cfg="/etc/ssh/sshd_config"
    [ -f "$cfg" ] || { warn "$cfg does not exist, skipping X11 config"; return 1; }
    log "Enabling X11 forwarding ($cfg)..."
    set_sshd_option "$cfg" "X11Forwarding" "yes" || return 1
    set_sshd_option "$cfg" "X11UseLocalhost" "yes" || return 1
    # macOS sshd is launched per-connection by launchd, so new connections read the new config; no explicit restart needed.
    # The client side needs XQuartz to actually display remote windows (server config alone is not enough).
    log "X11 forwarding configured; install XQuartz on the client if you need GUI display."
}

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

main() {
    log "Starting configuration (platform: $OS_TYPE)"
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/sbin:$PATH"

    case "$OS_TYPE" in
        "Darwin")
            run_step "Install Tailscale" install_tailscale_macos
            run_step "Start Tailscale daemon" start_tailscaled_macos
            run_step "Tailscale login" tailscale_up "$TS_AUTHKEY"
            run_step "Enable macOS SSH service" enable_ssh_macos
            run_step "Enable macOS X11 forwarding" enable_x11_macos
            ;;
        "Linux")
            run_step "Install Tailscale" install_tailscale_linux
            run_step "Start Tailscale daemon" start_tailscaled_linux
            run_step "Tailscale login" tailscale_up "$TS_AUTHKEY"
            run_step "Enable Linux SSH service" enable_ssh_linux
            run_step "Enable Linux X11 forwarding" enable_x11_linux
            ;;
        *)
            err "Unsupported platform: $OS_TYPE (this script is for Linux/macOS/WSL; use SETUP.ps1 on Windows)"
            exit 1
            ;;
    esac

    run_step "Configure authorized_keys" configure_authorized_keys

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    echo
    echo "==================== Summary ===================="
    local ts_ip="" ssh_user=""
    ssh_user="$(id -un)"
    ts_ip="$(tailscale ip -4 2>/dev/null | head -n 1)"
    if [ -n "$ts_ip" ]; then
        log "This machine's Tailscale IP: $ts_ip"
        log "Log in from another machine on the tailnet:  ssh $ssh_user@$ts_ip"
    else
        warn "Tailscale IP not available yet, run 'tailscale ip -4' shortly to check (connection may still be establishing)."
    fi

    # -----------------------------------------------------------------------
    # Telegram notification: passwordless SSH login is ready, tell how to log in
    # -----------------------------------------------------------------------
    if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
        local hostname="" login_line="" tg_msg=""
        hostname="$(hostname 2>/dev/null || echo "$OS_TYPE")"
        if [ -n "$ts_ip" ]; then
            login_line="ssh ${ssh_user}@${ts_ip}"
        else
            login_line="ssh ${ssh_user}@<Tailscale-IP>  (run tailscale ip -4 shortly to check)"
        fi
        tg_msg="[OK] Passwordless SSH login configured
Host: ${hostname} (${OS_TYPE})
User: ${ssh_user}
Tailscale IP: ${ts_ip:-pending}

Log in from another machine on the tailnet:
${login_line}"
        if send_telegram "$tg_msg"; then
            log "Telegram notification sent."
        else
            warn "Telegram notification failed (check TG_BOT_TOKEN/TG_CHAT_ID in the script and network)."
        fi
    fi

    if [ ${#FAILED_STEPS[@]} -gt 0 ]; then
        echo
        warn "The following steps did not succeed, please review:"
        local step=""
        for step in "${FAILED_STEPS[@]}"; do
            printf '    - %s\n' "$step" >&2
        done
        echo "=================================================="
        exit 1
    fi

    echo "All steps completed."
    echo "=================================================="
}

main "$@"
