<#
    setup.ps1 — SSH + Tailscale 一键配置 (Windows 原生)

    功能:
      1. 安装 Tailscale (winget, 回退官方 MSI)
      2. Tailscale 服务设为自动启动
      3. 用 auth key 无人值守登录 (tailscale up --authkey --unattended)
      4. 开启 OpenSSH Server 并设为开机自启 + 防火墙放行
      5. 把公钥幂等写入 authorized_keys

    配置: Tailscale auth key / SSH 公钥 / Telegram token 与 chat_id 均硬编码于本文件

    用法:  右键 "使用 PowerShell 运行", 或  powershell -ExecutionPolicy Bypass -File setup.ps1
           (非管理员时会自动请求 UAC 提权)
#>

param(
    [string]$RelaunchWorkingDirectory
)

# ---------------------------------------------------------------------------
# 自提权: 非管理员则以管理员重启自身
# ---------------------------------------------------------------------------
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
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

    try {
        $elevated = Start-Process -FilePath $psExe -ArgumentList $relaunchArgs `
            -Verb RunAs -Wait -PassThru
        $code = if ($null -ne $elevated.ExitCode) { $elevated.ExitCode } else { 0 }
        exit $code
    } catch {
        Write-Host '[ERROR] 需要管理员权限; 提权被取消或阻止。' -ForegroundColor Red
        exit 1
    }
}

if ($RelaunchWorkingDirectory -and (Test-Path -LiteralPath $RelaunchWorkingDirectory -PathType Container)) {
    Set-Location -LiteralPath $RelaunchWorkingDirectory
}

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 硬编码配置
# ---------------------------------------------------------------------------
$TsAuthKey = 'tskey-auth-kiLmAL1dzY11CNTRL-8kBw3rQUum5U8wepNaB6n5KzhgmcHBmkK'  #有效期:2026-10-05/Tags:fish
$SshPublicKeys = @(
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB star-mac',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ star-wsl'
)
$TgBotToken = '7724790582:AAE2Jish4jeQ_uheEuTgAeKIt1um0ml4-HM'
$TgChatId   = '7765138435'

$FailedSteps = New-Object System.Collections.Generic.List[string]

function Write-Log  { param($m) Write-Host "[*] $m" -ForegroundColor Cyan }
function Write-Warn { param($m) Write-Host "[!] $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "[ERROR] $m" -ForegroundColor Red }

function Invoke-Step {
    param(
        [string]$Desc,
        [scriptblock]$Action
    )
    try {
        & $Action
        if ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) {
            $FailedSteps.Add("$Desc (exit=$LASTEXITCODE)")
        }
    } catch {
        $FailedSteps.Add("$Desc ($($_.Exception.Message))")
    }
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# 刷新当前进程 PATH (安装 Tailscale 后需要)
function Update-ProcessPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
    # Tailscale 默认安装目录, 确保可被调用
    $tsDir = Join-Path $env:ProgramFiles 'Tailscale'
    if ((Test-Path $tsDir) -and ($env:Path -notlike "*$tsDir*")) {
        $env:Path = "$env:Path;$tsDir"
    }
}

# 定位 tailscale.exe
function Get-TailscaleExe {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $candidate) { return $candidate }
    return $null
}

# 通过 Telegram Bot API 发送一条消息
function Send-Telegram {
    param(
        [hashtable]$Config,
        [string]$Text
    )
    if (-not $Config) { return $false }
    try {
        $uri = "https://api.telegram.org/bot$($Config.Token)/sendMessage"
        $body = @{
            chat_id                  = $Config.ChatId
            text                     = $Text
            disable_web_page_preview = $true
        }
        Invoke-RestMethod -Uri $uri -Method Post -Body $body -TimeoutSec 15 | Out-Null
        return $true
    } catch {
        return $false
    }
}

# ---------------------------------------------------------------------------
# 1 & 2. 安装 Tailscale + 服务自启
# ---------------------------------------------------------------------------
function Install-Tailscale {
    if (Get-TailscaleExe) {
        Write-Log 'Tailscale 已安装, 跳过'
        return
    }

    if (Test-CommandExists 'winget') {
        Write-Log '通过 winget 安装 Tailscale...'
        winget install --id Tailscale.Tailscale -e --silent `
            --accept-source-agreements --accept-package-agreements
    } else {
        Write-Log 'winget 不可用, 下载官方 MSI 静默安装...'
        $msi = Join-Path $env:TEMP 'tailscale-setup.msi'
        try {
            Invoke-WebRequest -Uri 'https://pkgs.tailscale.com/stable/tailscale-setup-latest.msi' `
                -OutFile $msi -UseBasicParsing
            Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /quiet /norestart" -Wait
        } catch {
            Write-Err "MSI 下载/安装失败: $($_.Exception.Message)"
            throw
        }
    }
    Update-ProcessPath
}

function Enable-TailscaleService {
    $svc = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
    if ($svc) {
        Set-Service -Name Tailscale -StartupType Automatic -ErrorAction SilentlyContinue
        if ($svc.Status -ne 'Running') {
            Start-Service -Name Tailscale -ErrorAction SilentlyContinue
        }
    } else {
        Write-Warn 'Tailscale 服务未找到 (可能安装尚未完成), 跳过服务配置。'
    }
}

# ---------------------------------------------------------------------------
# 3. Tailscale 登录
# ---------------------------------------------------------------------------
function Connect-Tailscale {
    param([string]$AuthKey)
    if (-not $AuthKey) {
        Write-Err '未找到 Tailscale auth key。请在脚本顶部的 $TsAuthKey 变量中填入。'
        throw 'missing-authkey'
    }
    $ts = Get-TailscaleExe
    if (-not $ts) { Write-Err 'tailscale.exe 未找到'; throw 'tailscale-not-found' }

    Write-Log '登录 Tailscale (auth key 已读取, 不回显)...'
    # --unattended: 重启后无需交互保持连接
    & $ts up --authkey $AuthKey --unattended
}

# ---------------------------------------------------------------------------
# 4. OpenSSH Server
# ---------------------------------------------------------------------------
function Enable-OpenSSHServer {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue
    if ($cap -and $cap.State -ne 'Installed') {
        Write-Log '安装 OpenSSH Server...'
        Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    } else {
        Write-Log 'OpenSSH Server 已安装, 跳过'
    }

    # 首次启动会生成主机密钥并创建默认 sshd_config
    Set-Service -Name sshd -StartupType Automatic -ErrorAction SilentlyContinue
    Start-Service -Name sshd -ErrorAction SilentlyContinue

    # ssh-agent 一并设为自动 (可选, 便于密钥管理)
    Set-Service -Name ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue

    # 防火墙: 放行入站 22 端口
    $ruleName = 'OpenSSH-Server-In-TCP'
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if (-not $rule) {
        Write-Log '创建防火墙入站规则 (TCP 22)...'
        New-NetFirewallRule -Name $ruleName -DisplayName 'OpenSSH Server (sshd)' `
            -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    } else {
        Enable-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# 5. authorized_keys
# ---------------------------------------------------------------------------
function Set-AuthorizedKeys {
    if (-not $SshPublicKeys -or $SshPublicKeys.Count -eq 0) {
        Write-Warn '$SshPublicKeys 为空, 跳过公钥配置。'
        return
    }

    # 判断当前用户是否属于管理员组
    $isAdminUser = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if ($isAdminUser) {
        # Windows OpenSSH 对管理员组的特殊约定文件
        $authFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        $authDir = Split-Path -Parent $authFile
        if (-not (Test-Path $authDir)) { New-Item -ItemType Directory -Path $authDir -Force | Out-Null }
    } else {
        $sshDir = Join-Path $env:USERPROFILE '.ssh'
        if (-not (Test-Path $sshDir)) { New-Item -ItemType Directory -Path $sshDir -Force | Out-Null }
        $authFile = Join-Path $sshDir 'authorized_keys'
    }

    if (-not (Test-Path $authFile)) { New-Item -ItemType File -Path $authFile -Force | Out-Null }

    $existing = @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue)
    $added = 0
    foreach ($raw in $SshPublicKeys) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($existing -contains $line) { continue }
        Add-Content -LiteralPath $authFile -Value $line
        $existing += $line
        $added++
    }

    # 权限收紧: administrators_authorized_keys 仅 SYSTEM + Administrators
    if ($isAdminUser) {
        icacls $authFile /inheritance:r | Out-Null
        icacls $authFile /grant 'SYSTEM:F' | Out-Null
        icacls $authFile /grant 'BUILTIN\Administrators:F' | Out-Null
    }

    Write-Log "authorized_keys 配置完成 ($authFile), 本次新增 $added 个公钥。"
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
Write-Log '开始配置 (平台: Windows)'

Invoke-Step '安装 Tailscale'      { Install-Tailscale }
Invoke-Step 'Tailscale 服务自启'   { Enable-TailscaleService }
Invoke-Step 'Tailscale 登录'      { Connect-Tailscale -AuthKey $TsAuthKey }
Invoke-Step '开启 OpenSSH Server' { Enable-OpenSSHServer }
Invoke-Step '配置 authorized_keys' { Set-AuthorizedKeys }

# ---------------------------------------------------------------------------
# 结尾汇总
# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '==================== 配置汇总 ====================' -ForegroundColor Green
$ts = Get-TailscaleExe
$tsIp = $null
if ($ts) { $tsIp = (& $ts ip -4 2>$null | Select-Object -First 1) }
if ($tsIp) {
    Write-Log "本机 Tailscale IP: $tsIp"
    Write-Log "从 Tailnet 内其它机器登录:  ssh $env:USERNAME@$tsIp"
} else {
    Write-Warn "暂未获取到 Tailscale IP, 请稍后运行 'tailscale ip -4' 查看。"
}

# ---------------------------------------------------------------------------
# Telegram 通知: 免密 SSH 登录已就绪, 告知如何登录
# ---------------------------------------------------------------------------
if ($TgBotToken -and $TgChatId) {
    $tgConfig = @{ Token = $TgBotToken; ChatId = $TgChatId }
    $loginLine = if ($tsIp) {
        "ssh $env:USERNAME@$tsIp"
    } else {
        "ssh $env:USERNAME@<Tailscale-IP>  (稍后运行 tailscale ip -4 查看)"
    }
    $tgMsg = @"
✅ SSH 免密登录已配置完成
主机: $env:COMPUTERNAME (Windows)
用户: $env:USERNAME
Tailscale IP: $(if ($tsIp) { $tsIp } else { '待获取' })

从 Tailnet 内其它机器登录:
$loginLine
"@
    if (Send-Telegram -Config $tgConfig -Text $tgMsg) {
        Write-Log '已发送 Telegram 通知。'
    } else {
        Write-Warn 'Telegram 通知发送失败 (检查脚本内 $TgBotToken/$TgChatId 及网络)。'
    }
}

if ($FailedSteps.Count -gt 0) {
    Write-Host ''
    Write-Warn '以下步骤未成功, 请检查:'
    foreach ($s in $FailedSteps) { Write-Host "    - $s" -ForegroundColor Yellow }
    Write-Host '==================================================' -ForegroundColor Green
    exit 1
}

Write-Host '全部步骤完成。' -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green
