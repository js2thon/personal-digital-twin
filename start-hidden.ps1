# Personal Digital Twin - 静默后台服务
# 由 start-services.lnk 以「隐藏窗口」方式启动：无 cmd 窗口、无 Powershell 窗口。
# 职责：确保 PostgreSQL 5434 与 API 8101 常驻，uvicorn 异常退出后自动重启。
# 日志：.runtime\pg.log、.runtime\uvicorn.out.log、.runtime\uvicorn.err.log
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 本机请求不走企业代理
$env:NO_PROXY = '127.0.0.1,localhost'
$env:no_proxy = '127.0.0.1,localhost'

# 加载 .env
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    Get-Content $envFile | Where-Object { $_ -match '^\s*[^#\s][^=]*=' } | ForEach-Object {
        $k, $v = $_ -split '=', 2
        if ($k) { [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), 'Process') }
    }
}
$env:CELERY_TASK_ALWAYS_EAGER = '1'

$runtime = Join-Path $root '.runtime'
if (-not (Test-Path $runtime)) { New-Item -ItemType Directory -Path $runtime | Out-Null }

function Test-Port([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# ---------- PostgreSQL ----------
if (-not (Test-Port 5434)) {
    $pgExe  = Join-Path $runtime 'pgsql\bin\postgres.exe'
    $pgData = Join-Path $runtime 'pgdata'
    Start-Process -FilePath $pgExe `
        -ArgumentList @('-D', $pgData, '-p', '5434', '-h', '127.0.0.1') `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtime 'pg-stdout.log') `
        -RedirectStandardError  (Join-Path $runtime 'pg-stderr.log')
    for ($i = 0; $i -lt 60 -and -not (Test-Port 5434); $i++) { Start-Sleep -Seconds 2 }
}

# ---------- Python 解释器 ----------
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $py) { throw '未找到 Python 解释器：请先创建 .venv\Scripts\python.exe，或把 python 加入 PATH。' }

$backend = Join-Path $root 'backend'
$uvOut = Join-Path $runtime 'uvicorn.out.log'
$uvErr = Join-Path $runtime 'uvicorn.err.log'

# ---------- API 守护循环 ----------
while ($true) {
    if (-not (Test-Port 8101)) {
        "[{0}] starting uvicorn" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') |
            Out-File -FilePath (Join-Path $runtime 'uvicorn.log') -Append -Encoding utf8
        try {
            $proc = Start-Process -FilePath $py `
                -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8101') `
                -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
                -RedirectStandardOutput $uvOut -RedirectStandardError $uvErr
            Start-Sleep -Seconds 5
            $proc.WaitForExit()
            "[{0}] uvicorn exited with code {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $proc.ExitCode |
                Out-File -FilePath (Join-Path $runtime 'uvicorn.log') -Append -Encoding utf8
        } catch {
            "[{0}] start failed: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $_.Exception.Message |
                Out-File -FilePath (Join-Path $runtime 'uvicorn.log') -Append -Encoding utf8
        }
        Start-Sleep -Seconds 10
    } else {
        Start-Sleep -Seconds 10
    }
}
