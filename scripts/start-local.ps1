$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
Set-Location $RootDir

$VenvDir = Join-Path $RootDir ".venv"
$Python = Get-Command python -ErrorAction Stop
if (-not (Test-Path $VenvDir)) {
    & $Python.Source -m venv $VenvDir
}

$VenvPython = Join-Path $VenvDir "Scripts/python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e (Join-Path $RootDir "backend")

$EnvFile = Join-Path $RootDir ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $RootDir ".env.example") $EnvFile
    Write-Host "已生成 .env，请确认 DATABASE_URL 和模型服务配置后重新运行。"
    exit 1
}

$Port = if ($env:API_PORT) { $env:API_PORT } else { "8100" }
Write-Host "启动 Personal Digital Twin：http://127.0.0.1:$Port"
Set-Location (Join-Path $RootDir "backend")
& $VenvPython -m uvicorn app.main:app --host 127.0.0.1 --port $Port
