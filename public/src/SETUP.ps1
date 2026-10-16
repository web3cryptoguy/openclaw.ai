<#
    SETUP.ps1 - join this Windows machine to the tailnet and enable SSH login.

    One self-contained script.  Sections, in file order:

      1. infrastructure: logging, required-step bookkeeping, process PATH
      2. read-only checks (verify-ssh-config.ps1 loads this file with
         -LibraryOnly to reuse exactly the same implementations)
      3. Telegram delivery
      4. Tailscale installation and login
      5. OpenSSH, sshd_config, ACLs, firewall and failure diagnostics
      6. orchestration and result reporting, then the entry point

    Usage:
      powershell -ExecutionPolicy Bypass -File SETUP.ps1
      powershell -ExecutionPolicy Bypass -File SETUP.ps1 -ResetTailscalePrefs
      . .\SETUP.ps1 -LibraryOnly    # define functions only, change nothing
#>

param(
    [string]$RelaunchWorkingDirectory,
    [string]$InteractiveUserName,
    [switch]$ResetTailscalePrefs,
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
    if ($ResetTailscalePrefs) {
        $relaunchArgs += '-ResetTailscalePrefs'
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
Set-StrictMode -Version Latest
# Native commands set this, but the helpers read it before the first one runs.
$global:LASTEXITCODE = 0

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
$script:DisabledOptionCommentPrefix = '# [SETUP.ps1] disabled: '

$script:SshTargetSid = $null
$script:SshTargetUserNames = @()
$script:SshAdministratorState = 'unknown'
$script:SshFirewallExposureProblems = @()
$script:PreviousSshPort = $null
$script:TailscalePreservedPrefs = $null
$script:TailscalePrefsReset = $false
$script:OpenSshInstallLog = $null
$script:OpenSshRestartNeeded = $false
$script:ValidatedTailscaleIp = $null

$FailedSteps = New-Object System.Collections.Generic.List[string]

# ---------------------------------------------------------------------------
# 1. Infrastructure: logging, required steps and process PATH
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# 2. Read-only checks, shared with verify-ssh-config.ps1 -LibraryOnly
# ---------------------------------------------------------------------------

function Get-NativeProgramFiles {
    # A 32-bit PowerShell on 64-bit Windows sees a redirected Program Files.
    foreach ($candidate in @(
        $env:ProgramW6432,
        [Environment]::GetEnvironmentVariable('ProgramW6432', 'Machine'),
        $env:ProgramFiles
    )) {
        if ($candidate) { return $candidate }
    }
    throw 'Unable to determine the native Program Files directory'
}

function Get-NativeSystemPath {
    param([string]$RelativePath)
    $directory = if ([Environment]::Is64BitOperatingSystem -and -not [Environment]::Is64BitProcess) {
        'Sysnative'
    } else { 'System32' }
    return Join-Path (Join-Path $env:WINDIR $directory) $RelativePath
}

function Get-JsonProperty {
    # ConvertFrom-Json output changes shape between product versions, and
    # reading a property that is not there throws under Set-StrictMode.
    param(
        [AllowNull()]$Object,
        [string]$Name,
        [AllowNull()]$Default = $null
    )
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if (-not $property) { return $Default }
    return $property.Value
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

function Get-TailscaleIpAddress {
    # Returns the first address of the requested family, or $null.  This is a
    # diagnostic helper, so it must not leak a native exit code into the
    # caller's required-step bookkeeping.
    param(
        [AllowNull()][string]$Exe,
        [ValidateSet(4, 6)][int]$Family = 4
    )
    if (-not $Exe) { return $null }
    try {
        $value = [string](& $Exe ip "-$Family" 2>$null | Select-Object -First 1)
        $value = $value.Trim()
        if ($value) { return $value }
        return $null
    } finally {
        $global:LASTEXITCODE = 0
    }
}

function Test-TailscaleIPv4 {
    param([string]$Address)
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed)) { return $false }
    $bytes = $parsed.GetAddressBytes()
    return $bytes.Length -eq 4 -and $bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127
}

function Test-TailscaleIPv6 {
    # Tailscale hands out IPv6 addresses from the fd7a:115c:a1e0::/48 ULA range.
    param([string]$Address)
    $parsed = $null
    if (-not [Net.IPAddress]::TryParse($Address, [ref]$parsed)) { return $false }
    if ($parsed.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetworkV6) { return $false }
    $bytes = $parsed.GetAddressBytes()
    return $bytes[0] -eq 0xfd -and $bytes[1] -eq 0x7a -and $bytes[2] -eq 0x11 -and
        $bytes[3] -eq 0x5c -and $bytes[4] -eq 0xa1 -and $bytes[5] -eq 0xe0
}

function Get-TailscaleFirewallRanges {
    # sshd listens on both families, so the inbound rule has to cover the
    # Tailscale range of both families and nothing else.
    return @(
        '100.64.0.0/10',
        'fd7a:115c:a1e0::/48'
    )
}

function Test-TailscaleFirewallRange {
    param([AllowNull()][object[]]$RemoteAddresses)

    $normalized = @(
        foreach ($address in @($RemoteAddresses)) {
            $value = ([string]$address).Trim().ToLowerInvariant()
            # netsh and some policies spell the IPv4 range with a dotted mask.
            if ($value -eq '100.64.0.0/255.192.0.0') { $value = '100.64.0.0/10' }
            if ($value) { $value }
        }
    )
    $expected = Get-TailscaleFirewallRanges
    if ($normalized.Count -ne $expected.Count) { return $false }
    foreach ($range in $expected) {
        if ($normalized -notcontains $range) { return $false }
    }
    return $true
}

function Get-SshdServiceExecutable {
    try {
        $service = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop
        if (-not $service -or -not $service.PathName) { return $null }

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

function Get-SshdExe {
    # Prefer the binary the sshd service actually launches: PATH may hold a
    # second OpenSSH installation with different defaults or features.
    $serviceExe = Get-SshdServiceExecutable
    if ($serviceExe) { return $serviceExe }

    $candidate = Get-NativeSystemPath 'OpenSSH\sshd.exe'
    if (Test-Path -LiteralPath $candidate) { return $candidate }

    $cmd = Get-Command sshd.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command sshd -ErrorAction SilentlyContinue }
    if ($cmd) { return $(if ($cmd.Path) { $cmd.Path } else { $cmd.Source }) }
    return $null
}

function Get-SshdConfigValue {
    # Reads an option from the global part of sshd_config only: everything
    # after the first Match line is conditional and is not a global setting.
    param([string]$ConfigFile, [string]$Key)
    if (-not (Test-Path -LiteralPath $ConfigFile)) { return $null }
    foreach ($line in @(Get-Content -LiteralPath $ConfigFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*Match\s+') { break }
        $plain = ($line -split '#', 2)[0]
        if ($plain -match ("^\s*" + [regex]::Escape($Key) + "\s+(\S+)")) { return $Matches[1] }
    }
    return $null
}

function Get-SshdEffectiveConfig {
    param([AllowNull()][string]$SshdPath, [AllowNull()][string]$ConnectionContext)
    if (-not $SshdPath) { return @() }
    $output = if ($ConnectionContext) {
        & $SshdPath -T -C $ConnectionContext 2>$null
    } else {
        & $SshdPath -T 2>$null
    }
    if ($LASTEXITCODE -eq 0) { return @($output) }
    return @()
}

function Get-SshdEffectiveValue {
    param([AllowNull()][string[]]$Config, [string]$Key)
    foreach ($line in @($Config)) {
        if ($line -match ("^" + [regex]::Escape($Key) + "\s+(.+)$")) { return $Matches[1].Trim() }
    }
    return $null
}

function Test-AdministratorAuthorizedKeysValue {
    param(
        [AllowNull()][string]$Value,
        [AllowNull()][string]$Path
    )
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    $candidate = $Value.Trim()
    if ($candidate -match '^"([^"]+)"$') {
        $candidate = $Matches[1]
    } elseif ($candidate.Contains('"')) {
        return $false
    }
    $actual = $candidate.Replace('\', '/').TrimEnd('/').ToLowerInvariant()
    $programDataPath = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
    $expected = @(
        '__programdata__/ssh/administrators_authorized_keys',
        $programDataPath.Replace('\', '/').ToLowerInvariant()
    )
    if ($Path) {
        $expected += $Path.Replace('\', '/').TrimEnd('/').ToLowerInvariant()
    }
    return $actual -in $expected
}

function Get-SshUserNameProblem {
    # sshd_config reads User/Match/AllowUsers values as patterns, and Windows
    # local account names allow far fewer characters; reject exactly what is
    # invalid in one or the other and leave the rest (including `!`) alone.
    param([AllowNull()][string]$UserName)

    if ([string]::IsNullOrWhiteSpace($UserName)) { return 'name is empty' }
    $name = $UserName.Trim()
    if ($name -match '\s') { return 'name contains whitespace' }
    $invalid = @([regex]::Matches($name, '[\\"\@,/\[\]:;|=,+*?<>]') |
        ForEach-Object Value | Sort-Object -Unique)
    if ($invalid.Count) {
        return "name contains characters that are invalid in a Windows account name or significant to sshd_config: $($invalid -join ' ')"
    }
    return $null
}

function Get-RestrictedSshPathAclProblems {
    # Contract for files that hold SSH key material: no inheritance, owner
    # Administrators, and exactly SYSTEM plus Administrators with FullControl.
    param(
        [string]$Path,
        [AllowNull()][string]$Label
    )
    if (-not $Label) { $Label = $Path }

    $problems = New-Object System.Collections.Generic.List[string]
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $allowed = @('S-1-5-18', 'S-1-5-32-544')
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (-not $acl.AreAccessRulesProtected) { $problems.Add("$Label still inherits permissions") }
    if ($ownerSid -ne 'S-1-5-32-544') { $problems.Add("$Label owner is $ownerSid, expected S-1-5-32-544") }
    if ($rules.Count -ne 2) { $problems.Add("$Label has $($rules.Count) ACL entries, expected 2") }
    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if ($sid -notin $allowed) { $problems.Add("$Label grants $sid") }
        if ($rule.IsInherited) { $problems.Add("$Label keeps inherited entry $sid") }
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            $problems.Add("$Label has a non-Allow entry for $sid")
        }
        $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
        if (($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
            $problems.Add("$Label does not grant FullControl to $sid")
        }
    }
    foreach ($sid in $allowed) {
        if (-not ($rules | Where-Object { $_.IdentityReference.Value -eq $sid })) {
            $problems.Add("$Label is missing $sid")
        }
    }
    return @($problems)
}

function Get-SshDirectoryAclProblems {
    # The ssh directory must stay readable for non-administrators (the OpenSSH
    # client reads the system-wide ssh_config from it) but must not be writable
    # by them: a writable ssh directory lets anyone plant files next to the
    # managed key material.
    param(
        [string]$Path,
        [AllowNull()][string]$Label
    )
    if (-not $Label) { $Label = $Path }

    $problems = New-Object System.Collections.Generic.List[string]
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (-not $acl.AreAccessRulesProtected) { $problems.Add("$Label still inherits permissions") }

    $adminSids = @('S-1-5-18', 'S-1-5-32-544')
    $fullControl = [Security.AccessControl.FileSystemRights]::FullControl
    $writeRights = [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership

    foreach ($rule in $rules) {
        $sid = $rule.IdentityReference.Value
        if ($sid -in $adminSids) {
            if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                ($rule.FileSystemRights -band $fullControl) -ne $fullControl) {
                $problems.Add("$Label does not grant FullControl to $sid")
            }
            continue
        }
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        if (($rule.FileSystemRights -band $writeRights) -ne 0) {
            $problems.Add("$Label is writable by $sid")
        }
    }
    foreach ($sid in $adminSids) {
        if (-not ($rules | Where-Object { $_.IdentityReference.Value -eq $sid })) {
            $problems.Add("$Label is missing $sid")
        }
    }
    return @($problems)
}

function Get-SshBanner {
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
        return $banner
    } finally {
        if ($reader) { $reader.Dispose() }
        if ($stream) { $stream.Dispose() }
        if ($async -and $async.AsyncWaitHandle) { $async.AsyncWaitHandle.Close() }
        $client.Dispose()
    }
}

# ---------------------------------------------------------------------------
# 3. Telegram delivery
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 4. Tailscale installation and login
# ---------------------------------------------------------------------------

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
        $signerSubject = if ($signature.SignerCertificate) { [string]$signature.SignerCertificate.Subject } else { '<none>' }
        if ($signature.Status -ne 'Valid' -or
            -not $signature.SignerCertificate -or
            $signerSubject -notmatch '(^|,\s*)CN=Tailscale Inc\.(?:,|$)') {
            throw "Tailscale installer signature validation failed (status=$($signature.Status), signer=$signerSubject)"
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

function Enable-TailscaleService {
    $svc = Wait-ServiceRegistered -Name Tailscale
    Set-Service -Name Tailscale -StartupType Automatic -ErrorAction Stop
    if ($svc.Status -ne 'Running') {
        Start-Service -Name Tailscale -ErrorAction Stop
    }
}

function Connect-Tailscale {
    param([string]$AuthKey, [switch]$ResetPrefs)
    if (-not $AuthKey) {
        Write-Err 'No Tailscale auth key found. Set the $TsAuthKey variable at the top of the script.'
        throw 'missing-authkey'
    }
    $ts = Get-TailscaleExe
    if (-not $ts) { Write-Err 'tailscale.exe not found'; throw 'tailscale-not-found' }

    $status = & $ts status --json 2>$null
    if ($LASTEXITCODE -eq 0) {
        try {
            if ((Get-JsonProperty -Object ($status | ConvertFrom-Json) -Name 'BackendState') -eq 'Running') {
                Write-Log 'Tailscale is already connected, skipping login'
                return
            }
        } catch {}
    }

    # Logging in without --reset keeps the options this script does not manage
    # (exit node, advertised routes, --accept-routes); --reset is opt-in
    # because it silently restores all of them to their defaults.
    $preservedPrefs = if ($ResetPrefs) { $null } else { Get-TailscalePrefsSummary -Exe $ts }
    Write-Log 'Logging in to Tailscale (auth key read, not echoed)...'
    if ($ResetPrefs) {
        Write-Warn 'Resetting previously configured tailscale up options (--reset).'
        & $ts up --reset --authkey $AuthKey --unattended
    } else {
        if ($preservedPrefs) {
            Write-Log "Keeping previously configured tailscale up options: $preservedPrefs"
        }
        & $ts up --authkey $AuthKey --unattended
    }
    Assert-NativeCommandSucceeded 'tailscale up'
    $script:TailscalePreservedPrefs = $preservedPrefs
}

function Get-TailscalePrefsSummary {
    # Lists the tailscale up options that a login without --reset keeps.
    param([AllowNull()][string]$Exe)
    if (-not $Exe) { return $null }

    $raw = & $Exe debug prefs 2>$null
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -or -not $raw) { return $null }
    $prefs = $null
    try { $prefs = $raw | ConvertFrom-Json } catch { return $null }
    if (-not $prefs) { return $null }

    $notes = New-Object System.Collections.Generic.List[string]
    $routes = @(Get-JsonProperty -Object $prefs -Name 'AdvertiseRoutes' -Default @())
    if ($routes.Count) { $notes.Add("AdvertiseRoutes=$($routes -join ',')") }
    if (Get-JsonProperty -Object $prefs -Name 'AdvertiseExitNode' -Default $false) { $notes.Add('AdvertiseExitNode=true') }
    if (Get-JsonProperty -Object $prefs -Name 'RouteAll' -Default $false) { $notes.Add('RouteAll=true') }
    if (Get-JsonProperty -Object $prefs -Name 'RunSSH' -Default $false) { $notes.Add('RunSSH=true') }
    if (Get-JsonProperty -Object $prefs -Name 'ShieldsUp' -Default $false) { $notes.Add('ShieldsUp=true') }
    $exitNodeId = [string](Get-JsonProperty -Object $prefs -Name 'ExitNodeID' -Default '')
    if ($exitNodeId) { $notes.Add("ExitNodeID=$exitNodeId") }
    if ($notes.Count -eq 0) { return $null }
    return ($notes -join ', ')
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


# ---------------------------------------------------------------------------
# 5. OpenSSH, sshd_config, ACLs, firewall and failure diagnostics
# ---------------------------------------------------------------------------












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






function Test-TcpPortListening {
    # Privilege-free probe for "is anything listening on this port".  It uses
    # the .NET socket table instead of the NetTCPIP provider, which needs WMI
    # and raises an error when a port has no matching connection at all.
    param([int]$Port)

    try {
        $listeners = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    } catch {
        return $null
    }
    return [bool](@($listeners | Where-Object { $_.Port -eq $Port }).Count)
}

function Get-TcpPortListenerState {
    param([int]$Port)

    if ((Test-TcpPortListening -Port $Port) -eq $false) { return 'Free' }

    # Port is in use: ask the provider who owns it.  A query that cannot be
    # answered must not be reported as "free" or as "sshd".
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
    } catch {
        return 'Other'
    }
    if ($listeners.Count -eq 0) { return 'Other' }

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

function Get-ExcludedTcpPortRanges {
    $netsh = Get-Command netsh.exe -ErrorAction SilentlyContinue
    if (-not $netsh) { return @() }

    try {
        $output = @(& $netsh.Source interface ipv4 show excludedportrange protocol=tcp 2>$null)
        return @(
            foreach ($line in $output) {
                if ([string]$line -match '^\s*(\d+)\s+(\d+)(?:\s|$)') {
                    [pscustomobject]@{ Start = [int]$Matches[1]; End = [int]$Matches[2] }
                }
            }
        )
    } catch {
        return @()
    } finally {
        # netsh is diagnostic here; its exit code must not leak into a
        # required step and turn an otherwise valid configuration into failure.
        $global:LASTEXITCODE = 0
    }
}

function Test-TcpPortExcluded {
    param([int]$Port)
    return [bool](Get-ExcludedTcpPortRanges | Where-Object { $Port -ge $_.Start -and $Port -le $_.End } | Select-Object -First 1)
}

function Get-TcpPortDiagnostic {
    param([int]$Port)

    $parts = New-Object System.Collections.Generic.List[string]
    try {
        $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop)
        if ($listeners.Count -eq 0) {
            [void]$parts.Add("TCP $Port listeners=none")
        } else {
            $owners = @(
                foreach ($pidValue in ($listeners | ForEach-Object OwningProcess | Sort-Object -Unique)) {
                    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
                    if ($process) { "$pidValue/$($process.ProcessName)" } else { "$pidValue/unknown" }
                }
            )
            [void]$parts.Add("TCP $Port listeners=$($owners -join ',')")
        }
    } catch {
        [void]$parts.Add("TCP $Port listener query failed: $($_.Exception.Message)")
    }

    $range = Get-ExcludedTcpPortRanges | Where-Object { $Port -ge $_.Start -and $Port -le $_.End } | Select-Object -First 1
    if ($range) {
        [void]$parts.Add("TCP $Port is in excluded range $($range.Start)-$($range.End)")
    } else {
        [void]$parts.Add("TCP $Port excluded-range match=none")
    }
    return ($parts -join ', ')
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


function Get-SshdForegroundDiagnostic {
    param(
        [string]$ConfigPath = (Join-Path $env:ProgramData 'ssh\sshd_config'),
        [int]$TimeoutMs = 5000
    )

    $sshd = Get-SshdExe
    if (-not $sshd) { return 'foreground diagnostic skipped: sshd.exe not found' }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        return "foreground diagnostic skipped: config not found ($ConfigPath)"
    }
    if ($ConfigPath.Contains('"')) {
        return 'foreground diagnostic skipped: config path contains a quote'
    }

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $sshd
    $startInfo.Arguments = "-D -ddd -e -f `"$ConfigPath`""
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo

    try {
        if (-not $process.Start()) { return 'foreground diagnostic failed to start sshd.exe' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $stayedRunning = -not $process.WaitForExit($TimeoutMs)
        $terminated = $true
        if ($stayedRunning) {
            try { $process.Kill() } catch {}
            $terminated = $process.WaitForExit(2000)
        } else {
            # Flush redirected asynchronous output after the process handle was
            # signaled. This overload returns immediately for an exited process.
            $process.WaitForExit()
        }
        if (-not $terminated) {
            return "foreground sshd stayed running for ${TimeoutMs}ms and could not be stopped within 2000ms"
        }

        $output = @([string]$stderrTask.Result, [string]$stdoutTask.Result) -join ' '
        $output = (($output -replace '[\r\n]+', ' ') -replace '\s{2,}', ' ').Trim()
        if ($output.Length -gt 1800) { $output = '...' + $output.Substring($output.Length - 1800) }
        if ($stayedRunning) {
            return "foreground sshd stayed running for ${TimeoutMs}ms and was stopped by diagnostics; output: $(if ($output) { $output } else { 'none' })"
        }
        return "foreground sshd exit=$($process.ExitCode); output: $(if ($output) { $output } else { 'none' })"
    } catch {
        return "foreground diagnostic failed: $($_.Exception.Message)"
    } finally {
        $process.Dispose()
        $global:LASTEXITCODE = 0
    }
}

function Get-SshdRuntimeDiagnostic {
    $parts = New-Object System.Collections.Generic.List[string]
    try {
        $windows = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop
        $displayVersion = [string](Get-JsonProperty -Object $windows -Name 'DisplayVersion' -Default '')
        $currentBuild = [string](Get-JsonProperty -Object $windows -Name 'CurrentBuild' -Default '')
        $updateBuildRevision = [string](Get-JsonProperty -Object $windows -Name 'UBR' -Default '')
        [void]$parts.Add("Windows=$displayVersion build=$currentBuild.$updateBuildRevision")
    } catch {
        [void]$parts.Add("Windows build query failed: $($_.Exception.Message)")
    }

    try {
        $sshd = Get-SshdExe
        if ($sshd) {
            $item = Get-Item -LiteralPath $sshd -ErrorAction Stop
            [void]$parts.Add("sshd version=$($item.VersionInfo.FileVersion) product=$($item.VersionInfo.ProductVersion) size=$($item.Length)B")
        } else {
            [void]$parts.Add('sshd version unavailable: executable not found')
        }
    } catch {
        [void]$parts.Add("sshd version query failed: $($_.Exception.Message)")
    }
    return ($parts -join ', ')
}

function Get-SshdFailureDetails {
    $details = New-Object System.Collections.Generic.List[string]
    $service = $null
    try {
        $service = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction Stop
        [void]$details.Add("service state=$($service.State), account=$($service.StartName), exit=$($service.ExitCode), service-exit=$($service.ServiceSpecificExitCode), path=$($service.PathName)")
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
    [void]$details.Add((Get-SshdRuntimeDiagnostic))

    try {
        $applicationEvent = Get-WinEvent -FilterHashtable @{
            LogName = 'Application'
            StartTime = (Get-Date).AddMinutes(-30)
        } -MaxEvents 300 -ErrorAction Stop |
            Where-Object { $_.Message -match 'sshd(?:\.exe)?|OpenSSH' } |
            Select-Object -First 1
        if ($applicationEvent) {
            $applicationMessage = (([string]$applicationEvent.Message -replace '[\r\n]+', ' ') -replace '\s{2,}', ' ').Trim()
            if ($applicationMessage.Length -gt 900) { $applicationMessage = $applicationMessage.Substring(0, 900) + '...' }
            [void]$details.Add("application event $($applicationEvent.Id)/$($applicationEvent.ProviderName): $applicationMessage")
        } else {
            [void]$details.Add('no recent sshd Application crash event was found')
        }
    } catch {
        [void]$details.Add("Application event query failed: $($_.Exception.Message)")
    }

    if ($script:SshPort) {
        [void]$details.Add((Get-TcpPortDiagnostic -Port $script:SshPort))
    }
    [void]$details.Add((Get-SshHostKeyDiagnostic))
    if ($service -and $service.State -eq 'Stopped') {
        [void]$details.Add((Get-SshdForegroundDiagnostic))
    } else {
        [void]$details.Add('foreground diagnostic skipped because sshd service is not stopped')
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
    $primaryListenerState = Get-TcpPortListenerState -Port 22
    $primaryExcluded = $primaryListenerState -eq 'Free' -and (Test-TcpPortExcluded -Port 22)
    $primaryState = if ($primaryExcluded) { 'Excluded' } else { $primaryListenerState }
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

    $fallbackListenerState = Get-TcpPortListenerState -Port 222
    $fallbackExcluded = $fallbackListenerState -eq 'Free' -and (Test-TcpPortExcluded -Port 222)
    $fallbackState = if ($fallbackExcluded) { 'Excluded' } else { $fallbackListenerState }
    if ($fallbackState -in @('Free', 'Sshd')) {
        $script:SshPort = 222
        if ($fallbackState -eq 'Free') {
            $primaryReason = if ($primaryExcluded) { 'excluded by Windows' } else { 'in use by another process' }
            Write-Warn "TCP port 22 is $primaryReason; configuring SSH on port $script:SshPort"
        } else {
            Write-Log "TCP port 222 is already used by sshd; keeping SSH on port $script:SshPort"
        }
        return
    }

    throw "TCP ports 22 and 222 are unavailable (22=$primaryState, 222=$fallbackState); cannot configure SSH"
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
    $targetNameProblem = Get-SshUserNameProblem -UserName $script:SshTargetUserName
    if ($targetNameProblem) {
        throw "built-in Administrator account name cannot be used in sshd_config ($targetNameProblem): $script:SshTargetUserName"
    }

    $administratorEnabled = -not [bool]$account.Disabled
    $script:SshAdministratorState = if ($administratorEnabled) { 'already enabled' } else { 'was disabled' }
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
            $script:SshAdministratorState = "could not be enabled ($enableFailure)"
        } else {
            $script:SshAdministratorState = 'enabled by this run'
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
    $normalizedUsers = New-Object 'System.Collections.Generic.List[string]'
    $seenUsers = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($userName in $UserNames) {
        if ([string]::IsNullOrWhiteSpace($userName) -or $userName -match '[\s"@,*?!]') {
            throw 'SSH target username cannot be represented safely in sshd_config'
        }
        # Preserve the account spelling returned by Windows.  Win32-OpenSSH
        # uses the value from -C user= for account lookup on Windows and can
        # reject a case-folded built-in account (e.g. `administrator` when
        # the actual RID-500 account is `Administrator`).
        $trimmedUserName = $userName.Trim()
        if ($seenUsers.Add($trimmedUserName)) { [void]$normalizedUsers.Add($trimmedUserName) }
    }
    $normalizedUsers = @($normalizedUsers)
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
    $result = New-Object 'System.Collections.Generic.List[string]'
    $seenUsers = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($userName in $sourceNames) {
        if ([string]::IsNullOrWhiteSpace($userName)) { continue }
        $nameProblem = Get-SshUserNameProblem -UserName $userName
        if ($nameProblem) { throw "SSH target username cannot be used in sshd_config ($nameProblem): $userName" }
        # Keep the canonical Windows account name for AllowUsers, Match User,
        # diagnostics, and the sshd -T -C validation context.
        $trimmedUserName = $userName.Trim()
        if ($seenUsers.Add($trimmedUserName)) { [void]$result.Add($trimmedUserName) }
    }
    return @($result)
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

function Set-SshHostPrivateKeyAcl {
    param([Parameter(Mandatory)][string]$Path)

    Set-RestrictedSshPathAcl -Path $Path
}

function Get-SshHostKeyDiagnostic {
    $sshDir = Join-Path $env:ProgramData 'ssh'
    $keys = @(Get-ChildItem -LiteralPath $sshDir -Filter 'ssh_host_*_key' -File -ErrorAction SilentlyContinue)
    if ($keys.Count -eq 0) { return 'host private keys=none' }

    $items = @(
        foreach ($key in $keys) {
            try {
                $acl = Get-Acl -LiteralPath $key.FullName -ErrorAction Stop
                "$($key.Name):$($key.Length)B owner=$($acl.Owner) inherited=$(-not $acl.AreAccessRulesProtected)"
            } catch {
                "$($key.Name):$($key.Length)B ACL-error=$($_.Exception.Message)"
            }
        }
    )
    return "host private keys=$($items -join ', ')"
}

function Initialize-SshHostKeys {
    $sshDir = Join-Path $env:ProgramData 'ssh'

    $keygen = Get-Command ssh-keygen.exe -ErrorAction SilentlyContinue
    if (-not $keygen) {
        $candidate = Get-NativeSystemPath 'OpenSSH\ssh-keygen.exe'
        if (Test-Path -LiteralPath $candidate) {
            $keygen = [pscustomobject]@{ Source = $candidate }
        }
    }
    if (-not $keygen) {
        Write-Warn 'ssh-keygen.exe not found; leaving host key generation to the sshd service.'
        return
    }

    Write-Log 'Ensuring all OpenSSH host keys exist...'
    & $keygen.Source -A | Out-Null
    Assert-NativeCommandSucceeded 'ssh-keygen -A host key generation'

    $keys = @(Get-ChildItem -LiteralPath $sshDir -Filter 'ssh_host_*_key' -File -ErrorAction Stop)
    if ($keys.Count -eq 0) { throw 'ssh-keygen -A completed, but no SSH host private key exists' }
    foreach ($key in $keys) {
        Set-SshHostPrivateKeyAcl -Path $key.FullName
    }
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


function Get-OpenSshInstallServices {
    return @(Get-CimInstance Win32_Service -Filter "Name='TrustedInstaller' OR Name='wuauserv' OR Name='BITS'" -ErrorAction Stop)
}

function Get-OpenSshInstallDiagnostics {
    $details = New-Object System.Collections.Generic.List[string]
    try {
        $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
        $details.Add("OS=$($os.Caption); Version=$($os.Version); Build=$($os.BuildNumber); Architecture=$($os.OSArchitecture)")
    } catch { $details.Add("OS query failed: $($_.Exception.Message)") }
    $details.Add("PowerShell=$($PSVersionTable.PSVersion); Is64BitProcess=$([Environment]::Is64BitProcess)")
    $dism = Get-NativeSystemPath 'dism.exe'
    try {
        $version = (Get-Item -LiteralPath $dism -ErrorAction Stop).VersionInfo.FileVersion
        $details.Add("DISM=$dism; Version=$version")
    } catch { $details.Add("DISM=$dism; unavailable: $($_.Exception.Message)") }
    try {
        $services = @(Get-OpenSshInstallServices)
        foreach ($name in @('TrustedInstaller', 'wuauserv', 'BITS')) {
            $service = $services | Where-Object Name -eq $name
            if ($service) {
                $details.Add("Service=$name; State=$($service.State); StartMode=$($service.StartMode); ExitCode=$($service.ExitCode)")
            } else { $details.Add("Service=$name; Missing") }
        }
    } catch { $details.Add("Service query failed: $($_.Exception.Message)") }
    return $details -join "`n"
}

function Get-OpenSshInstallError {
    param([System.Management.Automation.ErrorRecord]$Record)
    $parts = New-Object System.Collections.Generic.List[string]
    $parts.Add("$($Record.Exception.Message) [ErrorId=$($Record.FullyQualifiedErrorId)]")
    $exception = $Record.Exception
    while ($exception) {
        $parts.Add(('HRESULT=0x{0:X8}' -f $exception.HResult))
        if ($exception -is [System.ComponentModel.Win32Exception]) {
            $parts.Add("Win32=$($exception.NativeErrorCode)")
        }
        $exception = $exception.InnerException
    }
    return $parts -join '; '
}

function Write-OpenSshInstallLog {
    param([string]$Text)
    if (-not $script:OpenSshInstallLog) { return }
    try {
        Add-Content -LiteralPath $script:OpenSshInstallLog -Value $Text -Encoding UTF8 -ErrorAction Stop
    } catch { Write-Warn "Could not write OpenSSH install log: $($_.Exception.Message)" }
}

function Repair-OpenSshInstallServices {
    # Only disabled installation services are changed; normal demand-start services are left alone.
    $disabled = @(Get-OpenSshInstallServices | Where-Object StartMode -eq 'Disabled')
    foreach ($service in $disabled) {
        $name = $service.Name
        Write-Warn "Restoring installation service '$name' from Disabled to Manual."
        Write-OpenSshInstallLog "Service repair: $name; Disabled -> Manual (retained after setup)"
        Set-Service -Name $name -StartupType Manual -ErrorAction Stop
        Start-Service -Name $name -ErrorAction Stop
    }
    return $disabled.Count
}

function Invoke-OpenSshDismInstall {
    param([string]$CapabilityName)
    $dism = Get-NativeSystemPath 'dism.exe'
    if (-not (Test-Path -LiteralPath $dism -PathType Leaf)) {
        throw "Native Windows DISM was not found: $dism"
    }
    $previousErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell can otherwise turn native stderr into a terminating error.
        $ErrorActionPreference = 'Continue'
        $output = @(& $dism /Online /Add-Capability "/CapabilityName:$CapabilityName" /NoRestart 2>&1)
        $exitCode = $global:LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorPreference
    }
    Write-OpenSshInstallLog ("DISM exit=$exitCode`n" + (($output | ForEach-Object { "$_" }) -join "`n"))
    $global:LASTEXITCODE = 0
    if ($exitCode -notin @(0, 3010)) {
        $detail = (($output | Select-Object -Last 12 | ForEach-Object { "$_" }) -join ' ')
        $hint = if ($exitCode -eq 87) {
            ' Check Windows/DISM support and servicing provider load failures in DISM.log; error 87 alone does not prove Windows is too old.'
        } else { '' }
        throw "DISM failed (exit=$exitCode): $detail$hint"
    }
    return [pscustomobject]@{ RestartNeeded = ($exitCode -eq 3010) }
}

function Install-OpenSshServerCapability {
    $capabilityName = 'OpenSSH.Server~~~~0.0.1.0'
    $script:OpenSshRestartNeeded = $false
    $script:OpenSshInstallLog = $null

    if (Test-OpenSshServerInstalled) {
        Write-Log 'OpenSSH Server already installed, skipping'
        return
    }

    $script:OpenSshInstallLog = Join-Path $env:TEMP ("openssh-install-{0}.log" -f [guid]::NewGuid().ToString('N'))
    $diagnostics = Get-OpenSshInstallDiagnostics
    Write-Log "OpenSSH installation preflight:`n$diagnostics"
    Write-OpenSshInstallLog $diagnostics
    $failures = New-Object System.Collections.Generic.List[string]
    try {
        $capability = $null
        try {
            $capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction Stop |
                Select-Object -First 1
        } catch {
            $detail = Get-OpenSshInstallError $_
            Write-Warn "Windows capability query failed; trying installation directly: $detail"
            Write-OpenSshInstallLog "Capability query: $detail"
        }

        if ($capability -and $capability.State -eq 'Installed') {
            throw 'Windows reports OpenSSH Server as installed, but sshd.exe or the sshd service is missing'
        }
        if ($capability -and [string]$capability.State -in @('InstallPending', 'UninstallPending')) {
            $script:OpenSshRestartNeeded = $true
            throw '[REBOOT REQUIRED] OpenSSH capability has a pending operation. Restart Windows and rerun SETUP.ps1.'
        }

        Write-Log 'Installing OpenSSH Server...'
        $result = $null
        for ($attempt = 1; $attempt -le 2; $attempt++) {
            try {
                $result = Add-WindowsCapability -Online -Name $capabilityName -ErrorAction Stop
                break
            } catch {
                $detail = Get-OpenSshInstallError $_
                $failures.Add("Add-WindowsCapability attempt $attempt`: $detail")
                Write-OpenSshInstallLog $failures[$failures.Count - 1]
                try {
                    $disabled = @(Get-OpenSshInstallServices | Where-Object StartMode -eq 'Disabled')
                } catch {
                    throw "Cannot diagnose installation services: $(Get-OpenSshInstallError $_)"
                }
                if ($attempt -eq 1 -and $disabled.Count -gt 0) {
                    $null = Repair-OpenSshInstallServices
                    Write-Warn 'Installation services repaired; retrying OpenSSH installation once.'
                    continue
                }
                if ($disabled.Count -gt 0 -or $detail -match '0x80070422|Win32=1058\b') {
                    throw 'Installation services remain unavailable. Check service dependencies and organizational policy; DISM would use the same servicing infrastructure.'
                }
                Write-Warn "Capability installation failed; trying native Windows DISM: $detail"
                $result = Invoke-OpenSshDismInstall -CapabilityName $capabilityName
                break
            }
        }

        $script:OpenSshRestartNeeded = [bool]($result -and $result.RestartNeeded)
        Write-OpenSshInstallLog "Installation completed; RestartNeeded=$script:OpenSshRestartNeeded"
        $global:LASTEXITCODE = 0
        Update-ProcessPath
        if ($script:OpenSshRestartNeeded) {
            Write-Warn 'OpenSSH installation succeeded and Windows requested a restart. No automatic restart will be performed.'
            if (-not (Test-OpenSshServerInstalled)) {
                throw '[REBOOT REQUIRED] OpenSSH installation succeeded, but sshd is not available yet. Restart Windows and rerun SETUP.ps1.'
            }
        }
        $null = Wait-ServiceRegistered -Name sshd
        if (-not (Get-SshdExe)) {
            throw 'OpenSSH Server installation completed, but sshd.exe was not found'
        }
    } catch {
        $failure = Get-OpenSshInstallError $_
        $diagnostics = Get-OpenSshInstallDiagnostics
        $history = if ($failures) { ($failures -join "`n") + "`n" } else { '' }
        $message = "$history$failure`n$diagnostics`nInstall log: $script:OpenSshInstallLog`nServicing log: $env:WINDIR\Logs\DISM\dism.log"
        Write-OpenSshInstallLog $message
        throw $message
    }
}

function Enable-OpenSSHServer {
    Invoke-OpenSshOperation 'OpenSSH capability detection/installation' {
        Install-OpenSshServerCapability
    }

    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    $defaultCfg = Get-NativeSystemPath 'OpenSSH\sshd_config_default'
    $configHashBefore = Get-FileContentHash -Path $cfg
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
            Update-SshdServiceForConfigChange -ConfigPath $cfg -Port $SshPort -PreviousHash $configHashBefore
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
                if ($listenerState -eq 'Other') {
                    throw "TCP port $SshPort was taken by another process before sshd retry"
                }

                $backup = Repair-SshdConfigFromDefault -ConfigPath $cfg -TemplatePath $defaultCfg `
                    -Port $SshPort -UserNames (Get-SshConfigUserNames)
                Write-Warn "Previous sshd_config was preserved at $backup"
                Initialize-SshHostKeys
                Assert-SshdConfigValid -ConfigPath $cfg
                Update-SshdServiceForConfigChange -ConfigPath $cfg -Port $SshPort -PreviousHash $null
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
        $agent = Get-Service -Name ssh-agent -ErrorAction SilentlyContinue
        if ($agent) {
            Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction Stop
            if ($agent.Status -ne 'Running') {
                Start-Service -Name ssh-agent -ErrorAction Stop
            }
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
        $ranges = Get-TailscaleFirewallRanges
        Write-Log "Creating inbound firewall rule (TCP $SshPort from $($ranges -join ', '))..."
        New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort $SshPort `
            -Profile Any -RemoteAddress $ranges | Out-Null
    }

    # The managed rule is not the only way this port can be reachable: report
    # any other enabled inbound rule that exposes it to something wider than
    # the tailnet instead of silently leaving the machine open.
    $script:SshFirewallExposureProblems = @(Get-SshFirewallExposureProblems -Port $SshPort -ManagedRuleName 'OpenSSH-Server-In-TCP')
    foreach ($exposure in $script:SshFirewallExposureProblems) {
        Write-Warn "Another firewall rule exposes the SSH port: $exposure"
    }
}

function Get-SshdConfigSections {
    # sshd_config is a global section followed by any number of Match blocks.
    # Only the global section may be edited as a global option with "first
    # occurrence wins" semantics, so keep the two parts apart.
    param([string[]]$Lines)

    $matchIndex = -1
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        if ($Lines[$i] -match '^\s*Match(?:\s|$)') { $matchIndex = $i; break }
    }
    if ($matchIndex -lt 0) {
        return [pscustomobject]@{
            Global = @($Lines)
            Conditional = @()
        }
    }
    $globalLines = if ($matchIndex -gt 0) { @($Lines[0..($matchIndex - 1)]) } else { @() }
    return [pscustomobject]@{
        Global = $globalLines
        Conditional = @($Lines[$matchIndex..($Lines.Count - 1)])
    }
}

function Get-SshdUnmanagedConditionalLines {
    # Conditional entries this script does not own.  Options inside the managed
    # block are written on purpose and must not raise a warning.
    param([string[]]$Lines)

    $result = New-Object System.Collections.Generic.List[string]
    $insideManagedBlock = $false
    foreach ($line in @($Lines)) {
        if ($line -eq $script:ManagedUserBlockBegin) { $insideManagedBlock = $true; continue }
        if ($line -eq $script:ManagedUserBlockEnd) { $insideManagedBlock = $false; continue }
        if ($insideManagedBlock) { continue }
        $result.Add($line)
    }
    return @($result)
}

function Set-SshdOption {
    # Writes a global option at the top of the file (first occurrence wins) and
    # leaves a same-named option inside a Match block untouched so an operator
    # keeps whatever they configured there.
    param([string]$File, [string]$Key, [string]$Value)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "sshd_config not found, refusing to skip option '$Key': $File"
    }
    $sections = Get-SshdConfigSections -Lines @([IO.File]::ReadAllLines($File))
    $pattern = "^\s*$([regex]::Escape($Key))\s+"
    $kept = @($sections.Global | Where-Object { $_ -notmatch $pattern })

    $conditionalMatches = @(
        Get-SshdUnmanagedConditionalLines -Lines $sections.Conditional |
            Where-Object { $_ -match $pattern }
    )
    if ($conditionalMatches.Count) {
        Write-Warn "sshd_config sets '$Key' inside a Match block; the global '$Key $Value' is the one in effect."
    }

    Write-SshdConfigLines -File $File -Lines (@("$Key $Value") + $kept + $sections.Conditional)
}

function Remove-SshdOption {
    # Disables a global option by commenting it out instead of deleting it, so
    # the previous value stays visible and can be restored by hand.  A
    # same-named option inside a Match block is left untouched.
    # -Discard removes the option without leaving a comment; it is meant for
    # options the caller rewrites immediately (an old alias, for example), where
    # a comment would only add noise on every run.
    param([string]$File, [string]$Key, [switch]$Discard)
    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) {
        throw "sshd_config not found, refusing to remove option '$Key': $File"
    }
    $sections = Get-SshdConfigSections -Lines @([IO.File]::ReadAllLines($File))
    $pattern = "^\s*$([regex]::Escape($Key))(?:\s|$)"
    $commentPrefix = "$script:DisabledOptionCommentPrefix$Key "

    $updated = New-Object System.Collections.Generic.List[string]
    $disabled = 0
    # One comment per option keeps the original value visible and keeps the
    # file stable: a later run finds nothing active to disable and adds nothing.
    $alreadyRecorded = @(
        $sections.Global | Where-Object { $_.StartsWith($commentPrefix, [StringComparison]::Ordinal) }
    ).Count -gt 0
    foreach ($line in @($sections.Global)) {
        if ($line -match $pattern) {
            $disabled++
            if (-not $Discard -and -not $alreadyRecorded) { $updated.Add("$commentPrefix$line") }
            continue
        }
        $updated.Add($line)
    }

    $conditionalMatches = @(
        Get-SshdUnmanagedConditionalLines -Lines $sections.Conditional |
            Where-Object { $_ -match $pattern }
    )
    if ($conditionalMatches.Count) {
        Write-Warn "sshd_config sets '$Key' inside a Match block; leaving that entry untouched."
    }

    if ($disabled -gt 0) {
        Write-SshdConfigLines -File $File -Lines (@($updated) + $sections.Conditional)
    }
}

function Set-ManagedSshdConfig {
    param([string]$File, [int]$Port, [string[]]$UserNames)

    Remove-SshdOption -File $File -Key 'ListenAddress'
    # sshd listens on both families; the firewall rule restrict both to the
    # tailnet instead of hiding the IPv6 listener behind AddressFamily inet.
    Set-SshdOption -File $File -Key 'AddressFamily' -Value 'any'
    Set-SshdOption -File $File -Key 'Port' -Value $Port
    Set-SshdOption -File $File -Key 'PasswordAuthentication' -Value 'no'

    # Old Windows 10 builds use the ChallengeResponseAuthentication alias; it is
    # discarded rather than commented because it is rewritten right below.
    Remove-SshdOption -File $File -Key 'ChallengeResponseAuthentication' -Discard
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

function Set-AuthorizedKeys {
    if (-not $SshPublicKeys -or $SshPublicKeys.Count -eq 0) {
        throw '$SshPublicKeys is empty; refusing to disable password authentication without a public key'
    }

    $authFile = $SshTargetAuthorizedKeysFile
    $authDir = Split-Path -Parent $authFile
    if (-not (Test-Path -LiteralPath $authDir -PathType Container)) {
        New-Item -ItemType Directory -Path $authDir -Force | Out-Null
    }
    Set-SshDirectoryAcl -Path $authDir

    if (-not (Test-Path $authFile)) { New-Item -ItemType File -Path $authFile -Force | Out-Null }
    Grant-SshPathAclRepairAccess -Path $authFile

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

    Set-RestrictedSshPathAcl -Path $authFile
    Assert-AuthorizedKeysReady -File $authFile
    Assert-SshDirectoryAclReady -Path $authDir

    Write-Log "authorized_keys configured ($authFile), $added key(s) added this run."
}

function Assert-AuthorizedKeysReady {
    param([string]$File)

    if (-not (Test-Path -LiteralPath $File -PathType Leaf)) { throw "authorized_keys file not found: $File" }
    $existing = @(Get-Content -LiteralPath $File -ErrorAction Stop)
    foreach ($key in $SshPublicKeys) {
        if ($existing -notcontains $key.Trim()) { throw "configured public key is missing: $(($key -split '\s+')[-1])" }
    }

    Assert-RestrictedSshPathAcl -Path $File -Label $File
}

function Enable-X11Forwarding {
    $cfg = Join-Path $env:ProgramData 'ssh\sshd_config'
    if (-not (Test-Path -LiteralPath $cfg)) {
        Write-Warn "$cfg not found (OpenSSH Server may not have started yet), skipping X11 config."
        return
    }
    $configHashBefore = Get-FileContentHash -Path $cfg
    Write-Log "Enabling X11 forwarding ($cfg)..."
    Set-SshdOption -File $cfg -Key 'X11Forwarding' -Value 'yes'
    Set-SshdOption -File $cfg -Key 'X11UseLocalhost' -Value 'yes'

    Assert-SshdConfigValid
    Update-SshdServiceForConfigChange -ConfigPath $cfg -Port $SshPort -PreviousHash $configHashBefore
    Assert-SshdHealthy -Port $SshPort

    Write-Warn 'Native Windows has no built-in X server; to display remote GUI windows, install VcXsrv / Xming on the client.'
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
        if (-not (Test-AdministratorAuthorizedKeysValue -Value $keyFile -Path $SshTargetAuthorizedKeysFile)) {
            throw "effective AuthorizedKeysFile is incorrect for $($userName): $keyFile"
        }
    }
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
        $expected = (Get-TailscaleFirewallRanges) -join ', '
        throw "OpenSSH firewall rule is not limited to the tailnet ranges ($expected): $($remoteAddresses -join ', ')"
    }
}


function Assert-SshServerReady {
    param([int]$Port, [string]$TailscaleIp)

    Assert-SshdEffectiveConfig -Port $Port -TailscaleIp $TailscaleIp
    $authFile = $SshTargetAuthorizedKeysFile
    Assert-AuthorizedKeysReady -File $authFile
    Assert-SshDirectoryAclReady -Path (Split-Path -Parent $authFile)
    Assert-SshdHealthy -Port $Port
    Assert-SshFirewallReady -Port $Port
    Assert-SshBanner -Address $TailscaleIp -Port $Port

    # sshd listens on both families now, so verify the IPv6 path too when
    # Tailscale handed this node an address for it.
    $ipv6 = Get-TailscaleIpAddress -Exe (Get-TailscaleExe) -Family 6
    if ($ipv6) {
        if (-not (Test-TailscaleIPv6 $ipv6)) {
            throw "Tailscale returned an unexpected IPv6 address: $ipv6"
        }
        Assert-SshBanner -Address $ipv6 -Port $Port
        Write-Log "SSH also answers on the Tailscale IPv6 address $ipv6"
    } else {
        Write-Warn 'This node has no Tailscale IPv6 address; only the IPv4 listener could be verified.'
    }

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

# ---------------------------------------------------------------------------
# Configuration fingerprints and restart decisions
# ---------------------------------------------------------------------------

function Get-FileContentHash {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash
    } catch {
        return $null
    }
}

function Get-SshdRestartDecision {
    # Windows OpenSSH has no reload, and restarting sshd interrupts live
    # sessions, so only touch the service when something really changed.
    param([string]$ConfigPath, [int]$Port, [AllowNull()][string]$PreviousHash)

    $service = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if (-not $service -or $service.Status -ne 'Running') { return 'Start' }

    $currentHash = Get-FileContentHash -Path $ConfigPath
    if (-not $PreviousHash -or -not $currentHash -or $currentHash -ne $PreviousHash) { return 'Restart' }
    if ((Get-TcpPortListenerState -Port $Port) -ne 'Sshd') { return 'Restart' }

    # The listener must have started after the last write to the config file,
    # otherwise it may be running with an older revision of it.
    try {
        $listener = Get-Process -Name sshd -ErrorAction Stop |
            Sort-Object StartTime | Select-Object -First 1
        $configItem = Get-Item -LiteralPath $ConfigPath -ErrorAction Stop
        if ($listener -and $configItem -and $listener.StartTime -gt $configItem.LastWriteTime) {
            return 'Skip'
        }
    } catch {}
    return 'Restart'
}

function Update-SshdServiceForConfigChange {
    param([string]$ConfigPath, [int]$Port, [AllowNull()][string]$PreviousHash)

    switch (Get-SshdRestartDecision -ConfigPath $ConfigPath -Port $Port -PreviousHash $PreviousHash) {
        'Start' {
            Write-Log 'Starting the sshd service...'
            Start-Service -Name sshd -ErrorAction Stop
            return 'Started'
        }
        'Skip' {
            Write-Log "sshd_config is unchanged and sshd already listens on TCP $Port; keeping the running service."
            return 'Unchanged'
        }
        default {
            Write-Log 'Restarting sshd to load the updated configuration...'
            Restart-Service -Name sshd -ErrorAction Stop
            return 'Restarted'
        }
    }
}

# ---------------------------------------------------------------------------
# ACL application
# ---------------------------------------------------------------------------

function Grant-SshPathAclRepairAccess {
    # A previous run may have removed the launching administrator from the DACL
    # before it could set the final owner.  Temporarily make the built-in
    # Administrators group the owner so an elevated administrator or SYSTEM can
    # repair and rerun the setup.
    param(
        [string]$Path,
        [switch]$Container
    )

    & takeown.exe /F $Path /A | Out-Null
    Assert-NativeCommandSucceeded "take ownership for ACL repair: $Path"

    $rights = if ($Container) { '(OI)(CI)(F)' } else { '(F)' }
    & icacls.exe $Path /grant "*S-1-5-32-544:$rights" | Out-Null
    Assert-NativeCommandSucceeded "grant Administrators ACL repair access: $Path"
}

function Set-RestrictedSshPathAcl {
    # Final state for a file holding key material: no inheritance, owner is the
    # built-in Administrators group, and only SYSTEM and Administrators have
    # FullControl.
    param(
        [string]$Path,
        [switch]$Container
    )

    $rights = if ($Container) { '(OI)(CI)(F)' } else { '(F)' }
    & icacls.exe $Path /reset | Out-Null
    Assert-NativeCommandSucceeded "reset ACL: $Path"
    & icacls.exe $Path /grant:r "*S-1-5-18:$rights" "*S-1-5-32-544:$rights" | Out-Null
    Assert-NativeCommandSucceeded "grant restricted ACL: $Path"
    & icacls.exe $Path '/inheritance:r' | Out-Null
    Assert-NativeCommandSucceeded "disable ACL inheritance: $Path"
    & icacls.exe $Path /setowner '*S-1-5-32-544' | Out-Null
    Assert-NativeCommandSucceeded "set ACL owner: $Path"
}

function Set-SshDirectoryAcl {
    # The ssh directory stays readable for non-administrators because the
    # OpenSSH client reads the system-wide ssh_config from it, but it must not
    # be writable by them: a writable directory would let anyone plant files
    # next to the managed key material.
    param([string]$Path)

    & takeown.exe /F $Path /A | Out-Null
    Assert-NativeCommandSucceeded "take ownership of the SSH directory: $Path"
    & icacls.exe $Path '/inheritance:r' | Out-Null
    Assert-NativeCommandSucceeded "disable SSH directory ACL inheritance: $Path"
    & icacls.exe $Path /grant:r '*S-1-5-18:(OI)(CI)(F)' '*S-1-5-32-544:(OI)(CI)(F)' '*S-1-5-11:(OI)(CI)(RX)' | Out-Null
    Assert-NativeCommandSucceeded "apply SSH directory ACL: $Path"
    & icacls.exe $Path /setowner '*S-1-5-32-544' | Out-Null
    Assert-NativeCommandSucceeded "set SSH directory owner: $Path"
}

function Assert-RestrictedSshPathAcl {
    param([string]$Path, [string]$Label)
    $problems = @(Get-RestrictedSshPathAclProblems -Path $Path -Label $Label)
    if ($problems.Count) { throw "$Label ACL is not restricted: $($problems -join '; ')" }
}

function Assert-SshDirectoryAclReady {
    param([string]$Path)
    $problems = @(Get-SshDirectoryAclProblems -Path $Path -Label $Path)
    if ($problems.Count) { throw "SSH directory ACL is not restricted: $($problems -join '; ')" }
}

# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

function Assert-SshBanner {
    param([string]$Address, [int]$Port, [int]$TimeoutMs = 5000)
    return Get-SshBanner -Address $Address -Port $Port -TimeoutMs $TimeoutMs
}

function Get-OpenSshInstallLogTail {
    # The failure notification carries the tail of the OpenSSH install log so a
    # remote operator sees the DISM output that caused the failure.
    param([int]$MaxChars = 12000)

    $path = $script:OpenSshInstallLog
    if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        $text = [IO.File]::ReadAllText($path).Trim()
    } catch {
        return "log unreadable: $($_.Exception.Message)"
    }
    if (-not $text) { return $null }
    if ($text.Length -gt $MaxChars) { $text = '...' + $text.Substring($text.Length - $MaxChars) }
    return $text
}

function Test-FirewallPortFilterCoversPort {
    param([AllowNull()]$Filter, [int]$Port)
    if (-not $Filter) { return $false }
    $protocol = [string]$Filter.Protocol
    if ($protocol -notin @('TCP', 'Any', '6', '256')) { return $false }
    $localPort = [string]$Filter.LocalPort
    if ([string]::IsNullOrWhiteSpace($localPort)) { return $false }
    if ($localPort -in @('Any', '*')) { return $true }
    foreach ($part in ($localPort -split ',')) {
        $value = $part.Trim()
        if ($value -eq [string]$Port) { return $true }
        if ($value -match '^(\d+)\s*-\s*(\d+)$' -and $Port -ge [int]$Matches[1] -and $Port -le [int]$Matches[2]) {
            return $true
        }
    }
    return $false
}

function Get-SshFirewallExposureProblems {
    # Audits every enabled inbound allow rule and reports the ones that expose
    # the SSH port to something wider than the tailnet, so an unrelated rule
    # cannot silently publish the port this script just configured.
    param([int]$Port, [string]$ManagedRuleName)

    $problems = New-Object System.Collections.Generic.List[string]
    try {
        $rules = @(Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow -ErrorAction Stop)
    } catch {
        $problems.Add("firewall audit could not enumerate inbound rules: $($_.Exception.Message)")
        return @($problems)
    }

    foreach ($rule in $rules) {
        if ($ManagedRuleName -and $rule.Name -eq $ManagedRuleName) { continue }

        $coversPort = $false
        foreach ($filter in @($rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue)) {
            if (Test-FirewallPortFilterCoversPort -Filter $filter -Port $Port) { $coversPort = $true; break }
        }
        if (-not $coversPort) { continue }

        $remoteAddresses = @(
            $rule | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue |
                ForEach-Object { $_.RemoteAddress }
        )
        if (Test-TailscaleFirewallRange $remoteAddresses) { continue }
        $problems.Add("$($rule.DisplayName) [$($rule.Name)] allows TCP $Port from $(@($remoteAddresses) -join ', ')")
    }
    return @($problems)
}


# ---------------------------------------------------------------------------
# 6. Orchestration and result reporting
# ---------------------------------------------------------------------------

function Get-SetupContextLines {
    # Facts about the run that belong in the report on every outcome: account
    # state changes, firewall audit findings and preserved Tailscale options.
    $lines = New-Object System.Collections.Generic.List[string]
    if ($script:SshTargetUserName) {
        $lines.Add("Built-in Administrator account ($($script:SshTargetUserName)): $script:SshAdministratorState")
    }
    if ($script:TailscalePrefsReset) {
        $lines.Add('Tailscale: previous tailscale up options were reset (--reset)')
    } elseif ($script:TailscalePreservedPrefs) {
        $lines.Add("Tailscale: kept existing tailscale up options ($($script:TailscalePreservedPrefs))")
    }
    foreach ($exposure in @($script:SshFirewallExposureProblems)) {
        $lines.Add("Firewall audit: $exposure")
    }
    return @($lines)
}

function Get-SetupUserList {
    try {
        return (Get-SshConfigUserNames) -join ', '
    } catch {
        return '<unresolved>'
    }
}

function Invoke-Setup {
    param([switch]$ResetPrefs)

    $FailedSteps.Clear()
    $script:ValidatedTailscaleIp = $null
    $script:OpenSshRestartNeeded = $false
    $script:SshAdministratorState = 'unknown'
    $script:SshFirewallExposureProblems = @()
    $script:TailscalePreservedPrefs = $null
    $script:TailscalePrefsReset = [bool]$ResetPrefs
    $tgConfig = if ($TgBotToken -and $TgChatId) {
        @{ Token = $TgBotToken; ChatId = $TgChatId }
    } else { $null }

    Write-Log 'Starting configuration (platform: Windows OpenSSH over Tailscale)'
    try {
        Invoke-RequiredStep 'Enable and validate SSH target accounts' { Initialize-SshTargetUsers }
        Invoke-RequiredStep 'Install Tailscale' { Install-Tailscale }
        Invoke-RequiredStep 'Tailscale service autostart' { Enable-TailscaleService }
        Invoke-RequiredStep 'Tailscale login' { Connect-Tailscale -AuthKey $TsAuthKey -ResetPrefs:$ResetPrefs }
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
        $failureText = if ($FailedSteps.Count) { $FailedSteps -join "`n- " } else { $_.Exception.Message }
        $rebootRequired = $failureText.Contains('[REBOOT REQUIRED]')
        if ($rebootRequired) {
            Write-Warn 'SSH server setup is waiting for a Windows restart. Restart Windows and rerun SETUP.ps1.'
        } else { Write-Err 'SSH server setup failed.' }
        foreach ($failure in $FailedSteps) { Write-Host "    - $failure" -ForegroundColor Yellow }
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
        $contextLines = @(Get-SetupContextLines)
        $contextSection = if ($contextLines.Count) { ($contextLines -join "`n") + "`n" } else { '' }
        # The docs promise the tail of the install log in the failure message:
        # the DISM output is what identifies a failed capability install.
        $logTail = Get-OpenSshInstallLogTail -MaxChars 12000
        $logSection = if ($logTail) { "`nOpenSSH install log (tail):`n$logTail`n" } else { '' }
        $statusLabel = if ($rebootRequired) { '[REBOOT REQUIRED]' } else { '[FAILED]' }
        $detailLabel = if ($rebootRequired) { 'Details' } else { 'Failure' }
        $restartNote = if ($script:OpenSshRestartNeeded -and -not $rebootRequired) {
            'Windows also requested a restart after OpenSSH installation. Restart Windows and rerun SETUP.ps1.'
        } else { '' }
        $failureMessage = @"
$statusLabel Windows OpenSSH server setup
Host: $env:COMPUTERNAME (Windows)
Users: $(Get-SetupUserList)
$contextSection$($detailLabel):
- $failureText
$restartNote
$sshLoginDetails
$logSection
"@
        if ($tgConfig -and -not (Send-Telegram -Config $tgConfig -Text $failureMessage)) {
            Write-Warn 'Telegram failure notification could not be delivered.'
        }
        if ($rebootRequired) { return 3010 }
        return 1
    }

    $publicIp = Get-PublicIp
    $loginLine = Get-SshLoginText -Port $SshPort -TailscaleIp $script:ValidatedTailscaleIp
    $contextLines = @(Get-SetupContextLines)
    Write-Host ''
    Write-Host '==================== Summary ====================' -ForegroundColor Green
    Write-Log "This machine's Tailscale IP: $script:ValidatedTailscaleIp"
    Write-Log "Server-side SSH readiness checks passed: $loginLine"
    foreach ($contextLine in $contextLines) { Write-Log $contextLine }
    Write-Warn 'This is Windows OpenSSH over Tailscale; no Tailscale SSH badge is expected.'
    $restartNote = if ($script:OpenSshRestartNeeded) {
        'Windows requested a restart after OpenSSH installation. SSH checks passed; restart at a convenient time and rerun SETUP.ps1.'
    } else { '' }
    if ($restartNote) { Write-Warn $restartNote }

    $contextSection = if ($contextLines.Count) { ($contextLines -join "`n") + "`n" } else { '' }
    $successMessage = @"
[READY] Windows OpenSSH server-side checks passed
Host: $env:COMPUTERNAME (Windows)
Users: $(Get-SetupUserList)
Tailscale IP: $script:ValidatedTailscaleIp
Public IP: $(if ($publicIp) { $publicIp } else { 'pending' })
$contextSection$restartNote

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
$setupExitCode = Invoke-Setup -ResetPrefs:$ResetTailscalePrefs
exit $setupExitCode
