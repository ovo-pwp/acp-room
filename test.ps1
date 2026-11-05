# 一键跑测试。用法：.\test.ps1
# 为什么要有这个文件：venv 在上一级，直接 `pytest` 会用系统 Python（没有依赖）。
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$py = Join-Path (Split-Path $PSScriptRoot -Parent) ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$env:PYTHONIOENCODING = "utf-8"
Write-Host "pytest <- $py" -ForegroundColor DarkGray
& $py -m pytest @args
exit $LASTEXITCODE
