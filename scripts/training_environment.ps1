# 文件作用：把训练使用的缓存与临时文件限定在本项目；仅设置当前进程环境。
$trainingRoot = Split-Path -Parent $PSScriptRoot # 不依赖终端当前目录。
$trainingCache = Join-Path $trainingRoot '.runtime/training-cache'
$trainingFolders = @{
    HF_HOME = (Join-Path $trainingCache 'huggingface')
    HF_HUB_CACHE = (Join-Path $trainingCache 'huggingface/hub')
    HF_DATASETS_CACHE = (Join-Path $trainingCache 'datasets')
    HF_XET_CACHE = (Join-Path $trainingCache 'huggingface/xet')
    TORCH_HOME = (Join-Path $trainingCache 'torch')
    TORCHINDUCTOR_CACHE_DIR = (Join-Path $trainingCache 'torchinductor')
    CUDA_CACHE_PATH = (Join-Path $trainingCache 'cuda')
    XDG_CACHE_HOME = (Join-Path $trainingCache 'xdg')
    PIP_CACHE_DIR = (Join-Path $trainingCache 'pip')
    TEMP = (Join-Path $trainingCache 'temp')
    TMP = (Join-Path $trainingCache 'temp')
}
foreach ($entry in $trainingFolders.GetEnumerator()) {
    New-Item -ItemType Directory -Path $entry.Value -Force | Out-Null # 所有目录都在 D 盘。
    [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process') # 不修改用户或系统永久设置。
}
$env:HF_HUB_DISABLE_TELEMETRY = '1' # 不发送使用统计。
$env:HF_HUB_DISABLE_IMPLICIT_TOKEN = '1' # 公共模型下载不读取用户登录凭据。
$env:HF_HUB_DISABLE_XET = '1' # 使用普通 HTTPS 下载，避免额外的 Xet 分块缓存。
$env:TOKENIZERS_PARALLELISM = 'false' # 在 Windows 上避免无必要的分词线程竞争。
$env:PYTHONUTF8 = '1' # 中文日志和数据都按 UTF-8 处理。
$env:PYTHONUNBUFFERED = '1' # 及时写出训练进度。
