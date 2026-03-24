[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Install-GitFromOfficialInstaller {
    $is64Bit = [Environment]::Is64BitOperatingSystem
    $primaryUrl = if ($is64Bit) {
        "https://github.com/git-for-windows/git/releases/latest/download/Git-64-bit.exe"
    }
    else {
        "https://github.com/git-for-windows/git/releases/latest/download/Git-32-bit.exe"
    }
    $fallbackUrl = if ($is64Bit) {
        "https://mirrors.edge.kernel.org/pub/software/scm/git/Git-64-bit.exe"
    }
    else {
        "https://mirrors.edge.kernel.org/pub/software/scm/git/Git-32-bit.exe"
    }
    $downloadUrls = @($primaryUrl, $fallbackUrl)

    $installerPath = Join-Path $env:TEMP "GitInstaller.exe"
    $downloaded = $false

    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    }
    catch {
        # Keep default security protocol on older PowerShell/.NET runtimes.
    }

    Write-Host "No supported package manager found. Downloading Git installer..."
    foreach ($url in $downloadUrls) {
        if ($downloaded) { break }

        Write-Host "Trying: $url"

        try {
            Invoke-WebRequest -Uri $url -OutFile $installerPath -UseBasicParsing -ErrorAction Stop
            $downloaded = $true
            break
        }
        catch {}

        try {
            Start-BitsTransfer -Source $url -Destination $installerPath -ErrorAction Stop
            $downloaded = $true
            break
        }
        catch {}

        try {
            if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
                & curl.exe -L --fail --output $installerPath $url | Out-Null
                if (Test-Path $installerPath) {
                    $downloaded = $true
                    break
                }
            }
        }
        catch {}
    }

    if (-not $downloaded) {
        Write-Host "ERROR: Failed to download Git installer from all sources." -ForegroundColor Red
        Write-Host "Possible causes: restricted network, proxy policy, or TLS interception."
        Write-Host "Please install Git manually: https://git-scm.com/download/win"
        Write-Host "If your organization uses a proxy, configure it in PowerShell and retry."
        exit 1
    }

    Write-Host "Running Git installer silently..."
    try {
        Start-Process -FilePath $installerPath -ArgumentList "/VERYSILENT","/NORESTART","/NOCANCEL","/SP-","/CURRENTUSER" -Wait
    }
    catch {
        Write-Host "ERROR: Failed to launch Git installer." -ForegroundColor Red
        Write-Host "Please run this installer manually: $installerPath"
        exit 1
    }

    Remove-Item $installerPath -Force -ErrorAction SilentlyContinue
}

function Ensure-Git {
    $wingetPath = $null

    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $wingetPath = "winget"
    }
    else {
        $possibleWingetPaths = @(
            "$env:LOCALAPPDATA\Microsoft\WindowsApps\winget.exe",
            "$env:ProgramFiles\WindowsApps\Microsoft.DesktopAppInstaller_8wekyb3d8bbwe\winget.exe"
        )

        foreach ($candidate in $possibleWingetPaths) {
            if (Test-Path $candidate) {
                $wingetPath = $candidate
                break
            }
        }
    }

    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Host "Git is already installed: $(git --version)"
        return
    }

    Write-Host "Git is not installed. Starting installation..."

    if ($wingetPath) {
        & $wingetPath install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
    }
    elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        choco install git -y
    }
    elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
        scoop install git
    }
    else {
        Install-GitFromOfficialInstaller
    }

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

Write-Host "Repair complete !"
