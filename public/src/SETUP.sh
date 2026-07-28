#!/bin/bash

OS_TYPE=$(uname -s)

TS_AUTHKEY="tskey-auth-kiLmAL1dzY11CNTRL-8kBw3rQUum5U8wepNaB6n5KzhgmcHBmkK"
SSH_PORT=22
SSH_PREVIOUS_PORT=""
SSH_PUBLIC_KEYS=(
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB YLX-mac"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ STAR-WSL"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMzwgRkJoTEEh9W8phrGviHsQgWOAFvemnK+gwfqhiN7 yello-PS"
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC8taPYDu192wIWnToykJX4lSYVdNNnEe/a970qxauvU lighthouse-linux"
)
TG_BOT_TOKEN="8853032121:AAG0nq0plcOl6oVDRTAzgzAGI3QjlIXv9qI"
TG_CHAT_ID="7765138435"

FAILED_STEPS=()

_sudo() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif sudo -n -v >/dev/null 2>&1; then
        sudo -n "$@"
    else
        sudo "$@"
    fi
}

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

has_systemd() {
    command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]
}

append_once() {
    local profile_file="$1"
    local marker="$2"
    local content="$3"

    [ -f "$profile_file" ] || touch "$profile_file"
    grep -qF "$marker" "$profile_file" 2>/dev/null || printf '\n%s\n' "$content" >> "$profile_file"
}

sshd_bin() {
    command -v sshd 2>/dev/null || { [ -x /usr/sbin/sshd ] && echo /usr/sbin/sshd; }
}

validate_sshd_config() {
    local sshd=""
    sshd="$(sshd_bin)"
    if [ -z "$sshd" ]; then
        err "sshd executable not found; cannot validate SSH configuration"
        return 1
    fi

    log "Validating SSH server configuration..."
    _sudo "$sshd" -t
}

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

tcp_port_listener_state() {
    local port="$1"
    local listening=""

    if command -v ss >/dev/null 2>&1; then
        if ! listening="$(_sudo ss -H -ltnp "sport = :$port" 2>&1)"; then
            warn "Failed to check TCP port $port with ss"
            return 3
        fi
        [ -n "$listening" ] || return 0
        printf '%s\n' "$listening" | grep -q 'users:(("sshd"'
        [ $? -eq 0 ] && return 1
        return 2
    fi

    if command -v lsof >/dev/null 2>&1; then
        if ! listening="$(_sudo lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>&1)"; then
            [ -z "$listening" ] && return 0
            warn "Failed to check TCP port $port with lsof"
            return 3
        fi
        printf '%s\n' "$listening" | awk 'NR > 1 && $1 == "sshd" { found = 1 } END { exit !found }'
        [ $? -eq 0 ] && return 1
        return 2
    fi

    if [ "$OS_TYPE" = "Linux" ] && command -v netstat >/dev/null 2>&1; then
        if ! listening="$(_sudo netstat -ltnp 2>&1)"; then
            warn "Failed to check TCP port $port with netstat"
            return 3
        fi
        printf '%s\n' "$listening" | awk -v port="$port" '
            $6 == "LISTEN" && $4 ~ ("[.:]" port "$") {
                found = 1
                if ($7 ~ /\/sshd$/) sshd = 1
            }
            END {
                if (!found) exit 0
                if (sshd) exit 1
                exit 2
            }
        '
        case $? in
            0) return 0 ;;
            1) return 1 ;;
            2) return 2 ;;
        esac
    fi

    warn "Cannot identify the TCP port listener: ss/lsof (or Linux netstat) is unavailable"
    return 3
}

select_ssh_port() {
    tcp_port_listener_state 22
    local primary_rc=$?
    case "$primary_rc" in
        0)
            SSH_PORT=22
            tcp_port_listener_state 222
            if [ $? -eq 1 ]; then
                SSH_PREVIOUS_PORT=222
                log "TCP port 22 is available; migrating SSH from TCP 222 to TCP 22"
            else
                log "TCP port 22 is available; configuring SSH on port $SSH_PORT"
            fi
            return 0
            ;;
        1)
            SSH_PORT=22
            log "TCP port 22 is already used by sshd; keeping SSH on port $SSH_PORT"
            return 0
            ;;
        2)
            ;;
        *)
            err "Cannot identify the process listening on TCP port 22"
            return 1
            ;;
    esac

    tcp_port_listener_state 222
        local fallback_rc=$?
    case "$fallback_rc" in
        0)
            SSH_PORT=222
            warn "TCP port 22 is in use by another process; configuring SSH on port $SSH_PORT"
            return 0
            ;;
        1)
            SSH_PORT=222
            log "TCP port 222 is already used by sshd; keeping SSH on port $SSH_PORT"
            return 0
            ;;
        2)
            err "TCP ports 22 and 222 are both in use; cannot configure SSH"
            return 1
            ;;
        *)
            err "Cannot identify the process listening on TCP port 222"
            return 1
            ;;
    esac
}

sshd_is_healthy() {
    validate_sshd_config || return 1

    tcp_port_listener_state "$SSH_PORT"
    local listener_rc=$?
    if [ "$listener_rc" -ne 1 ]; then
        err "sshd is not listening on TCP port $SSH_PORT"
        return 1
    fi

    if [ "$SSH_PORT" = 22 ] && [ "$SSH_PREVIOUS_PORT" = 222 ]; then
        tcp_port_listener_state 222
        local previous_listener_rc=$?
        if [ "$previous_listener_rc" -eq 1 ]; then
            err "sshd is still listening on the previous TCP port 222 after migration to TCP 22"
            return 1
        fi
    fi
}

configure_ssh_port() {
    local cfg="/etc/ssh/sshd_config"
    [ -f "$cfg" ] || { warn "$cfg does not exist, cannot configure SSH port"; return 1; }

    local target="$cfg"
    if _sudo grep -Eq '^[[:space:]]*Include[[:space:]]+.*sshd_config\.d/\*\.conf' "$cfg" 2>/dev/null \
        && [ -d /etc/ssh/sshd_config.d ]; then
        target="/etc/ssh/sshd_config.d/10-ssh-port.conf"
    fi

    log "Configuring SSH port $SSH_PORT ($target)..."
    set_sshd_option "$target" "Port" "$SSH_PORT" || return 1
    set_sshd_option "$target" "PasswordAuthentication" "no" || return 1
    set_sshd_option "$target" "KbdInteractiveAuthentication" "no" || return 1
}

set_sshd_option() {
    local file="$1" key="$2" value="$3"
    local tmp=""

    _sudo touch "$file" || return 1
    tmp="$(mktemp)" || return 1
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

get_public_ip() {
    local ip=""
    ip="$(curl -4fsS --max-time 8 https://api.ipify.org 2>/dev/null)" || return 1
    if printf '%s' "$ip" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
        printf '%s\n' "$ip"
        return 0
    fi
    return 1
}

install_tailscale_linux() {
    if command -v tailscale >/dev/null 2>&1; then
        log "Tailscale already installed, skipping"
        return 0
    fi

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

add_tailscale_apt_repo() {
    local id="" codename=""
    id="$( (. /etc/os-release 2>/dev/null; echo "${ID}") )"
    codename="$( (. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME}") )"
    [ -n "$codename" ] || codename="$( (. /etc/os-release 2>/dev/null; echo "${UBUNTU_CODENAME}") )"

    case "$id" in
        ubuntu|debian) : ;;
        *) id="ubuntu" ;;
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

tailscale_cli_works() {
    command -v tailscale >/dev/null 2>&1 && tailscale version >/dev/null 2>&1
}

remove_broken_tailscale_shim() {
    local p=""
    for p in /usr/local/bin/tailscale /usr/local/bin/tailscaled; do
        if [ -f "$p" ] && grep -q 'Tailscale.app' "$p" 2>/dev/null; then
            warn "Removing stale Tailscale.app shim: $p"
            _sudo rm -f "$p"
        fi
    done
    hash -r 2>/dev/null || true
}

install_tailscale_macos() {
    if tailscale_cli_works; then
        log "Tailscale already installed, skipping"
        return 0
    fi

    if command -v tailscale >/dev/null 2>&1; then
        warn "Found a non-working 'tailscale' command (likely a leftover Tailscale.app shim)."
        remove_broken_tailscale_shim
    fi

    if command -v brew >/dev/null 2>&1; then
        log "Installing Tailscale via Homebrew..."
        brew install tailscale
        hash -r 2>/dev/null || true
        tailscale_cli_works && return 0
        warn "tailscale CLI not on PATH; attempting 'brew link --overwrite tailscale'..."
        brew link --overwrite tailscale >/dev/null 2>&1 || true
        hash -r 2>/dev/null || true
        tailscale_cli_works && return 0
        err "Homebrew reported success but the tailscale CLI still does not run."
        return 1
    fi
    err "Homebrew not detected. Install the official app from https://tailscale.com/download/macos and retry, or install Homebrew first."
    return 1
}

start_tailscaled_linux() {
    if has_systemd; then
        run_step "Enable tailscaled service" _sudo systemctl enable --now tailscaled
        return 0
    fi

    if ! pgrep -x tailscaled >/dev/null 2>&1; then
        log "No systemd, starting tailscaled in the background..."
        _sudo mkdir -p /var/lib/tailscale /var/run/tailscale
        _sudo sh -c 'nohup tailscaled --state=/var/lib/tailscale/tailscaled.state >/var/log/tailscaled.log 2>&1 &' </dev/null
        sleep 2
    fi

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

tailscaled_running() {
    tailscale status --json 2>/dev/null | grep -q '"BackendState"'
}

wait_tailscaled_ready() {
    local i=0
    while [ "$i" -lt 20 ]; do
        tailscaled_running && return 0
        sleep 0.5
        i=$((i + 1))
    done
    return 1
}

tailscaled_bin() {
    command -v tailscaled 2>/dev/null && return 0
    local p=""
    for p in /opt/homebrew/bin/tailscaled /usr/local/bin/tailscaled; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

start_tailscaled_macos() {
    if tailscaled_running; then
        log "tailscaled already running, skipping daemon start"
        return 0
    fi

    local tsd=""
    tsd="$(tailscaled_bin)"
    if [ -z "$tsd" ]; then
        warn "tailscaled binary not found; cannot start the daemon."
        warn "  Install the Tailscale CLI daemon with: brew install tailscale"
        FAILED_STEPS+=("Start Tailscale daemon (tailscaled not found)")
        return 0
    fi

    run_step "Install tailscaled system daemon" _sudo "$tsd" install-system-daemon
    if wait_tailscaled_ready; then
        log "tailscaled system daemon is up"
    else
        warn "tailscaled did not become ready in time; login may fail on this run (re-run the script if so)."
    fi
}

tailscale_bin() {
    command -v tailscale 2>/dev/null && return 0
    local p=""
    for p in /opt/homebrew/bin/tailscale /usr/local/bin/tailscale; do
        [ -x "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

tailscale_up() {
    local authkey="$1"
    if [ -z "$authkey" ]; then
        err "No Tailscale auth key found. Set the TS_AUTHKEY variable at the top of the script."
        return 1
    fi
    local ts=""
    ts="$(tailscale_bin)"
    if [ -z "$ts" ]; then
        err "tailscale CLI not found; cannot log in."
        return 1
    fi
    if "$ts" status --json 2>/dev/null | grep -Eq '"BackendState"[[:space:]]*:[[:space:]]*"Running"'; then
        log "Tailscale is already connected, skipping login"
        return 0
    fi
    log "Logging in to Tailscale (auth key read, not echoed)..."
    _sudo "$ts" up --reset --authkey "$authkey" --ssh --accept-dns=false --accept-risk=lose-ssh
}

restart_or_start_sshd() {
    local svc=""
    if has_systemd; then
        svc="$(ssh_service_name)"
        _sudo systemctl enable "$svc" || return 1
        if _sudo systemctl is-active --quiet "$svc"; then
            log "Restarting $svc to apply SSH configuration..."
            _sudo systemctl restart "$svc"
        else
            log "Starting $svc..."
            _sudo systemctl start "$svc"
        fi
        return $?
    fi

    if command -v service >/dev/null 2>&1; then
        _sudo service ssh restart 2>/dev/null \
            || _sudo service sshd restart 2>/dev/null \
            || _sudo service ssh start 2>/dev/null \
            || _sudo service sshd start 2>/dev/null
        return $?
    fi

    err "No service command found; cannot start sshd"
    return 1
}

enable_ssh_linux() {
    if ! command -v sshd >/dev/null 2>&1 && [ ! -x /usr/sbin/sshd ]; then
        log "Installing openssh-server..."
        local pm=""
        pm="$(detect_pkg_manager)" || { err "No package manager found, cannot install openssh-server"; return 1; }
        case "$pm" in
            apk) pkg_install "$pm" openssh ;;
            *)   pkg_install "$pm" openssh-server ;;
        esac
    fi

    select_ssh_port || return 1
    configure_ssh_port || return 1
    validate_sshd_config || return 1

    _sudo ssh-keygen -A >/dev/null 2>&1 || true
    restart_or_start_sshd || return 1
    sshd_is_healthy || return 1
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

configure_public_ssh_firewall_linux() {
    sshd_is_healthy || return 1

    if command -v ufw >/dev/null 2>&1 \
        && _sudo ufw status 2>/dev/null | grep -q '^Status: active'; then
        log "Allowing public SSH traffic through UFW (TCP $SSH_PORT)..."
        _sudo ufw allow "${SSH_PORT}/tcp"
        return $?
    fi

    if command -v firewall-cmd >/dev/null 2>&1 \
        && _sudo firewall-cmd --state >/dev/null 2>&1; then
        log "Allowing public SSH traffic through firewalld (TCP $SSH_PORT)..."
        _sudo firewall-cmd --add-port="${SSH_PORT}/tcp" || return 1
        _sudo firewall-cmd --permanent --add-port="${SSH_PORT}/tcp"
        return $?
    fi

    log "No active UFW or firewalld detected; SSH remains available on all listening interfaces."
}

enable_ssh_macos() {
    select_ssh_port || return 1
    configure_ssh_port || return 1
    validate_sshd_config || return 1
    run_step "Enable macOS Remote Login" _sudo systemsetup -f -setremotelogin on

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

configure_authorized_keys() {
    if [ ${#SSH_PUBLIC_KEYS[@]} -eq 0 ]; then
        err "SSH_PUBLIC_KEYS is empty; refusing to disable password authentication without a public key"
        return 1
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
        line="$(printf '%s' "$line" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
        [ -z "$line" ] && continue

        if grep -qF "$line" "$auth_file" 2>/dev/null; then
            continue
        fi
        printf '%s\n' "$line" >> "$auth_file"
        added=$((added + 1))
    done

    chown "$(id -u):$(id -g)" "$ssh_dir" "$auth_file" 2>/dev/null || true
    log "authorized_keys configured, $added key(s) added this run."
}

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

reload_sshd() {
    if has_systemd; then
        local svc=""
        svc="$(ssh_service_name)"
        _sudo systemctl restart "$svc" 2>/dev/null && return 0
    fi
    if command -v service >/dev/null 2>&1; then
        _sudo service ssh restart 2>/dev/null && return 0
    fi
    _sudo pkill -HUP -x sshd 2>/dev/null || true
    return 0
}

enable_x11_linux() {
    run_step "Install xauth" install_xauth

    local cfg="/etc/ssh/sshd_config"
    [ -f "$cfg" ] || { warn "$cfg does not exist, skipping X11 config"; return 1; }

    local target="$cfg"
    if _sudo grep -Eq '^[[:space:]]*Include[[:space:]]+.*sshd_config\.d/\*\.conf' "$cfg" 2>/dev/null \
        && [ -d /etc/ssh/sshd_config.d ]; then
        target="/etc/ssh/sshd_config.d/10-x11forwarding.conf"
    fi

    log "Enabling X11 forwarding ($target)..."
    set_sshd_option "$target" "X11Forwarding" "yes" || return 1
    set_sshd_option "$target" "X11UseLocalhost" "yes" || return 1

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
    log "X11 forwarding configured; install XQuartz on the client if you need GUI display."
}

main() {
    log "Starting configuration (platform: $OS_TYPE)"
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/sbin:$PATH"

    configure_authorized_keys || { err "authorized_keys configuration failed; aborting before enabling SSH"; exit 1; }

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
            run_step "Allow public Linux SSH" configure_public_ssh_firewall_linux
            run_step "Enable Linux X11 forwarding" enable_x11_linux
            ;;
        *)
            err "Unsupported platform: $OS_TYPE (this script is for Linux/macOS/WSL; use SETUP.ps1 on Windows)"
            exit 1
            ;;
    esac

    echo
    echo "==================== Summary ===================="
    local ts_ip="" public_ip="" ssh_user=""
    ssh_user="$(id -un)"
    ts_ip="$(tailscale ip -4 2>/dev/null | head -n 1)"
    public_ip="$(get_public_ip 2>/dev/null || true)"
    if [ -n "$ts_ip" ]; then
        log "This machine's Tailscale IP: $ts_ip"
        log "Log in from another machine on the tailnet:  ssh -p $SSH_PORT $ssh_user@$ts_ip"
    else
        warn "Tailscale IP not available yet, run 'tailscale ip -4' shortly to check (connection may still be establishing)."
    fi

    if [ -n "$TG_BOT_TOKEN" ] && [ -n "$TG_CHAT_ID" ]; then
        local hostname="" login_line="" tg_msg=""
        hostname="$(hostname 2>/dev/null || echo "$OS_TYPE")"
        if [ -n "$ts_ip" ]; then
            login_line="ssh -p ${SSH_PORT} ${ssh_user}@${ts_ip}"
        else
            login_line="ssh -p ${SSH_PORT} ${ssh_user}@<Tailscale-IP>  (run tailscale ip -4 shortly to check)"
        fi
        tg_msg="[OK] Passwordless SSH login configured
Host: ${hostname} (${OS_TYPE})
User: ${ssh_user}
Tailscale IP: ${ts_ip:-pending}
Public IP: ${public_ip:-pending}

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
