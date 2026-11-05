# 启动 ACP Room
#   .\start.ps1            默认 8770
#   .\start.ps1 -Port 9000
param([int]$Port = 8770, [switch]$NoBrowser)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  # 兼容旧布局：仓库被放在另一个项目目录里，venv 在上层
  $sibling = Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe"
  if (Test-Path $sibling) { $py = $sibling } else { $py = "python" }
}

$env:PYTHONIOENCODING = "utf-8"
$env:PORT = "$Port"
Write-Host "ACP Room -> http://127.0.0.1:$Port"

if (-not $NoBrowser) {
  Start-Job -ScriptBlock {
    param($p)
    for ($i = 0; $i -lt 60; $i++) {
      try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$p/api/state" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { Start-Process "http://127.0.0.1:$p"; return }
      } catch { }
      Start-Sleep -Milliseconds 500
    }
  } -ArgumentList $Port | Out-Null
}

& $py server.py --port $Port
