"""下载固定版本的官方训练基座，并逐文件验证完整性；不启动训练。"""

import hashlib  # 检查官方 Git LFS SHA-256 或普通 Git blob 标识。
import json  # 保存下载来源与验证结果。
import os  # 在导入 Hugging Face 之前指定 D 盘缓存。
import time  # 下载失败时短暂退避，保留已完成分段。
import urllib.request  # 对大文件使用有限长度的 HTTP Range 请求。
from concurrent.futures import ThreadPoolExecutor, as_completed  # 并行下载互不重叠的片段。
from pathlib import Path  # 所有模型与缓存放入项目。

ROOT = Path(__file__).resolve().parents[1]
os.environ['HF_HOME'] = str(ROOT / '.runtime/training-cache/huggingface')
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['HF_HUB_DISABLE_XET'] = '1'
from huggingface_hub import HfApi  # 从官方获取固定版本的大小及校验值。
import yaml  # 读取明确固定的模型版本。


def ranged_download(url, path, size):
    """分段续传，避免单个数 GB 响应中途停滞；完成后仍需官方 SHA 校验。"""
    part = path.with_suffix(path.suffix + '.part')
    journal = path.with_suffix(path.suffix + '.ranges.json')
    segment = 8 * 1024 * 1024  # 每个请求最多 8MB，读超时后可单独重试。
    count = (size + segment - 1) // segment
    done = set(json.loads(journal.read_text())['done']) if journal.exists() else set()
    if not part.exists():
        assert not done, '进度存在但分段文件丢失。'
        with part.open('wb') as stream:
            stream.truncate(size)  # 只在新建文件时预分配；后续运行不会截断已下载内容。
    assert part.stat().st_size == size

    def chunk_download(index):
        start, end = index * segment, min((index + 1) * segment, size) - 1
        for attempt in range(6):
            try:
                request = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}'})
                with urllib.request.urlopen(request, timeout=25) as response:
                    assert response.status == 206 and response.headers.get('Content-Range') == f'bytes {start}-{end}/{size}'
                    content = response.read(end - start + 2)
                assert len(content) == end - start + 1
                with part.open('r+b') as stream:  # 每个线程独立文件句柄，只写自己的非重叠范围。
                    stream.seek(start)
                    stream.write(content)
                return index
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(attempt + 1, 5))
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(chunk_download, index) for index in range(count) if index not in done]
        for job in as_completed(jobs):
            done.add(job.result())
            temporary = journal.with_suffix('.tmp')
            temporary.write_text(json.dumps({'size': size, 'segment': segment, 'done': sorted(done)}), encoding='utf-8')
            temporary.replace(journal)
            if len(done) % 16 == 0 or len(done) == count:
                print(f'{path.name}: {len(done)}/{count} 分段（{len(done) / count:.1%}）', flush=True)
    assert len(done) == count
    part.replace(path)  # 全部片段完成才使用正式文件名。


def main():
    config = yaml.safe_load((ROOT / 'config/lora_training.yaml').read_text(encoding='utf-8'))
    destination = ROOT / config['model_directory']
    destination.mkdir(parents=True, exist_ok=True)
    info = HfApi(token=False).model_info(config['model_id'], revision=config['model_revision'], files_metadata=True)
    assert info.sha == config['model_revision']
    allowed = [item for item in info.siblings if item.rfilename.endswith(('.safetensors', '.json', '.txt', '.jinja', '.md')) or item.rfilename in ('LICENSE', '.gitattributes')]
    print(f'下载官方 {config["model_id"]}，版本 {info.sha}，目录 {destination}', flush=True)
    # 先下载 tokenizer 与配置，让分词检查可在权重下载期间进行。
    for item in sorted(allowed, key=lambda entry: entry.size):
        path = destination / item.rfilename
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size == item.size:
            continue  # 下方仍会对已有文件做完整的哈希校验。
        url = f'https://huggingface.co/{config["model_id"]}/resolve/{info.sha}/{item.rfilename}'
        if item.size > 32 * 1024 * 1024:
            ranged_download(url, path, item.size)
        else:
            with urllib.request.urlopen(url, timeout=30) as response:
                content = response.read()
            assert len(content) == item.size
            path.write_bytes(content)
    verified = []
    for item in allowed:
        path = destination / item.rfilename
        assert path.is_file() and path.stat().st_size == item.size, item.rfilename
        sha256 = hashlib.sha256()
        git_hash = hashlib.sha1(f'blob {item.size}\0'.encode())
        with path.open('rb') as stream:
            while chunk := stream.read(16 * 1024 * 1024):
                sha256.update(chunk)
                git_hash.update(chunk)
        if item.lfs:
            assert sha256.hexdigest() == item.lfs.sha256, item.rfilename
        else:
            assert git_hash.hexdigest() == item.blob_id, item.rfilename
        verified.append({'file': item.rfilename, 'bytes': item.size, 'sha256': sha256.hexdigest()})
        print(f'校验通过：{item.rfilename}', flush=True)
    (destination / 'download_verified.json').write_text(json.dumps({'model_id': config['model_id'], 'revision': info.sha, 'verified': verified}, ensure_ascii=False, indent=2), encoding='utf-8')
    print('训练基座下载与完整性校验完成。', flush=True)


if __name__ == '__main__':
    main()
