param([Alias("q")][switch]$Quick)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "未检测到 uv，正在安装..." -ForegroundColor Cyan
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv 安装完成后仍不可用，请重新打开终端再运行本脚本。"
}

function Set-EnvValue([string]$Path, [string]$Key, [string]$Value) {
    $lines = [System.Collections.Generic.List[string]](Get-Content -LiteralPath $Path -Encoding UTF8)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$([regex]::Escape($Key))=") {
            $lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    [IO.File]::WriteAllLines((Resolve-Path $Path), $lines, [Text.UTF8Encoding]::new($false))
}

if (-not (Test-Path -LiteralPath ".env.prod")) {
    Copy-Item -LiteralPath ".env.prod.example" -Destination ".env.prod"
    Write-Host "已从模板创建 .env.prod" -ForegroundColor Cyan
    if (-not $Quick) {
        $superuser = Read-Host "超级管理员 QQ"
        $wsUrl = Read-Host "OneBot WebSocket 地址 [ws://127.0.0.1:3001/onebot/v11/ws]"
        if ([string]::IsNullOrWhiteSpace($wsUrl)) { $wsUrl = "ws://127.0.0.1:3001/onebot/v11/ws" }
        $accessToken = Read-Host "OneBot Access Token（可留空）"
        Set-EnvValue ".env.prod" "SUPERUSERS" "[`"$superuser`"]"
        Set-EnvValue ".env.prod" "ADMIN_QQS" $superuser
        Set-EnvValue ".env.prod" "ONEBOT_WS_URLS" "[`"$wsUrl`"]"
        Set-EnvValue ".env.prod" "ONEBOT_ACCESS_TOKEN" $accessToken
        Write-Host "必填配置已写入 .env.prod，其余配置可稍后修改。" -ForegroundColor Green
    }
}

Write-Host "正在同步依赖..." -ForegroundColor Cyan
uv sync --locked
Write-Host "正在启动 Bili Group Gatekeeper..." -ForegroundColor Green
uv run python bot.py
