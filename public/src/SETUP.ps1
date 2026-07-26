param(
    [string]$RelaunchWorkingDirectory
)

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

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

try {
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    if (Get-Command chcp -ErrorAction SilentlyContinue) { chcp 65001 > $null 2>&1 }
} catch {}

$TsAuthKey = 'tskey-auth-kiLmAL1dzY11CNTRL-8kBw3rQUum5U8wepNaB6n5KzhgmcHBmkK'
$SshPort = 222
$SshPublicKeys = @(
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB YLX-mac',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ STAR-WSL',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMzwgRkJoTEEh9W8phrGviHsQgWOAFvemnK+gwfqhiN7 yello-PS',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC8taPYDu192wIWnToykJX4lSYVdNNnEe/a970qxauvU lighthouse-linux'
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
        $global:LASTEXITCODE = 0
        & $Action
        if ($global:LASTEXITCODE -ne 0) {
            throw "exit=$global:LASTEXITCODE"
        }
    } catch {
        $FailedSteps.Add("$Desc ($($_.Exception.Message))")
    }
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
    $tsDir = Join-Path $env:ProgramFiles 'Tailscale'
    if ((Test-Path $tsDir) -and ($env:Path -notlike "*$tsDir*")) {
        $env:Path = "$env:Path;$tsDir"
    }
}

function Get-TailscaleExe {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $candidate) { return $candidate }
    return $null
}

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

function Get-PublicIp {
    try {
        $ip = (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 8).ToString().Trim()
        if ($ip -match '^(?:\d{1,3}\.){3}\d{1,3}$') { return $ip }
    } catch {}
    return $null
}

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
            $process = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait -PassThru
            if ($process.ExitCode -ne 0) { throw "msiexec exited with code $($process.ExitCode)" }
        } catch {
            Write-Err "MSI download/install failed: $($_.Exception.Message)"
            throw
        }
    }
    Update-ProcessPath
}

function Enable-TailscaleService {
    $svc = Get-Service -Name Tailscale -ErrorAction Stop
    if ($svc) {
        Set-Service -Name Tailscale -StartupType Automatic -ErrorAction Stop
        if ($svc.Status -ne 'Running') {
            Start-Service -Name Tailscale -ErrorAction Stop
        }
    }
}

function Connect-Tailscale {
    param([string]$AuthKey)
    if (-not $AuthKey) {
        Write-Err 'No Tailscale auth key found. Set the $TsAuthKey variable at the top of the script.'
        throw 'missing-authkey'
    }
    $ts = Get-TailscaleExe
    if (-not $ts) { Write-Err 'tailscale.exe not found'; throw 'tailscale-not-found' }

    $status = & $ts status --json 2>$null
    if ($LASTEXITCODE -eq 0) {
        try {
            if ((($status | ConvertFrom-Json).BackendState) -eq 'Running') {
                Write-Log 'Tailscale is already connected, skipping login'
                return
            }
        } catch {}
    }
    Write-Log 'Logging in to Tailscale (auth key read, not echoed)...'

    & $ts up --reset --authkey $AuthKey --unattended
}

function Enable-OpenSSHServer {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction Stop
    if ($cap -and $cap.State -ne 'Installed') {
        Write-Log 'Installing OpenSSH Server...'
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' -ErrorAction Stop | Out-Null
    } else {
        Write-Log 'OpenSSH Server already installed, skipping'
    }

    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $cfg)) { throw "$cfg not found after OpenSSH Server installation" }
    Set-SshdOption -File $cfg -Key 'Port' -Value $SshPort

    $sshd = Get-Command sshd.exe -ErrorAction SilentlyContinue
    if (-not $sshd) { $sshd = Get-Command sshd -ErrorAction SilentlyContinue }
    if ($sshd) {
        & $sshd.Source -t 2>$null
        if ($LASTEXITCODE -ne 0) { throw "sshd config validation failed (exit=$LASTEXITCODE)" }
    }

    Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop
    $sshdService = Get-Service -Name sshd -ErrorAction Stop
    if ($sshdService.Status -eq 'Running') {
        Restart-Service -Name sshd -ErrorAction Stop
    } else {
        Start-Service -Name sshd -ErrorAction Stop
    }

    Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction Stop

    $ruleName = 'OpenSSH-Server-In-TCP'
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        Remove-NetFirewallRule -Name $ruleName -ErrorAction Stop
    }
    Write-Log "Creating inbound firewall rule (TCP $SshPort)..."
    New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort $SshPort | Out-Null
}

function Set-SshdOption {
    param([string]$File, [string]$Key, [string]$Value)
    if (-not (Test-Path -LiteralPath $File)) { return }
    $lines = @(Get-Content -LiteralPath $File -ErrorAction Stop)
    $pattern = "^\s*$([regex]::Escape($Key))\s+"
    $found = $false
    $inMatch = $false
    $out = foreach ($l in $lines) {
        if ($l -match '^\s*Match\s+') {
            if (-not $found) { $found = $true; "$Key $Value" }
            $inMatch = $true
        }
        if (-not $inMatch -and $l -match $pattern) {
            if (-not $found) { $found = $true; "$Key $Value" }
            continue
        }
        $l
    }
    if (-not $found) { $out = @($out) + "$Key $Value" }
    Set-Content -LiteralPath $File -Value $out -Encoding ASCII -ErrorAction Stop
}

function Set-AuthorizedKeys {
    if (-not $SshPublicKeys -or $SshPublicKeys.Count -eq 0) {
        Write-Warn '$SshPublicKeys is empty, skipping public key config.'
        return
    }

    $isAdminUser = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($isAdminUser) {
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

    if ($isAdminUser) {
        icacls $authFile /inheritance:r | Out-Null
        icacls $authFile /grant 'SYSTEM:F' | Out-Null
        icacls $authFile /grant 'BUILTIN\Administrators:F' | Out-Null
    }

    Write-Log "authorized_keys configured ($authFile), $added key(s) added this run."
}

function Enable-X11Forwarding {
    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $cfg)) {
        Write-Warn "$cfg not found (OpenSSH Server may not have started yet), skipping X11 config."
        return
    }
    Write-Log "Enabling X11 forwarding ($cfg)..."
    Set-SshdOption -File $cfg -Key 'X11Forwarding' -Value 'yes'
    Set-SshdOption -File $cfg -Key 'X11UseLocalhost' -Value 'yes'

    $sshd = Get-Command sshd.exe -ErrorAction SilentlyContinue
    if (-not $sshd) { $sshd = Get-Command sshd -ErrorAction SilentlyContinue }
    if ($sshd) {
        & $sshd.Source -t 2>$null
        if ($LASTEXITCODE -ne 0) { throw "sshd config validation failed (exit=$LASTEXITCODE)" }
    } else {
        Write-Warn 'sshd executable not found on PATH; unable to validate sshd_config before restart.'
    }
    Restart-Service -Name sshd -ErrorAction Stop

    Write-Warn 'Native Windows has no built-in X server; to display remote GUI windows, install VcXsrv / Xming on the client.'
}

Write-Log 'Starting configuration (platform: Windows)'

Invoke-Step 'Install Tailscale'         { Install-Tailscale }
Invoke-Step 'Tailscale service autostart' { Enable-TailscaleService }
Invoke-Step 'Tailscale login'           { Connect-Tailscale -AuthKey $TsAuthKey }
Invoke-Step 'Enable OpenSSH Server'     { Enable-OpenSSHServer }
Invoke-Step 'Configure authorized_keys' { Set-AuthorizedKeys }
Invoke-Step 'Enable X11 forwarding'     { Enable-X11Forwarding }

Write-Host ''
Write-Host '==================== Summary ====================' -ForegroundColor Green
$ts = Get-TailscaleExe
$tsIp = $null
if ($ts) { $tsIp = (& $ts ip -4 2>$null | Select-Object -First 1) }
$publicIp = Get-PublicIp
if ($tsIp) {
    Write-Log "This machine's Tailscale IP: $tsIp"
    Write-Log "Log in from another machine on the tailnet:  ssh -p $SshPort $env:USERNAME@$tsIp"
} else {
    Write-Warn "Tailscale IP not available yet, run 'tailscale ip -4' shortly to check."
}

if ($TgBotToken -and $TgChatId) {
    $tgConfig = @{ Token = $TgBotToken; ChatId = $TgChatId }
    $loginLine = if ($tsIp) {
        "ssh -p $SshPort $env:USERNAME@$tsIp"
    } else {
        "ssh -p $SshPort $env:USERNAME@<Tailscale-IP>  (run tailscale ip -4 shortly to check)"
    }
    $tgMsg = @"
[OK] Passwordless SSH login configured
Host: $env:COMPUTERNAME (Windows)
User: $env:USERNAME
Tailscale IP: $(if ($tsIp) { $tsIp } else { 'pending' })
Public IP: $(if ($publicIp) { $publicIp } else { 'pending' })

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
