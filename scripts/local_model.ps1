#Requires -Version 7.0
# 文件作用：将 Ollama 程序、模型、运行配置与日志全部保存在本项目的 D 盘目录；使用 PowerShell 7。
param([ValidateSet('download', 'start', 'pull', 'status')][string]$Action = 'status') # 明确选择下载程序、启动服务、拉取模型或检查状态。
$ErrorActionPreference = 'Stop' # 任何失败立即停止，避免继续使用未校验的文件。
$projectRoot = Split-Path -Parent $PSScriptRoot # scripts 的父目录就是项目根目录，不依赖终端工作目录。
$runtimeRoot = Join-Path $projectRoot '.runtime' # 可执行程序及运行配置位于项目内。
$binaryRoot = Join-Path $runtimeRoot 'ollama' # 独立版解压位置。
$binary = Join-Path $binaryRoot 'ollama.exe' # 不调用 C 盘可能存在的同名程序。
$modelRoot = Join-Path $projectRoot 'models\ollama' # 模型文件固定保存到项目 D 盘目录。
$logRoot = Join-Path $projectRoot 'logs' # 标准输出和错误日志也保存在项目中。
$pidFile = Join-Path $runtimeRoot 'ollama.pid' # 记录本项目启动的进程，避免混用其他 Ollama 实例。
$baseUrl = 'http://127.0.0.1:11434' # 只监听本机，应用不请求云端模型。
$version = '0.33.3' # 固定已核对的官方发布版本。
$expectedHash = '52cb36a62e7e501f61514f60212dec7117b6c098811357585e02fffe32d2fcd7' # 官方 GitHub 发布资产的 SHA-256。

function Get-LocalVersion { # 只读取本机健康状态，不发起任何模型推理。
    try { return Invoke-RestMethod -Uri "$baseUrl/api/version" -TimeoutSec 3 -NoProxy } catch { return $null }
}

if ($Action -eq 'download') { # 一次性下载程序；此步骤本身不下载模型。
    if (Test-Path -LiteralPath $binaryRoot) { throw 'Runtime directory already exists; inspect it instead of overwriting.' }
    $downloadRoot = Join-Path $projectRoot 'work\downloads' # 大安装包也放 D 盘，不使用浏览器默认下载目录。
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    $archive = Join-Path $downloadRoot "ollama-windows-amd64-$version.zip"
    if (-not (Test-Path -LiteralPath $archive)) { # 完整下载包存在时先校验，不自动覆盖。
        $partial = "$archive.part" # 断点文件使用独立后缀，避免被误认为完整下载。
        & curl.exe --fail --location --retry 3 --continue-at - --output $partial "https://github.com/ollama/ollama/releases/download/v$version/ollama-windows-amd64.zip"
        if ($LASTEXITCODE -ne 0) { throw 'Official runtime download failed; partial file retained for resume.' }
        if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw 'Runtime checksum mismatch; archive will not be extracted.' }
        Move-Item -LiteralPath $partial -Destination $archive # 校验完成后才标记为正式下载包；不是递归移动。
    }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw 'Runtime checksum mismatch.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem # 使用系统 ZIP 读取能力，不需要另装解压软件。
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    try { # 检查解压后的绝对路径始终位于目标目录，拒绝路径穿越条目。
        $prefix = [IO.Path]::GetFullPath($binaryRoot) + [IO.Path]::DirectorySeparatorChar
        foreach ($entry in $zip.Entries) {
            $destination = [IO.Path]::GetFullPath((Join-Path $binaryRoot $entry.FullName))
            if (-not $destination.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe archive entry.' }
        }
    } finally { $zip.Dispose() }
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $binaryRoot) # 目标目录必须尚未存在，不覆盖旧安装。
    Write-Output "Verified Ollama $version extracted to $binaryRoot"
    exit 0
}

if ($Action -eq 'status') { # 未安装时也能运行，给出明确下一步。
    [PSCustomObject]@{BinaryExists=(Test-Path -LiteralPath $binary); ModelDirectory=$modelRoot; Server=(Get-LocalVersion)} | ConvertTo-Json -Depth 3
    exit 0
}
if (-not (Test-Path -LiteralPath $binary)) { throw 'Run scripts/local_model.ps1 download first.' }
$oldEnvironment = @{} # 环境变量只作用于本次脚本及子进程，结束后恢复，不改系统或用户永久配置。
$settings = @{
    OLLAMA_HOST='127.0.0.1:11434'; OLLAMA_MODELS=$modelRoot; OLLAMA_NO_CLOUD='1'
    OLLAMA_NUM_PARALLEL='1'; OLLAMA_MAX_LOADED_MODELS='1'; OLLAMA_CONTEXT_LENGTH='4096'
    USERPROFILE=(Join-Path $runtimeRoot 'profile') # 将服务自己的用户配置也重定向至 D 盘；不修改 PowerShell 的 HOME。
    LOCALAPPDATA=(Join-Path $runtimeRoot 'localappdata')
    APPDATA=(Join-Path $runtimeRoot 'appdata')
    TEMP=(Join-Path $runtimeRoot 'temp'); TMP=(Join-Path $runtimeRoot 'temp')
}
foreach ($folder in @($modelRoot, $logRoot, $settings.USERPROFILE, $settings.LOCALAPPDATA, $settings.APPDATA, $settings.TEMP)) { New-Item -ItemType Directory -Path $folder -Force | Out-Null }
try {
    foreach ($key in $settings.Keys) { # 使用进程级环境，不触碰系统环境变量。
        $oldEnvironment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $settings[$key], 'Process')
    }
    if ($Action -eq 'start') {
        if (Get-LocalVersion) { throw 'Port 11434 already serves Ollama; inspect the existing process instead of starting another.' }
        $process = Start-Process -FilePath $binary -ArgumentList 'serve' -WorkingDirectory $binaryRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logRoot 'ollama.stdout.log') -RedirectStandardError (Join-Path $logRoot 'ollama.stderr.log')
        [IO.File]::WriteAllText($pidFile, [string]$process.Id) # 只记录自己的新进程，不停止其他服务。
        Write-Output "Started project Ollama PID $($process.Id); models: $modelRoot"
    } else {
        if (-not (Get-LocalVersion)) { throw 'Project Ollama is not running; start it first.' }
        if (-not (Test-Path -LiteralPath $pidFile)) { throw 'Missing project service PID; refusing an unverified model download location.' }
        $owned = Get-Process -Id ([int](Get-Content -LiteralPath $pidFile -Raw))
        if ($owned.Path -ne $binary) { throw 'Ollama process is not the project runtime.' }
        & $binary pull 'qwen3:8b' # 约 5.2 GB 量化权重；真正的下载由已固定 OLLAMA_MODELS 的服务完成。
        if ($LASTEXITCODE -ne 0) { throw 'Model pull failed; rerun pull to resume.' }
    }
} finally {
    foreach ($key in $oldEnvironment.Keys) { [Environment]::SetEnvironmentVariable($key, $oldEnvironment[$key], 'Process') }
}
