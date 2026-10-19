# 网页构建完成后，在项目终端执行 ./scripts/start_web.ps1。
$ErrorActionPreference = 'Stop' # 启动准备失败就停止，不继续输出误导信息。
$projectRoot = Split-Path $PSScriptRoot -Parent # 无论终端当前在哪，都找到本项目。
$pythonPath = Join-Path $projectRoot '.venv/Scripts/python.exe' # 使用项目虚拟环境。
$indexPath = Join-Path $projectRoot 'frontend/dist/index.html' # 必须先有 React 构建产物。
if (-not (Test-Path -LiteralPath $indexPath)) { throw '请先进入 frontend 目录运行 npm run build。' }
Push-Location $projectRoot # 让 Python 能导入 api 包。
try {
    & $pythonPath -X utf8 -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log # 一个进程与全局分析锁配套，不开放到局域网。
} finally {
    Pop-Location # Ctrl+C 结束后恢复终端原目录。
}
