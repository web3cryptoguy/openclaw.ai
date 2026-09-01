param(
    [string]$RelaunchWorkingDirectory,
    [string]$InteractiveUserName,
    [switch]$LibraryOnly
)

$normalizeInteractiveUserName = {
    param([AllowNull()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $name = $Value.Trim()
    if ($name -match '\\([^\\]+)$') { $name = $Matches[1] }
    if ($name -in @('SYSTEM', 'LOCAL SERVICE', 'NETWORK SERVICE') -or $name -match '\$$') {
        return $null
    }
    return $name
}

$InteractiveUserName = & $normalizeInteractiveUserName $InteractiveUserName
if (-not $InteractiveUserName) {
    $InteractiveUserName = & $normalizeInteractiveUserName ([string]$env:USERNAME)
}
if (-not $InteractiveUserName) {
    try {
        $InteractiveUserName = & $normalizeInteractiveUserName ([string](Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).UserName)
    } catch {}
}

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
    if ($InteractiveUserName) {
        $relaunchArgs += @('-InteractiveUserName', (& $quote $InteractiveUserName))
    }
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
$SshTargetUserName = $null # Resolved from the built-in account's RID 500 below.
$SshCurrentUserName = $InteractiveUserName
$SshTargetAuthorizedKeysFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
$script:SshTargetSid = $null
$script:SshTargetUserNames = @()
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

function Get-NativeProgramFiles {
    foreach ($candidate in @(
        $env:ProgramW6432,
        [Environment]::GetEnvironmentVariable('ProgramW6432', 'Machine'),
        $env:ProgramFiles
    )) {
        if ($candidate) { return $candidate }
    }
    throw 'Unable to determine the native Program Files directory'
}

function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'

    $tailscaleDirs = @(
        (Join-Path (Get-NativeProgramFiles) 'Tailscale'),
        (Join-Path $env:ProgramFiles 'Tailscale')
    ) | Select-Object -Unique
    foreach ($tsDir in $tailscaleDirs) {
        if ((Test-Path -LiteralPath $tsDir) -and ($env:Path -notlike "*$tsDir*")) {
            $env:Path = "$env:Path;$tsDir"
        }
    }
}

function Get-TailscaleExe {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        (Join-Path (Get-NativeProgramFiles) 'Tailscale\tailscale.exe'),
        (Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'),
        $(if (${env:ProgramFiles(x86)}) {
            Join-Path ${env:ProgramFiles(x86)} 'Tailscale\tailscale.exe'
        })
    ) | Where-Object { $_ } | Select-Object -Unique
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Split-TelegramText {
    param(
        [string]$Text,
        [int]$MaxLength = 3800
    )

    if ([string]::IsNullOrEmpty($Text)) { return @() }
    if ($MaxLength -lt 100) { throw 'Telegram message chunk size is too small' }

    $chunks = New-Object System.Collections.Generic.List[string]
    $remaining = $Text
    while ($remaining.Length -gt $MaxLength) {
        $splitAt = $remaining.LastIndexOf("`n", $MaxLength)
        if ($splitAt -lt [Math]::Floor($MaxLength / 2)) {
            $splitAt = $MaxLength
        }
        $chunk = $remaining.Substring(0, $splitAt).TrimEnd([char[]]@("`r", "`n"))
        if ($chunk) { $chunks.Add($chunk) }
        $remaining = $remaining.Substring($splitAt).TrimStart([char[]]@("`r", "`n"))
    }
    if ($remaining) { $chunks.Add($remaining) }
    return @($chunks)
}

function Send-Telegram {
    param(
        [hashtable]$Config,
        [string]$Text
    )
    if (-not $Config -or [string]::IsNullOrWhiteSpace($Text)) { return $false }
    try {
        $uri = "https://api.telegram.org/bot$($Config.Token)/sendMessage"
        $chunks = @(Split-TelegramText -Text $Text)
        for ($index = 0; $index -lt $chunks.Count; $index++) {
            $payload = if ($chunks.Count -gt 1) {
                "[$($index + 1)/$($chunks.Count)]`n$($chunks[$index])"
            } else {
                $chunks[$index]
            }
            $body = @{
                chat_id                  = $Config.ChatId
                text                     = $payload
                disable_web_page_preview = $true
            }
            Invoke-RestMethod -Uri $uri -Method Post -Body $body -TimeoutSec 15 | Out-Null
        }
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
    $architectureCandidates = New-Object System.Collections.Generic.List[string]

    try {
        $architectureCandidates.Add(
            [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        )
    } catch {}

    try {
        $nativeEnvironment = Get-ItemProperty `
            -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment' `
            -Name PROCESSOR_ARCHITECTURE -ErrorAction Stop
        $architectureCandidates.Add($nativeEnvironment.PROCESSOR_ARCHITECTURE)
    } catch {}

    foreach ($candidate in @($env:PROCESSOR_ARCHITEW6432, $env:PROCESSOR_ARCHITECTURE)) {
        if ($candidate) { $architectureCandidates.Add($candidate) }
    }

    foreach ($candidate in $architectureCandidates) {
        switch -Regex ($candidate) {
            '^(AMD64|X64)$' { return 'amd64' }
            '^ARM64$'       { return 'arm64' }
            '^(x86|X86)$'   { return 'x86' }
        }
    }

    throw "Unsupported or unknown Windows architecture: $($architectureCandidates -join ', ')"
}

function Get-NativeMsiexec {
    if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        $sysnativeMsiexec = Join-Path $env:WINDIR 'Sysnative\msiexec.exe'
        if (Test-Path -LiteralPath $sysnativeMsiexec -PathType Leaf) {
            return $sysnativeMsiexec
        }
    }

    $systemMsiexec = Join-Path $env:WINDIR 'System32\msiexec.exe'
    if (Test-Path -LiteralPath $systemMsiexec -PathType Leaf) {
        return $systemMsiexec
    }
    return 'msiexec.exe'
}

function Install-TailscaleStandalone {
    $arch = Get-TailscaleMsiArchitecture
    $installer = Join-Path $env:TEMP "tailscale-setup-latest-$arch.msi"
    $msiLog = Join-Path $env:TEMP "tailscale-msi-install-$arch.log"
    $msiexec = Get-NativeMsiexec
    Write-Log "Downloading the official Tailscale MSI (native architecture: $arch)..."
    try {
        Invoke-WebRequest -Uri "https://pkgs.tailscale.com/stable/tailscale-setup-latest-$arch.msi" `
            -OutFile $installer -UseBasicParsing
        $process = Start-Process -FilePath $msiexec -Wait -PassThru -ArgumentList @(
            '/i', ('"' + $installer + '"'),
            '/quiet', '/norestart',
            '/l*v', ('"' + $msiLog + '"')
        )
        # 3010 is success with a pending reboot; Tailscale is usable without it.
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Tailscale MSI installer exited with code $($process.ExitCode) (log: $msiLog)"
        }
    } catch {
        Update-ProcessPath
        if (Get-TailscaleExe) {
            Write-Warn "The Tailscale MSI returned an error, but tailscale.exe is installed; continuing. Installer detail: $($_.Exception.Message)"
            return
        }
        Write-Err "Tailscale installer download/run failed: $($_.Exception.Message)"
        throw
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

function Install-TailscaleBootstrapper {
    $installer = Join-Path $env:TEMP 'tailscale-setup-latest.exe'
    $exeLog = Join-Path $env:TEMP 'tailscale-exe-install.log'
    Write-Log 'Downloading the official Tailscale Windows installer (.exe)...'
    try {
        Invoke-WebRequest -Uri 'https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe' `
            -OutFile $installer -UseBasicParsing

        $signature = Get-AuthenticodeSignature -FilePath $installer
        if ($signature.Status -ne 'Valid' -or
            -not $signature.SignerCertificate -or
            $signature.SignerCertificate.Subject -notmatch '(^|,\s*)CN=Tailscale Inc\.(?:,|$)') {
            throw "Tailscale installer signature validation failed (status=$($signature.Status), signer=$($signature.SignerCertificate.Subject))"
        }

        $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList @(
            '/quiet', '/norestart',
            '/log', ('"' + $exeLog + '"')
        )
        # 3010 is success with a pending reboot; Tailscale is usable without it.
        if ($process.ExitCode -notin @(0, 3010)) {
            throw "Tailscale EXE installer exited with code $($process.ExitCode) (log: $exeLog)"
        }
    } catch {
        Update-ProcessPath
        if (Get-TailscaleExe) {
            Write-Warn "The Tailscale EXE installer returned an error, but tailscale.exe is installed; continuing. Installer detail: $($_.Exception.Message)"
            return
        }
        Write-Err "Tailscale EXE installer download/run failed: $($_.Exception.Message)"
        throw
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

function Install-TailscaleOfficialFallback {
    try {
        Install-TailscaleStandalone
        return
    } catch {
        $msiFailure = $_.Exception.Message
        Write-Warn "The official MSI fallback failed; trying the official EXE installer. MSI detail: $msiFailure"
    }

    try {
        Install-TailscaleBootstrapper
    } catch {
        throw "Both official Tailscale installers failed. MSI detail: $msiFailure; EXE detail: $($_.Exception.Message)"
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
            $global:LASTEXITCODE = 0
            Update-ProcessPath
            if (Get-TailscaleExe) {
                Write-Warn "winget returned exit=$wingetExit, but Tailscale is installed; continuing."
            } else {
                $unsignedExit = [BitConverter]::ToUInt32([BitConverter]::GetBytes([int32]$wingetExit), 0)
                $wingetHex = '0x{0:X8}' -f $unsignedExit
                Write-Warn "winget install failed (exit=$wingetExit, $wingetHex); falling back to the official installers."
                Install-TailscaleOfficialFallback
            }
        }
    } else {
        Write-Log 'winget unavailable; using the official installers.'
        Install-TailscaleOfficialFallback
    }
    $global:LASTEXITCODE = 0
    Update-ProcessPath
    if (-not (Get-TailscaleExe)) {
        throw 'Tailscale installer completed, but tailscale.exe was not found'
    }
}

function Wait-ServiceRegistered {
    param([string]$Name, [int]$Attempts = 30, [int]$DelaySeconds = 2)

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if ($svc) { return $svc }
        if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    throw "Windows service '$Name' was not registered in time"
}

function Wait-ServiceState {
    param(
        [string]$Name,
        [string]$DesiredState,
        [int]$Attempts = 15,
        [int]$DelaySeconds = 1
    )

    $state = 'Unknown'
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $service = Get-Service -Name $Name -ErrorAction SilentlyContinue
        $state = if ($service) { [string]$service.Status } else { 'Missing' }
        if ($state -eq $DesiredState) { return $state }
        if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    return $state
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

function Get-SshdServiceExecutable {
    try {
        $service = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop
        if (-not $service.PathName) { return $null }

        $commandLine = [Environment]::ExpandEnvironmentVariables([string]$service.PathName)
        $candidate = $null
        if ($commandLine -match '^\s*"([^"]+\.exe)"') {
            $candidate = $Matches[1]
        } elseif ($commandLine -match '^\s*(.+?\.exe)(?:\s|$)') {
            $candidate = $Matches[1].Trim()
        }
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    } catch {}
    return $null
}

function Get-SshdFailureDetails {
    $details = New-Object System.Collections.Generic.List[string]
    try {
        $service = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop
        [void]$details.Add("service state=$($service.State), exit=$($service.ExitCode), service-exit=$($service.ServiceSpecificExitCode), path=$($service.PathName)")
    } catch {
        [void]$details.Add("service query failed: $($_.Exception.Message)")
    }
    try {
        $sshdPids = @(Get-Process -Name sshd -ErrorAction SilentlyContinue | ForEach-Object Id)
        $ports = if ($sshdPids.Count) {
            @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { $_.OwningProcess -in $sshdPids } |
                ForEach-Object LocalPort | Sort-Object -Unique)
        } else { @() }
        [void]$details.Add("sshd listener ports=$(if ($ports.Count) { $ports -join ',' } else { 'none' })")
    } catch {
        [void]$details.Add("sshd listener query failed: $($_.Exception.Message)")
    }

    $event = $null
    try {
        $event = Get-WinEvent -FilterHashtable @{
            LogName = 'OpenSSH/Operational'
            StartTime = (Get-Date).AddMinutes(-30)
        } -MaxEvents 30 -ErrorAction Stop | Where-Object { $_.Message } | Select-Object -First 1
    } catch {}
    if (-not $event) {
        try {
            $event = Get-WinEvent -FilterHashtable @{
                LogName = 'System'
                StartTime = (Get-Date).AddMinutes(-30)
            } -MaxEvents 200 -ErrorAction Stop |
                Where-Object { $_.Message -match 'sshd|OpenSSH' } |
                Select-Object -First 1
        } catch {}
    }
    if ($event) {
        $message = (([string]$event.Message -replace '[\r\n]+', ' ') -replace '\s{2,}', ' ').Trim()
        if ($message.Length -gt 700) { $message = $message.Substring(0, 700) + '...' }
        [void]$details.Add("event $($event.Id): $message")
    } else {
        [void]$details.Add('no recent OpenSSH event was found')
    }
    return ($details -join '; ')
}

function Assert-SshdHealthy {
    param([int]$Port, [int]$Attempts = 15, [int]$DelaySeconds = 1)

    $state = 'unknown'
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $state = Get-TcpPortListenerState -Port $Port
        if ($state -eq 'Sshd') { break }

        $service = Get-Service -Name sshd -ErrorAction SilentlyContinue
        if ($attempt -gt 1 -and $service -and $service.Status -eq 'Stopped') { break }
        if ($attempt -lt $Attempts -and $DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }
    if ($state -ne 'Sshd') {
        $details = Get-SshdFailureDetails
        throw "sshd is not listening on TCP port $Port (last observed state: $state; $details)"
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

function Initialize-SshTargetUsers {
    # The built-in Administrator account is identified by RID 500.  Its name
    # is localized and can also be renamed, so never assume "Administrator".
    $account = Get-CimInstance Win32_UserAccount -Filter "SID LIKE '%-500'" -ErrorAction Stop |
        Where-Object { $_.LocalAccount -and [string]$_.SID -match '-500$' } |
        Select-Object -First 1
    if (-not $account) { throw 'local built-in Administrator account (RID 500) not found' }

    $script:SshTargetUserName = [string]$account.Name
    $script:SshTargetSid = [string]$account.SID
    if ([string]::IsNullOrWhiteSpace($script:SshTargetUserName)) {
        throw 'local built-in Administrator account (RID 500) has no usable name'
    }
    if ($script:SshTargetUserName -match '[\s"@,*?!]') {
        throw "built-in Administrator account name cannot be represented safely in sshd_config: $script:SshTargetUserName"
    }

    $administratorEnabled = -not [bool]$account.Disabled
    if (-not $administratorEnabled) {
        $enableFailure = $null
        try {
            & net.exe user $script:SshTargetUserName /active:yes | Out-Null
            $enableExitCode = $LASTEXITCODE
            if ($enableExitCode -ne 0) {
                $enableFailure = "net.exe exit=$enableExitCode"
            }

            $safeTargetSid = $script:SshTargetSid.Replace("'", "''")
            $refreshedAccount = Get-CimInstance Win32_UserAccount -Filter "SID='$safeTargetSid'" -ErrorAction Stop |
                Where-Object { $_.LocalAccount -and [string]$_.SID -eq $script:SshTargetSid } |
                Select-Object -First 1
            if ($refreshedAccount -and -not $refreshedAccount.Disabled) {
                $account = $refreshedAccount
                $administratorEnabled = $true
            } elseif (-not $enableFailure) {
                $enableFailure = 'account remains disabled after net.exe returned success'
            }
        } catch {
            $enableFailure = $_.Exception.Message
        } finally {
            # This native failure is an accepted per-account downgrade.  Do
            # not let Invoke-RequiredStep treat it as a failure of the step.
            $global:LASTEXITCODE = 0
        }

        if (-not $administratorEnabled) {
            Write-Warn "Could not enable built-in Administrator '$script:SshTargetUserName' ($enableFailure); continuing with the current user only."
        }
    }

    $script:SshTargetUserNames = @()
    if ($administratorEnabled) {
        $script:SshTargetUserNames += $script:SshTargetUserName
    }

    if ([string]::IsNullOrWhiteSpace($SshCurrentUserName)) {
        if ($script:SshTargetUserNames.Count -eq 0) {
            throw 'no enabled SSH target account is available'
        }
        return
    }
    if ($SshCurrentUserName -ieq $script:SshTargetUserName) {
        if (-not $administratorEnabled) {
            throw "current login account is disabled: $SshCurrentUserName"
        }
        return
    }
    if ($SshCurrentUserName -match '[\s"@,*?!]') { throw 'current login username cannot be represented safely in sshd_config' }

    $safeCurrentUserName = $SshCurrentUserName.Replace("'", "''")
    $currentAccount = Get-CimInstance Win32_UserAccount -Filter "Name='$safeCurrentUserName'" -ErrorAction Stop |
        Where-Object { $_.LocalAccount } |
        Select-Object -First 1
    if (-not $currentAccount) { throw "current local login account not found: $SshCurrentUserName" }
    if ($currentAccount.Disabled) { throw "current local login account is disabled: $SshCurrentUserName" }
    $script:SshTargetUserNames += [string]$currentAccount.Name
    $script:SshTargetUserNames = @($script:SshTargetUserNames | Select-Object -Unique)
}
function Set-TargetUsersAuthorizedKeysMatch {
    param([string]$File, [string[]]$UserNames)

    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "sshd_config not found: $File" }
    $normalizedUsers = @(
        foreach ($userName in $UserNames) {
            if ([string]::IsNullOrWhiteSpace($userName) -or $userName -match '[\s"@,*?!]') {
                throw 'SSH target username cannot be represented safely in sshd_config'
            }
            $userName.Trim().ToLowerInvariant()
        }
    ) | Select-Object -Unique
    if ($normalizedUsers.Count -eq 0) {
        throw 'no SSH target users were provided'
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

    $block = New-Object System.Collections.Generic.List[string]
    $block.Add($begin)
    foreach ($userName in $normalizedUsers) {
        $block.Add(('Match User "{0}"' -f $userName))
        $block.Add('    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys')
        $block.Add('    KbdInteractiveAuthentication no')
    }
    $block.Add($end)
    $keptLines = @($kept)
    $matchIndex = -1
    for ($i = 0; $i -lt $keptLines.Count; $i++) {
        if ($keptLines[$i] -match '^\s*Match(?:\s|$)') { $matchIndex = $i; break }
    }
    if ($matchIndex -lt 0) {
        $result = $keptLines + @($block)
    } else {
        $before = if ($matchIndex -gt 0) { @($keptLines[0..($matchIndex - 1)]) } else { @() }
        $after = @($keptLines[$matchIndex..($keptLines.Count - 1)])
        $result = $before + @($block) + $after
    }
    Write-SshdConfigLines -File $File -Lines $result
}

function Get-SshConfigUserNames {
    $sourceNames = if ($script:SshTargetUserNames.Count) {
        @($script:SshTargetUserNames)
    } else {
        @($SshTargetUserName, $SshCurrentUserName)
    }
    return @(
        foreach ($userName in $sourceNames) {
            if ([string]::IsNullOrWhiteSpace($userName)) { continue }
            if ($userName -match '[\s"@,*?!]') { throw 'SSH target username cannot be represented safely in sshd_config' }
            $userName.Trim().ToLowerInvariant()
        }
    ) | Select-Object -Unique
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
    param([string]$ConfigPath = (Join-Path $env:ProgramData 'ssh\sshd_config'))

    $sshd = Get-SshdExe
    if (-not $sshd) { throw 'sshd.exe not found; cannot validate sshd_config' }

    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = @(& $sshd -t -f $ConfigPath 2>&1)
        $exitCode = $global:LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    if ($exitCode -ne 0) {
        $detail = (($output | ForEach-Object { "$_" }) -join ' ') -replace '\s{2,}', ' '
        if ($detail.Length -gt 700) { $detail = $detail.Substring(0, 700) + '...' }
        if (-not $detail) { $detail = 'no diagnostic output' }
        throw "sshd config validation failed using '$sshd' (exit=$exitCode): $detail"
    }
}

function Invoke-OpenSshOperation {
    param(
        [string]$Description,
        [scriptblock]$Action
    )

    try {
        & $Action
    } catch {
        throw "$Description`: $($_.Exception.Message)"
    }
}

function Test-OpenSshServerInstalled {
    $sshd = Get-SshdExe
    $service = Get-Service -Name sshd -ErrorAction SilentlyContinue
    return [bool]($sshd -and $service)
}

function Install-OpenSshServerCapability {
    $capabilityName = 'OpenSSH.Server~~~~0.0.1.0'

    if (Test-OpenSshServerInstalled) {
        Write-Log 'OpenSSH Server already installed, skipping'
        return
    }

    $capability = $null
    try {
        $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction Stop |
            Select-Object -First 1
    } catch {
        Write-Warn "Windows capability query failed; trying installation directly: $($_.Exception.Message)"
    }

    if ($capability -and $capability.State -eq 'Installed') {

        throw 'Windows reports OpenSSH Server as installed, but sshd.exe or the sshd service is missing'
    }

    Write-Log 'Installing OpenSSH Server...'
    $powershellFailure = $null
    try {
        Add-WindowsCapability -Online -Name $capabilityName -ErrorAction Stop | Out-Null
    } catch {
        $powershellFailure = $_.Exception.Message
        Write-Warn "Add-WindowsCapability failed; retrying with DISM: $powershellFailure"

        $dism = Get-Command dism.exe -ErrorAction SilentlyContinue
        if (-not $dism) {
            throw "Add-WindowsCapability failed ($powershellFailure), and dism.exe was not found"
        }

        $global:LASTEXITCODE = 0
        $dismOutput = @(& $dism.Source /Online /Add-Capability "/CapabilityName:$capabilityName" /NoRestart 2>&1)
        $dismExit = $global:LASTEXITCODE
        if ($dismExit -ne 0) {
            $detail = ($dismOutput | Select-Object -Last 8) -join ' '
            throw "Add-WindowsCapability failed ($powershellFailure); DISM failed (exit=$dismExit): $detail"
        }
    }

    $global:LASTEXITCODE = 0
    Update-ProcessPath
    $null = Wait-ServiceRegistered -Name sshd
    if (-not (Get-SshdExe)) {
        throw 'OpenSSH Server installation completed, but sshd.exe was not found'
    }
}

function Enable-OpenSSHServer {
    Invoke-OpenSshOperation 'OpenSSH capability detection/installation' {
        Install-OpenSshServerCapability
    }

    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    $defaultCfg = Join-Path $env:WINDIR 'System32\OpenSSH\sshd_config_default'
    Invoke-OpenSshOperation 'Initialize and write sshd_config' {
        Initialize-SshdConfig -ConfigPath $cfg -TemplatePath $defaultCfg
        Select-SshPort
        Set-ManagedSshdConfig -File $cfg -Port $SshPort -UserNames (Get-SshConfigUserNames)
    }

    Invoke-OpenSshOperation 'Generate host keys and validate sshd_config' {
        Initialize-SshHostKeys
        Assert-SshdConfigValid
    }

    Invoke-OpenSshOperation 'Configure and start the sshd service' {
        $null = Wait-ServiceRegistered -Name sshd
        Set-Service -Name sshd -StartupType Automatic -ErrorAction Stop

        try {
            Start-OrRestartSshdService
            Assert-SshdHealthy -Port $SshPort
        } catch {
            $initialFailure = $_.Exception.Message
            $initialDetails = Get-SshdFailureDetails
            if ($initialDetails) {
                $initialFailure = "$initialFailure; diagnostics: $initialDetails"
            }
            Write-Warn "Initial sshd start failed; rebuilding sshd_config from the Windows default template: $initialFailure"
            try {
                $service = Get-Service -Name sshd -ErrorAction SilentlyContinue
                if ($service -and $service.Status -ne 'Stopped') {
                    Stop-Service -Name sshd -Force -ErrorAction SilentlyContinue
                }
                $stoppedState = Wait-ServiceState -Name sshd -DesiredState 'Stopped'
                if ($stoppedState -ne 'Stopped') {
                    throw "sshd service did not reach Stopped before retry (state=$stoppedState)"
                }
                $listenerState = Wait-TcpPortListenerState -Port $SshPort -DesiredStates @('Free', 'Other')
                if ($listenerState -eq 'Sshd') {
                    throw "an sshd process is still listening on TCP port $SshPort before retry"
                }

                $backup = Repair-SshdConfigFromDefault -ConfigPath $cfg -TemplatePath $defaultCfg `
                    -Port $SshPort -UserNames (Get-SshConfigUserNames)
                Write-Warn "Previous sshd_config was preserved at $backup"
                Initialize-SshHostKeys
                Assert-SshdConfigValid -ConfigPath $cfg
                Start-OrRestartSshdService
                Assert-SshdHealthy -Port $SshPort
            } catch {
                $repairFailure = $_.Exception.Message
                $repairDetails = Get-SshdFailureDetails
                if ($repairDetails) {
                    $repairFailure = "$repairFailure; diagnostics: $repairDetails"
                }
                throw "automatic clean-config retry failed (initial: $initialFailure; retry: $repairFailure)"
            }
        }
    }

    try {
        if (Get-Service -Name ssh-agent -ErrorAction SilentlyContinue) {
            Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction Stop
        }
    } catch {
        Write-Warn "Optional ssh-agent configuration failed: $($_.Exception.Message)"
    }

    Invoke-OpenSshOperation 'Create the OpenSSH firewall rule' {
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
}

function Set-SshdOption {
    param([string]$File, [string]$Key, [string]$Value)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "sshd_config not found, refusing to skip option '$Key': $File"
    }
    $lines = @([IO.File]::ReadAllLines($File))
    $pattern = "^\s*$([regex]::Escape($Key))\s+"
    $kept = @($lines | Where-Object { $_ -notmatch $pattern })

    Write-SshdConfigLines -File $File -Lines (@("$Key $Value") + $kept)
}

function Remove-SshdOption {
    param([string]$File, [string]$Key)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "sshd_config not found, refusing to remove option '$Key': $File"
    }
    $pattern = "^\s*$([regex]::Escape($Key))(?:\s|$)"
    $lines = @([IO.File]::ReadAllLines($File))
    $kept = @($lines | Where-Object { $_ -notmatch $pattern })
    if ($kept.Count -ne $lines.Count) {
        Write-SshdConfigLines -File $File -Lines $kept
    }
}

function Set-ManagedSshdConfig {
    param([string]$File, [int]$Port, [string[]]$UserNames)

    Remove-SshdOption -File $File -Key 'ListenAddress'
    Set-SshdOption -File $File -Key 'AddressFamily' -Value 'inet'
    Set-SshdOption -File $File -Key 'Port' -Value $Port
    Set-SshdOption -File $File -Key 'PasswordAuthentication' -Value 'no'

    Remove-SshdOption -File $File -Key 'ChallengeResponseAuthentication'
    Set-SshdOption -File $File -Key 'KbdInteractiveAuthentication' -Value 'no'
    Set-SshdOption -File $File -Key 'ChallengeResponseAuthentication' -Value 'no'
    Set-SshdOption -File $File -Key 'PubkeyAuthentication' -Value 'yes'
    Remove-SshdOption -File $File -Key 'DenyUsers'
    Remove-SshdOption -File $File -Key 'DenyGroups'
    Remove-SshdOption -File $File -Key 'AllowGroups'
    Set-SshdOption -File $File -Key 'AllowUsers' -Value ($UserNames -join ' ')
    Set-TargetUsersAuthorizedKeysMatch -File $File -UserNames $UserNames
}

function Repair-SshdConfigFromDefault {
    param(
        [string]$ConfigPath,
        [string]$TemplatePath,
        [int]$Port,
        [string[]]$UserNames
    )

    if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) {
        throw "OpenSSH default configuration template not found: $TemplatePath"
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$ConfigPath.before-auto-repair-$stamp.bak"
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        Copy-Item -LiteralPath $ConfigPath -Destination $backup -ErrorAction Stop
    }
    Copy-Item -LiteralPath $TemplatePath -Destination $ConfigPath -Force -ErrorAction Stop
    Set-ManagedSshdConfig -File $ConfigPath -Port $Port -UserNames $UserNames
    return $backup
}

function Start-OrRestartSshdService {
    $service = Get-Service -Name sshd -ErrorAction Stop
    if ($service.Status -eq 'Running') {
        Restart-Service -Name sshd -ErrorAction Stop
    } else {
        Start-Service -Name sshd -ErrorAction Stop
    }
}

function Enable-AuthorizedKeysAclRepairAccess {
    param(
        [string]$Path,
        [switch]$Container
    )

    # A previous run may have removed the launching administrator from the
    # DACL before it could set the final owner.  Temporarily make the built-in
    # Administrators group the owner so an elevated administrator or SYSTEM
    # can repair and rerun the setup.
    & takeown.exe /F $Path /A | Out-Null
    Assert-NativeCommandSucceeded "take ownership for ACL repair: $Path"

    $rights = if ($Container) { '(OI)(CI)(F)' } else { '(F)' }
    & icacls.exe $Path /grant "*S-1-5-32-544:$rights" | Out-Null
    Assert-NativeCommandSucceeded "grant Administrators ACL repair access: $Path"
}

function Set-RestrictedAdministratorsAuthorizedKeysAcl {
    param([string]$Path)

    & icacls.exe $Path /reset | Out-Null
    Assert-NativeCommandSucceeded 'reset administrators-authorized-keys ACL'
    & icacls.exe $Path /grant:r '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
    Assert-NativeCommandSucceeded 'grant administrators-authorized-keys ACL'
    & icacls.exe $Path '/inheritance:r' | Out-Null
    Assert-NativeCommandSucceeded 'disable administrators-authorized-keys ACL inheritance'
    & icacls.exe $Path /setowner '*S-1-5-32-544' | Out-Null
    Assert-NativeCommandSucceeded 'set administrators-authorized-keys owner'
}

function Set-AuthorizedKeys {
    if (-not $SshPublicKeys -or $SshPublicKeys.Count -eq 0) {
        throw '$SshPublicKeys is empty; refusing to disable password authentication without a public key'
    }

    $authFile = $SshTargetAuthorizedKeysFile
    $authDir = Split-Path -Parent $authFile
    if (-not (Test-Path -LiteralPath $authDir -PathType Container)) {
        New-Item -ItemType Directory -Path $authDir -Force | Out-Null
    }

    if (-not (Test-Path $authFile)) { New-Item -ItemType File -Path $authFile -Force | Out-Null }
    Enable-AuthorizedKeysAclRepairAccess -Path $authFile

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

    # Finalize the file first.  The directory keeps the temporary repair ACE
    # until the child no longer needs to be opened by this process.
    Set-RestrictedAdministratorsAuthorizedKeysAcl -Path $authFile
    Assert-AuthorizedKeysReady -File $authFile

    Write-Log "authorized_keys configured ($authFile), $added key(s) added this run."
}

function Assert-RestrictedAdministratorsAuthorizedKeysAcl {
    param([string]$Path)

    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $acl.AreAccessRulesProtected) { throw 'administrators_authorized_keys ACL inheritance is enabled' }
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($ownerSid -ne 'S-1-5-32-544') { throw 'administrators_authorized_keys owner is not Administrators' }

    $allowed = @('S-1-5-18', 'S-1-5-32-544')
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 2) { throw 'administrators_authorized_keys must have exactly two ACL entries' }
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if ($sid -notin $allowed -or $rule.IsInherited) { throw 'administrators_authorized_keys has an unexpected ACL entry' }
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { throw 'administrators_authorized_keys has a non-Allow ACL entry' }
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) { throw 'administrators_authorized_keys ACL is not FullControl' }
    }
    foreach ($sid in $allowed) {
        if (-not ($rules | Where-Object { $_.IdentityReference.Value -eq $sid })) { throw 'administrators_authorized_keys is missing a required ACL principal' }
    }
}

function Assert-AuthorizedKeysReady {
    param([string]$File)

    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "authorized_keys file not found: $File" }
    $existing = @(Get-Content -LiteralPath $File -ErrorAction Stop)
    foreach ($key in $SshPublicKeys) {
        if ($existing -notcontains $key.Trim()) { throw "configured public key is missing: $(($key -split '\s+')[-1])" }
    }

    Assert-RestrictedAdministratorsAuthorizedKeysAcl -Path $File
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
    # Validate with the binary that Start-Service will actually launch. PATH can
    # contain a second OpenSSH installation with different defaults or features.
    $serviceExe = Get-SshdServiceExecutable
    if ($serviceExe) { return $serviceExe }

    $candidate = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }

    $cmd = Get-Command sshd.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command sshd -ErrorAction SilentlyContinue }
    if ($cmd) { return $(if ($cmd.Path) { $cmd.Path } else { $cmd.Source }) }
    return $null
}

function Get-SshdEffectiveValue {
    param([string[]]$Config, [string]$Key)
    foreach ($line in $Config) {
        if ($line -match ("^" + [regex]::Escape($Key) + "\s+(.+)$")) { return $Matches[1].Trim() }
    }
    return $null
}

function Test-AdministratorAuthorizedKeysValue {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    $candidate = $Value.Trim()
    if ($candidate -match '^"([^"]+)"$') {
        $candidate = $Matches[1]
    } elseif ($candidate.Contains('"')) {
        return $false
    }
    $actual = $candidate.Replace('\', '/').TrimEnd('/').ToLowerInvariant()
    $expected = @('__programdata__/ssh/administrators_authorized_keys')
    if ($SshTargetAuthorizedKeysFile) {
        $expected += $SshTargetAuthorizedKeysFile.Replace('\', '/').ToLowerInvariant()
    }
    return $actual -in $expected
}

function Assert-SshdEffectiveConfig {
    param([int]$Port, [string]$TailscaleIp)

    $sshd = Get-SshdExe
    if (-not $sshd) { throw 'sshd.exe not found' }
    $config = Join-Path $env:ProgramData 'ssh\sshd_config'
    & $sshd -t -f $config 2>$null
    Assert-NativeCommandSucceeded 'sshd config validation'

    foreach ($userName in Get-SshConfigUserNames) {
        $context = "user=$userName,host=$env:COMPUTERNAME,addr=100.64.0.1,laddr=$TailscaleIp,lport=$Port"
        $effective = @(& $sshd -T -f $config -C $context 2>$null)
        Assert-NativeCommandSucceeded "sshd effective config validation for $userName"

        if ((Get-SshdEffectiveValue $effective 'port') -ne [string]$Port) {
            throw "effective sshd port is not $Port for $userName"
        }
        if ((Get-SshdEffectiveValue $effective 'passwordauthentication') -ne 'no') {
            throw "effective PasswordAuthentication is not no for $userName"
        }
        $kbdInteractive = Get-SshdEffectiveValue $effective 'kbdinteractiveauthentication'
        if ($kbdInteractive -ne 'no') {
            throw "effective KbdInteractiveAuthentication is not no for $userName (actual: $kbdInteractive)"
        }
        if ((Get-SshdEffectiveValue $effective 'pubkeyauthentication') -ne 'yes') {
            throw "effective PubkeyAuthentication is not yes for $userName"
        }
        $keyFile = Get-SshdEffectiveValue $effective 'authorizedkeysfile'
        if (-not (Test-AdministratorAuthorizedKeysValue $keyFile)) {
            throw "effective AuthorizedKeysFile is incorrect for $($userName): $keyFile"
        }
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
    $authFile = $SshTargetAuthorizedKeysFile
    Assert-AuthorizedKeysReady -File $authFile
    Assert-SshdHealthy -Port $Port
    Assert-SshFirewallReady -Port $Port
    Assert-SshBanner -Address $TailscaleIp -Port $Port
    $finalTailscaleIp = Assert-TailscaleReady -Attempts 3 -DelaySeconds 1
    if ($finalTailscaleIp -ne $TailscaleIp) {
        throw "Tailscale IPv4 address changed during setup ($TailscaleIp -> $finalTailscaleIp)"
    }
}

function Get-SshLoginText {
    param([int]$Port, [string]$TailscaleIp)
    return @(
        Get-SshConfigUserNames | ForEach-Object {
            "ssh -p $Port $_@$TailscaleIp"
        }
    ) -join "`n"
}

function Invoke-Setup {
    $FailedSteps.Clear()
    $script:ValidatedTailscaleIp = $null
    $tgConfig = if ($TgBotToken -and $TgChatId) {
        @{ Token = $TgBotToken; ChatId = $TgChatId }
    } else { $null }

    Write-Log 'Starting configuration (platform: Windows OpenSSH over Tailscale)'
    try {
        Invoke-RequiredStep 'Enable and validate SSH target accounts' { Initialize-SshTargetUsers }
        Invoke-RequiredStep 'Install Tailscale' { Install-Tailscale }
        Invoke-RequiredStep 'Tailscale service autostart' { Enable-TailscaleService }
        Invoke-RequiredStep 'Tailscale login' { Connect-Tailscale -AuthKey $TsAuthKey }
        Invoke-RequiredStep 'Validate Tailscale readiness' {
            $script:ValidatedTailscaleIp = Assert-TailscaleReady
        }
        Invoke-RequiredStep 'Enable OpenSSH Server' { Enable-OpenSSHServer }
        Invoke-RequiredStep 'Configure target-user public keys' { Set-AuthorizedKeys }
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
                Assert-SshServerReady -Port $SshPort -TailscaleIp $script:ValidatedTailscaleIp
                $loginLine = Get-SshLoginText -Port $SshPort -TailscaleIp $script:ValidatedTailscaleIp
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
Users: $((Get-SshConfigUserNames) -join ', ')
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
    $loginLine = Get-SshLoginText -Port $SshPort -TailscaleIp $script:ValidatedTailscaleIp
    Write-Host ''
    Write-Host '==================== Summary ====================' -ForegroundColor Green
    Write-Log "This machine's Tailscale IP: $script:ValidatedTailscaleIp"
    Write-Log "Server-side SSH readiness checks passed: $loginLine"
    Write-Warn 'This is Windows OpenSSH over Tailscale; no Tailscale SSH badge is expected.'

    $successMessage = @"
[READY] Windows OpenSSH server-side checks passed
Host: $env:COMPUTERNAME (Windows)
Users: $((Get-SshConfigUserNames) -join ', ')
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
exit $setupExitCode
