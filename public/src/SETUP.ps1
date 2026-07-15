param(
    [string]$RelaunchWorkingDirectory
)

# ---------------------------------------------------------------------------
# Self-elevation: relaunch as Administrator if not already elevated
# ---------------------------------------------------------------------------
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Definition }

    $psExe = (Get-Process -Id $PID).Path
    if (-not $psExe) { $psExe = 'powershell.exe' }

    $quote = { param($v) '"' + ($v -replace '"', '\"') + '"' }

    $workDir = if ($PWD.Path) { $PWD.Path } else { '' }
    $relaunchArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', (& $quote $scriptPath),
        '-RelaunchWorkingDirectory', (& $quote $workDir)
    )

    try {
        $elevated = Start-Process -FilePath $psExe -ArgumentList $relaunchArgs `
            -Verb RunAs -Wait -PassThru
        $code = if ($null -ne $elevated.ExitCode) { $elevated.ExitCode } else { 0 }
        exit $code
    } catch {
        Write-Host '[ERROR] Administrator privileges required; elevation was cancelled or blocked.' -ForegroundColor Red
        exit 1
    }
}

if ($RelaunchWorkingDirectory -and (Test-Path -LiteralPath $RelaunchWorkingDirectory -PathType Container)) {
    Set-Location -LiteralPath $RelaunchWorkingDirectory
}

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# Force UTF-8 console output so any non-ASCII text is not mangled under GBK/437 code pages.
# (Paired with the file's own UTF-8 BOM, this covers both "powershell -File" and "iwr | iex".)
try {
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if (Get-Command chcp -ErrorAction SilentlyContinue) { chcp 65001 > $null 2>&1 }
} catch {}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
$TsAuthKey = 'tskey-auth-kiLmAL1dzY11CNTRL-8kBw3rQUum5U8wepNaB6n5KzhgmcHBmkK'  # expires: 2026-10-05 / Tags: fish
$SshPublicKeys = @(
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB YLX-mac',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ STAR-WSL',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMzwgRkJoTEEh9W8phrGviHsQgWOAFvemnK+gwfqhiN7 yello-PS'
)
$TgBotToken = '8853032121:AAG0nq0plcOl6oVDRTAzgzAGI3QjlIXv9qI'
$TgChatId   = '7765138435'

$FailedSteps = New-Object System.Collections.Generic.List[string]

function Write-Log  { param($m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Warn { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "[ERROR] $m" -ForegroundColor Red }

function Invoke-Step {
    param(
        [string]$Desc,
        [scriptblock]$Action
    )
    try {
        & $Action
        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
            $FailedSteps.Add("$Desc (exit=$LASTEXITCODE)")
        }
    } catch {
        $FailedSteps.Add("$Desc ($($_.Exception.Message))")
    }
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# Refresh the current process PATH (needed after installing Tailscale)
function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
    # Tailscale default install dir, make sure it is callable
    $tsDir = Join-Path $env:ProgramFiles 'Tailscale'
    if ((Test-Path $tsDir) -and ($env:Path -notlike "*$tsDir*")) {
        $env:Path = "$env:Path;$tsDir"
    }
}

# Locate tailscale.exe
function Get-TailscaleExe {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $candidate) { return $candidate }
    return $null
}

# Send a message via the Telegram Bot API
function Send-Telegram {
    param(
        [hashtable]$Config,
        [string]$Text
    )
    if (-not $Config) { return $false }
    try {
        $uri = "https://api.telegram.org/bot$($Config.Token)/sendMessage"
        $body = @{
            chat_id                  = $Config.ChatId
            text                     = $Text
            disable_web_page_preview = $true
        }
        Invoke-RestMethod -Uri $uri -Method Post -Body $body -TimeoutSec 15 | Out-Null
        return $true
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# 1 & 2. Install Tailscale + service autostart
# ---------------------------------------------------------------------------
function Install-Tailscale {
    if (Get-TailscaleExe) {
        Write-Log 'Tailscale already installed, skipping'
        return
    }

    if (Test-CommandExists 'winget') {
        Write-Log 'Installing Tailscale via winget...'
        winget install --id Tailscale.Tailscale -e --silent `
            --accept-source-agreements --accept-package-agreements
    } else {
        Write-Log 'winget unavailable, downloading official MSI for silent install...'
        $msi = Join-Path $env:TEMP 'tailscale-setup.msi'
        try {
            Invoke-WebRequest -Uri 'https://pkgs.tailscale.com/stable/tailscale-setup-latest.msi' `
                -OutFile $msi -UseBasicParsing
            Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait
        } catch {
            Write-Err "MSI download/install failed: $($_.Exception.Message)"
            throw
        }
    }
    Update-ProcessPath
}

function Enable-TailscaleService {
    $svc = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
    if ($svc) {
        Set-Service -Name Tailscale -StartupType Automatic -ErrorAction SilentlyContinue
        if ($svc.Status -ne 'Running') {
            Start-Service -Name Tailscale -ErrorAction SilentlyContinue
        }
    } else {
        Write-Warn 'Tailscale service not found (install may not have finished), skipping service config.'
    }
}

# ---------------------------------------------------------------------------
# 3. Tailscale login
# ---------------------------------------------------------------------------
function Connect-Tailscale {
    param([string]$AuthKey)
    if (-not $AuthKey) {
        Write-Err 'No Tailscale auth key found. Set the $TsAuthKey variable at the top of the script.'
        throw 'missing-authkey'
    }
    $ts = Get-TailscaleExe
    if (-not $ts) { Write-Err 'tailscale.exe not found'; throw 'tailscale-not-found' }

    Write-Log 'Logging in to Tailscale (auth key read, not echoed)...'
    # --unattended: stay connected without interaction after reboot
    & $ts up --authkey $AuthKey --unattended
}

# ---------------------------------------------------------------------------
# 4. OpenSSH Server
# ---------------------------------------------------------------------------
function Enable-OpenSSHServer {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue
    if ($cap -and $cap.State -ne 'Installed') {
        Write-Log 'Installing OpenSSH Server...'
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    } else {
        Write-Log 'OpenSSH Server already installed, skipping'
    }

    # First start generates host keys and creates the default sshd_config
    Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name sshd -ErrorAction SilentlyContinue

    # Also set ssh-agent to automatic (optional, convenient for key management)
    Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue

    # Firewall: allow inbound port 22
    $ruleName = 'OpenSSH-Server-In-TCP'
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if (-not $rule) {
        Write-Log 'Creating inbound firewall rule (TCP 22)...'
        New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    } else {
        Enable-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    }
}

# Idempotently set an option in sshd_config: replace an existing (uncommented) line, else append
function Set-SshdOption {
    param([string]$File, [string]$Key, [string]$Value)
    if (-not (Test-Path -LiteralPath $File)) { return }
    $lines = @(Get-Content -LiteralPath $File -ErrorAction SilentlyContinue)
    $pattern = "^\s*$([regex]::Escape($Key))\s+"
    $found = $false
    $out = foreach ($l in $lines) {
        if ($l -match $pattern) { $found = $true; "$Key $Value" } else { $l }
    }
    if (-not $found) { $out = @($out) + "$Key $Value" }
    Set-Content -LiteralPath $File -Value $out -Encoding ASCII
}

# ---------------------------------------------------------------------------
# 5. authorized_keys
# ---------------------------------------------------------------------------
function Set-AuthorizedKeys {
    if (-not $SshPublicKeys -or $SshPublicKeys.Count -eq 0) {
        Write-Warn '$SshPublicKeys is empty, skipping public key config.'
        return
    }

    # Determine whether the current user is in the Administrators group
    $isAdminUser = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($isAdminUser) {
        # Special file that Windows OpenSSH uses for the Administrators group
        $authFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        $authDir = Split-Path -Parent $authFile
        if (-not (Test-Path $authDir)) { New-Item -ItemType Directory -Path $authDir -Force | Out-Null }
    } else {
        $sshDir = Join-Path $env:USERPROFILE '.ssh'
        if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }
        $authFile = Join-Path $sshDir 'authorized_keys'
    }

    if (-not (Test-Path $authFile)) { New-Item -ItemType File -Path $authFile -Force | Out-Null }

    $existing = @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue)
    $added = 0
    foreach ($raw in $SshPublicKeys) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($existing -contains $line) { continue }
        Add-Content -LiteralPath $authFile -Value $line
        $existing += $line
        $added++
    }

    # Tighten permissions: administrators_authorized_keys allows only SYSTEM + Administrators
    if ($isAdminUser) {
        icacls $authFile /inheritance:r | Out-Null
        icacls $authFile /grant 'SYSTEM:F' | Out-Null
        icacls $authFile /grant 'BUILTIN\Administrators:F' | Out-Null
    }

    Write-Log "authorized_keys configured ($authFile), $added key(s) added this run."
}

# ---------------------------------------------------------------------------
# 6. X11 forwarding
# ---------------------------------------------------------------------------
function Enable-X11Forwarding {
    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $cfg)) {
        Write-Warn "$cfg not found (OpenSSH Server may not have started yet), skipping X11 config."
        return
    }
    Write-Log "Enabling X11 forwarding ($cfg)..."
    Set-SshdOption -File $cfg -Key 'X11Forwarding' -Value 'yes'
    Set-SshdOption -File $cfg -Key 'X11UseLocalhost' -Value 'yes'

    # Restart sshd to apply the config
    Restart-Service -Name sshd -ErrorAction SilentlyContinue

    # Native Windows has no built-in xauth or X server: the server side is enabled, but the
    # client (or this machine acting as client) needs an X server (VcXsrv / Xming) to actually
    # display forwarded graphical windows.
    Write-Warn 'Native Windows has no built-in X server; to display remote GUI windows, install VcXsrv / Xming on the client.'
}

# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------
Write-Log 'Starting configuration (platform: Windows)'

Invoke-Step 'Install Tailscale'         { Install-Tailscale }
Invoke-Step 'Tailscale service autostart' { Enable-TailscaleService }
Invoke-Step 'Tailscale login'           { Connect-Tailscale -AuthKey $TsAuthKey }
Invoke-Step 'Enable OpenSSH Server'     { Enable-OpenSSHServer }
Invoke-Step 'Configure authorized_keys' { Set-AuthorizedKeys }
Invoke-Step 'Enable X11 forwarding'     { Enable-X11Forwarding }

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '==================== Summary ====================' -ForegroundColor Green
$ts = Get-TailscaleExe
$tsIp = $null
if ($ts) { $tsIp = (& $ts ip -4 2>$null | Select-Object -First 1) }
if ($tsIp) {
    Write-Log "This machine's Tailscale IP: $tsIp"
    Write-Log "Log in from another machine on the tailnet:  ssh $env:USERNAME@$tsIp"
} else {
    Write-Warn "Tailscale IP not available yet, run 'tailscale ip -4' shortly to check."
}

# ---------------------------------------------------------------------------
# Telegram notification: passwordless SSH login is ready, tell how to log in
# ---------------------------------------------------------------------------
if ($TgBotToken -and $TgChatId) {
    $tgConfig = @{ Token = $TgBotToken; ChatId = $TgChatId }
    $loginLine = if ($tsIp) {
        "ssh $env:USERNAME@$tsIp"
    } else {
        "ssh $env:USERNAME@<Tailscale-IP>  (run tailscale ip -4 shortly to check)"
    }
    $tgMsg = @"
[OK] Passwordless SSH login configured
Host: $env:COMPUTERNAME (Windows)
User: $env:USERNAME
Tailscale IP: $(if ($tsIp) { $tsIp } else { 'pending' })

Log in from another machine on the tailnet:
$loginLine
"@
    if (Send-Telegram -Config $tgConfig -Text $tgMsg) {
        Write-Log 'Telegram notification sent.'
    } else {
        Write-Warn 'Telegram notification failed (check $TgBotToken/$TgChatId in the script and network).'
    }
}

if ($FailedSteps.Count -gt 0) {
    Write-Host ''
    Write-Warn 'The following steps did not succeed, please review:'
    foreach ($s in $FailedSteps) { Write-Host "    - $s" -ForegroundColor Yellow }
    Write-Host '==================================================' -ForegroundColor Green
    exit 1
}

Write-Host 'All steps completed.' -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green
