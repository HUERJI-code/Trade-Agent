#Requires -Version 7.0
# 文件作用：在隐藏后台进程中启动正式训练；日志与 PID 都保存在项目内。
param([switch]$Resume)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'training_environment.ps1') # 应用 D 盘缓存设置。
$rootDirectory = Split-Path -Parent $PSScriptRoot
$runDirectory = Join-Path $rootDirectory 'outputs/training/answer_style_qwen3_8b_sft_v1'
$pidPath = Join-Path $runDirectory 'training.pid'
if (Test-Path -LiteralPath $pidPath) {
    $existing = Get-Process -Id ([int](Get-Content -LiteralPath $pidPath -Raw)) -ErrorAction SilentlyContinue
    if ($existing) { throw '记录的训练进程仍存在，请先核对状态，不重复启动。' }
}
$smokePath = Join-Path $runDirectory 'smoke_report.json'
if (-not (Test-Path -LiteralPath $smokePath)) { throw '请先完成 --mode smoke 检查。' }
if ((Get-Content -LiteralPath $smokePath -Raw | ConvertFrom-Json).status -ne 'passed') { throw '显存或梯度检查未通过。' }
$arguments = '-X utf8 scripts/train_answer_style.py --mode train'
if ($Resume) { $arguments += ' --resume' }
$process = Start-Process -FilePath (Join-Path $rootDirectory '.venv-training/Scripts/python.exe') -ArgumentList $arguments -WorkingDirectory $rootDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runDirectory 'train.stdout.log') -RedirectStandardError (Join-Path $runDirectory 'train.stderr.log')
[IO.File]::WriteAllText($pidPath, [string]$process.Id)
Write-Output "训练进程已启动，PID=$($process.Id)，状态文件：$runDirectory/status.json"
