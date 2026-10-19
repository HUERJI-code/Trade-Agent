# 将已转换的 F16 GGUF 量化为 Q4_K_M；只在 D 盘项目内生成文件。
$ErrorActionPreference = 'Stop'
$quantRoot = Split-Path -Parent $PSScriptRoot
$inputModel = Join-Path $quantRoot 'models/gguf/trade-agent-qwen3-8b-lora-v1-f16.gguf'
$outputModel = Join-Path $quantRoot 'models/gguf/trade-agent-qwen3-8b-lora-v1-Q4_K_M.gguf'
$temporaryModel = "$outputModel.part"
& (Join-Path $quantRoot '.runtime/llama-b6500/llama-quantize.exe') $inputModel $temporaryModel Q4_K_M 4 *> (Join-Path $quantRoot 'logs/gguf_quantize.log')
if ($LASTEXITCODE -ne 0) { throw 'GGUF 量化失败，请检查日志。' }
[IO.File]::Move($temporaryModel, $outputModel, $true)
$report = @{ status='quantized'; quantization='Q4_K_M'; llama_cpp='b6500'; input=$inputModel; output=$outputModel; sha256=(Get-FileHash -LiteralPath $outputModel -Algorithm SHA256).Hash.ToLower() }
$report | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $quantRoot 'models/gguf/quantization_report.json') -Encoding utf8
Write-Output 'Q4_K_M 量化完成。'
