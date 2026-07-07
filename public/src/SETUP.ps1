<#
    Verify.ps1 — SSH + Tailscale 配置验证 (Windows 原生)

    功能: 逐项检查 SETUP.ps1 的配置成果, 输出一份美观、清晰的验证报告。
      1. Tailscale 是否安装 / 服务是否运行 / 是否已登录 / 有无 100.x IP
      2. OpenSSH Server 是否安装 / sshd 服务是否运行 / 防火墙规则 / 监听 22
      3. authorized_keys (管理员组约定文件或用户文件) 是否含全部硬编码公钥
      4. Telegram 配置是否就绪 (可选 -Ping 实测发送)

    只读检查, 不改动系统。本脚本独立自足, 配置直接硬编码于下方 (与 SETUP.ps1 保持一致)。

    用法:
      powershell -ExecutionPolicy Bypass -File Verify.ps1
      powershell -ExecutionPolicy Bypass -File Verify.ps1 -Ping
      powershell -ExecutionPolicy Bypass -File Verify.ps1 -NoColor
#>

param(
    [switch]$Ping,
    [switch]$NoColor
)

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 硬编码配置 (与 SETUP.ps1 保持一致; 本脚本独立, 不依赖 SETUP.ps1)
# ---------------------------------------------------------------------------
$TsAuthKey = 'tskey-auth-kiLmAL1dzY11CNTRL-8kBw3rQUum5U8wepNaB6n5KzhgmcHBmkK'  #有效期:2026-10-05/Tags:fish
$SshPublicKeys = @(
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHCru1fsEf+V1Dp6etLeB28qkMLDdd/CO2cdYN2takSB default-mac',
    'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINnCe0w8jneYzlCU3ozapFNqQX138WaNau22kuhd6wA+ admin-wsl'
)
$TgBotToken = '7724790582:AAE2Jish4jeQ_uheEuTgAeKIt1um0ml4-HM'
$TgChatId   = '7765138435'

$UseColor = -not $NoColor
if ($env:NO_COLOR) { $UseColor = $false }

# ---------------------------------------------------------------------------
# 外观: 颜色 / 图标 / 排版
# ---------------------------------------------------------------------------
$Width = 64
$script:PassN = 0
$script:FailN = 0
$script:WarnN = 0

function Get-DisplayWidth {
    param([string]$Text)
    # 去掉 ANSI 转义
    $plain = [regex]::Replace($Text, "`e\[[0-9;]*m", '')
    $w = 0
    foreach ($ch in $plain.ToCharArray()) {
        $code = [int][char]$ch
        # ✔ (0x2714) / ✗ (0x2717) 等 dingbat 终端渲染宽度为 1
        if ($code -eq 0x2714 -or $code -eq 0x2717) { $w += 1; continue }
        # CJK / 全角区间记 2 列
        if ( ($code -ge 0x1100 -and $code -le 0x115F) -or
             ($code -ge 0x2E80 -and $code -le 0xA4CF) -or
             ($code -ge 0xAC00 -and $code -le 0xD7A3) -or
             ($code -ge 0xF900 -and $code -le 0xFAFF) -or
             ($code -ge 0xFF00 -and $code -le 0xFF60) -or
             ($code -ge 0xFFE0 -and $code -le 0xFFE6) ) {
            $w += 2
        } else {
            $w += 1
        }
    }
    return $w
}

function Write-Part {
    param([string]$Text, [string]$Color, [switch]$NoNewline)
    if ($UseColor -and $Color) {
        Write-Host $Text -ForegroundColor $Color -NoNewline:$NoNewline
    } else {
        Write-Host $Text -NoNewline:$NoNewline
    }
}

function Write-Rule {
    param([string]$Left, [string]$Right)
    $line = $Left + ('─' * $Width) + $Right
    Write-Part $line 'DarkGray'
}

function Write-BarText {
    param([string]$Text)
    $dw = Get-DisplayWidth $Text
    $pad = $Width - 1 - $dw
    if ($pad -lt 0) { $pad = 0 }
    Write-Part '│' 'DarkGray' -NoNewline
    Write-Host (' ' + $Text + (' ' * $pad)) -NoNewline
    Write-Part '│' 'DarkGray'
}

function Write-Banner {
    param([string]$Title, [string]$Sub)
    Write-Host ''
    Write-Rule '╭' '╮'
    Write-BarText $Title
    if ($Sub) { Write-BarText $Sub }
    Write-Rule '╰' '╯'
    Write-Host ''
}

function Write-Section {
    param([string]$Title)
    Write-Host ''
    Write-Part ('  ' + $Title) 'Blue'
    Write-Rule '├' '┤'
}

# 一条检查结果: Status = ok/fail/warn/info
function Write-Check {
    param(
        [ValidateSet('ok', 'fail', 'warn', 'info')]
        [string]$Status,
        [string]$Label,
        [string]$Detail = ''
    )
    switch ($Status) {
        'ok'   { $icon = '✔'; $color = 'Green';  $script:PassN++ }
        'fail' { $icon = '✗'; $color = 'Red';    $script:FailN++ }
        'warn' { $icon = '!'; $color = 'Yellow'; $script:WarnN++ }
        'info' { $icon = '·'; $color = 'Cyan' }
    }
    $dw = Get-DisplayWidth $Label
    $pad = 26 - $dw
    if ($pad -lt 0) { $pad = 0 }
    Write-Host '  ' -NoNewline
    Write-Part $icon $color -NoNewline
    Write-Host ('  ' + $Label + (' ' * $pad)) -NoNewline
    if ($Detail) {
        Write-Part $Detail 'DarkGray'
    } else {
        Write-Host ''
    }
}

# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------
function Get-TailscaleExe {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidate = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $candidate) { return $candidate }
    return $null
}

function Test-Tailscale {
    Write-Section 'Tailscale'
    $ts = Get-TailscaleExe
    if (-not $ts) {
        Write-Check fail 'Tailscale 已安装' '未找到 tailscale.exe'
        $script:TsIp = ''
        return
    }
    Write-Check ok 'Tailscale 已安装' $ts

    $svc = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -eq 'Running') {
            Write-Check ok 'Tailscale 服务' "运行中 (启动类型 $($svc.StartType))"
        } else {
            Write-Check warn 'Tailscale 服务' "状态 $($svc.Status)"
        }
    } else {
        Write-Check warn 'Tailscale 服务' '未找到服务'
    }

    $statusOut = (& $ts status) 2>$null | Out-String
    if ($statusOut -match 'Logged out') {
        Write-Check fail 'Tailscale 登录状态' '已注销 (Logged out)'
    } elseif ($statusOut.Trim()) {
        Write-Check ok 'Tailscale 登录状态' '已登录'
    } else {
        Write-Check warn 'Tailscale 登录状态' '无法确定 (可能仍在连接)'
    }

    $script:TsIp = (& $ts ip -4 2>$null | Select-Object -First 1)
    if ($script:TsIp) {
        Write-Check ok '本机 Tailscale IP' $script:TsIp
    } else {
        Write-Check warn '本机 Tailscale IP' '暂未获取 (稍后重试 tailscale ip -4)'
    }
}

function Test-OpenSSH {
    Write-Section 'OpenSSH Server'
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue
    if ($cap -and $cap.State -eq 'Installed') {
        Write-Check ok 'OpenSSH Server 已安装' ''
    } elseif ($cap) {
        Write-Check fail 'OpenSSH Server 已安装' "状态 $($cap.State)"
    } else {
        Write-Check warn 'OpenSSH Server 已安装' '无法查询能力状态'
    }

    $sshd = Get-Service -Name sshd -ErrorAction SilentlyContinue
    if ($sshd) {
        if ($sshd.Status -eq 'Running') {
            Write-Check ok 'sshd 服务' "运行中 (启动类型 $($sshd.StartType))"
        } else {
            Write-Check fail 'sshd 服务' "状态 $($sshd.Status)"
        }
        if ($sshd.StartType -ne 'Automatic') {
            Write-Check warn 'sshd 开机自启' "当前 $($sshd.StartType) (建议 Automatic)"
        }
    } else {
        Write-Check fail 'sshd 服务' '未找到服务'
    }

    $rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    if ($rule -and $rule.Enabled -eq 'True') {
        Write-Check ok '防火墙入站 (TCP 22)' '已启用'
    } elseif ($rule) {
        Write-Check warn '防火墙入站 (TCP 22)' '规则存在但未启用'
    } else {
        Write-Check warn '防火墙入站 (TCP 22)' '未找到规则'
    }

    $listening = Get-NetTCPConnection -State Listen -LocalPort 22 -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Check ok '监听端口 22' '已监听'
    } else {
        Write-Check warn '监听端口 22' '未检测到 (服务可能未起或用其它端口)'
    }
}

function Test-AuthorizedKeys {
    Write-Section '公钥 authorized_keys'
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        $authFile = Join-Path $env:ProgramData 'ssh\administrators_authorized_keys'
        Write-Check info '目标文件' 'administrators_authorized_keys (管理员组)'
    } else {
        $authFile = Join-Path $env:USERPROFILE '.ssh\authorized_keys'
        Write-Check info '目标文件' '%USERPROFILE%\.ssh\authorized_keys'
    }

    if (-not (Test-Path -LiteralPath $authFile)) {
        Write-Check fail 'authorized_keys 文件' "不存在: $authFile"
        return
    }
    Write-Check ok 'authorized_keys 文件' '存在'

    # 管理员文件 ACL 检查: 应仅 SYSTEM + Administrators
    if ($isAdmin) {
        $acl = Get-Acl -LiteralPath $authFile
        $ids = $acl.Access | ForEach-Object { $_.IdentityReference.Value }
        $unexpected = $ids | Where-Object {
            $_ -notmatch 'SYSTEM' -and $_ -notmatch 'Administrators'
        }
        if ($unexpected) {
            Write-Check warn 'ACL 权限收紧' ("含额外主体: " + ($unexpected -join ', '))
        } else {
            Write-Check ok 'ACL 权限收紧' '仅 SYSTEM + Administrators'
        }
    }

    $existing = @(Get-Content -LiteralPath $authFile -ErrorAction SilentlyContinue)
    if ($script:SshPublicKeys.Count -eq 0) {
        Write-Check warn '硬编码公钥' '配置中未定义任何公钥'
        return
    }
    foreach ($key in $script:SshPublicKeys) {
        $k = $key.Trim()
        if (-not $k) { continue }
        $parts = $k -split '\s+'
        $label = $parts[-1]
        if ($label -like 'ssh-*' -or $label -like 'AAAA*') {
            $label = $k.Substring(0, [Math]::Min(24, $k.Length)) + '…'
        }
        if ($existing -contains $k) {
            Write-Check ok "公钥: $label" '已写入'
        } else {
            Write-Check fail "公钥: $label" '缺失'
        }
    }
}

function Test-Telegram {
    Write-Section 'Telegram 通知'
    if (-not $script:TgBotToken -or -not $script:TgChatId) {
        Write-Check info 'Telegram 配置' '未配置 (通知功能关闭)'
        return
    }
    $masked = ($script:TgBotToken -split ':')[0] + ':••••••'
    Write-Check ok 'Bot Token' $masked
    Write-Check ok 'Chat ID' $script:TgChatId

    if ($Ping) {
        try {
            $uri = "https://api.telegram.org/bot$($script:TgBotToken)/sendMessage"
            $body = @{
                chat_id                  = $script:TgChatId
                text                     = "🔎 Verify.ps1 测试消息 — 来自 $env:COMPUTERNAME"
                disable_web_page_preview = $true
            }
            $resp = Invoke-RestMethod -Uri $uri -Method Post -Body $body -TimeoutSec 15
            if ($resp.ok) {
                Write-Check ok '实测发送' '已成功投递'
            } else {
                Write-Check fail '实测发送' 'API 返回 ok=false'
            }
        } catch {
            Write-Check fail '实测发送' "失败: $($_.Exception.Message)"
        }
    } else {
        Write-Check info '实测发送' '未执行 (加 -Ping 可实测)'
    }
}

function Write-Summary {
    Write-Host ''
    Write-Rule '╭' '╮'
    if ($script:FailN -eq 0 -and $script:WarnN -eq 0) {
        Write-BarText '全部通过 · 配置就绪'
    } elseif ($script:FailN -eq 0) {
        Write-BarText '基本就绪 · 有可忽略的提醒'
    } else {
        Write-BarText '存在失败项 · 请按上方排查'
    }
    Write-BarText ("✔ 通过 $($script:PassN)    ! 提醒 $($script:WarnN)    ✗ 失败 $($script:FailN)")
    if ($script:TsIp) {
        Write-BarText ("登录命令: ssh $env:USERNAME@$($script:TsIp)")
    }
    Write-Rule '╰' '╯'
    Write-Host ''
    if ($script:FailN -gt 0) { exit 1 }
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
Write-Banner 'SSH + Tailscale 配置验证' "平台 Windows · 主机 $env:COMPUTERNAME"

if (-not (Import-HardcodedConfig)) {
    Write-Check warn '读取硬编码配置' "无法从 $SetupFile 提取, 部分检查将跳过"
}

Test-Tailscale
Test-OpenSSH
Test-AuthorizedKeys
Test-Telegram
Write-Summary
