#Requires -Version 7.0
# 使用已经合并和量化的 GGUF 创建本机 Ollama 模型，不上传模型。
$ErrorActionPreference = 'Stop'
$deployRoot = Split-Path -Parent $PSScriptRoot
$modelDirectory = Join-Path $deployRoot 'models/gguf'
$quantizedPath = Join-Path $modelDirectory 'trade-agent-qwen3-8b-lora-v1-Q4_K_M.gguf'
$ollamaBinary = Join-Path $deployRoot '.runtime/ollama/ollama.exe'
if (-not (Test-Path -LiteralPath (Join-Path $modelDirectory 'quantization_report.json'))) { throw '先完成 GGUF 转换与量化。' }
if (-not (Test-Path -LiteralPath $quantizedPath)) { throw '部署副本已清理；现有 Ollama 模型可直接启动。重新导入前请恢复或重新生成 Q4_K_M GGUF。' }
$ownedPid = [int](Get-Content -LiteralPath (Join-Path $deployRoot '.runtime/ollama.pid') -Raw)
if ((Get-Process -Id $ownedPid).Path -ne $ollamaBinary) { throw '当前服务不是已记录的项目 Ollama。' }
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = Join-Path $deployRoot 'models/ollama'
$env:OLLAMA_NO_CLOUD = '1'
$env:USERPROFILE = Join-Path $deployRoot '.runtime/profile'
$env:LOCALAPPDATA = Join-Path $deployRoot '.runtime/localappdata'
$env:APPDATA = Join-Path $deployRoot '.runtime/appdata'
$env:TEMP = Join-Path $deployRoot '.runtime/temp'
$env:TMP = $env:TEMP
$templateArchive = Join-Path $deployRoot 'outputs/model_provenance/base_ollama_show.json' # 清理原模型后，使用已归档的原始聊天模板。
$baseSettings = if (Test-Path -LiteralPath $templateArchive) {
    Get-Content -LiteralPath $templateArchive -Raw | ConvertFrom-Json
} else {
    Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/show' -Method Post -ContentType 'application/json' -Body '{"model":"qwen3:8b"}'
}
if ([string]::IsNullOrWhiteSpace($baseSettings.template)) { throw '缺少已验证的 Qwen3 聊天模板。' }
$modelFilePath = Join-Path $modelDirectory 'Modelfile'
$modelSource = $quantizedPath.Replace('\', '/')
$modelFile = @"
FROM "$modelSource"
TEMPLATE """$($baseSettings.template)"""
PARAMETER num_ctx 4096
PARAMETER temperature 0
PARAMETER seed 42
PARAMETER stop "<|im_start|>"
PARAMETER stop "<|im_end|>"
"@
[IO.File]::WriteAllText($modelFilePath, $modelFile, [Text.UTF8Encoding]::new($false))
& $ollamaBinary create 'trade-agent-qwen3:8b-lora-v1' -f $modelFilePath *> (Join-Path $deployRoot 'logs/ollama_import.log')
if ($LASTEXITCODE -ne 0) { throw 'Ollama 导入未完成，请检查日志。' }
Write-Output '候选模型已导入；实际评估后再切换应用配置。'
