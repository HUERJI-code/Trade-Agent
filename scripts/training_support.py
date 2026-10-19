"""训练的路径设置、数据检查与日志；训练主流程在 train_answer_style.py。"""

import hashlib  # 对比数据指纹，防止训练输入被意外更改。
import json  # 读写报告。
import math  # 检查损失是否为有限数值。
import os  # 指定缓存环境变量。
import time  # 记录耗时。
from pathlib import Path  # 统一使用项目内路径。

ROOT = Path(__file__).resolve().parents[1]  # scripts 的上级就是项目根目录。


def setup_environment():
    """在导入训练框架之前，把模型缓存和临时目录设到 D 盘项目。"""
    folders = {'HF_HOME': 'huggingface', 'HF_HUB_CACHE': 'huggingface/hub',
               'HF_DATASETS_CACHE': 'datasets', 'HF_XET_CACHE': 'xet', 'TORCH_HOME': 'torch',
               'TORCHINDUCTOR_CACHE_DIR': 'torchinductor', 'CUDA_CACHE_PATH': 'cuda',
               'XDG_CACHE_HOME': 'xdg', 'PIP_CACHE_DIR': 'pip', 'TEMP': 'temp', 'TMP': 'temp'}
    for variable, folder in folders.items():
        path = ROOT / '.runtime/training-cache' / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[variable] = str(path)
    os.environ.update(HF_HUB_DISABLE_TELEMETRY='1', HF_HUB_DISABLE_IMPLICIT_TOKEN='1',
                      HF_HUB_DISABLE_XET='1', TOKENIZERS_PARALLELISM='false')


setup_environment()  # 下面导入库时，缓存位置已经生效。
import torch  # 读取 CUDA 显存统计。
from transformers import TrainerCallback  # 使用标准回调记录日志，不改训练算法。
from trl.chat_template_utils import get_training_chat_template  # 与 SFTTrainer 相同的官方训练模板。


def write_json(path, value):
    """先写临时文件再替换，避免读到写了一半的状态。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    for attempt in range(20):  # Windows 上状态查看器可能短暂持有不允许替换的文件句柄。
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise  # 持续权限错误仍需报告，不伪装为写入成功。
            time.sleep(0.1)  # 短暂等待读句柄释放；不改变训练参数或权重。


def answer_body(text):
    """模板会生成空思考标记；检查时剥去该固定前缀，只比较回答正文。"""
    return text.strip().removeprefix('<think>\n\n</think>\n\n').strip()


def check_dataset(tokenizer, datasets, directory, max_length):
    """逐条验证长度和 assistant 掩码，禁止悄悄截断答案。"""
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    template = get_training_chat_template(tokenizer)
    report = {}
    for split, records in datasets.items():
        path = directory / f'{split}.json'
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest['report']['files_sha256'][path.name]
        lengths = []
        for index, record in enumerate(records):
            messages = record['messages']
            assert [m['role'] for m in messages] == ['system', 'user', 'assistant']
            tokens = tokenizer.apply_chat_template(messages, chat_template=template, tokenize=True,
                return_dict=True, return_assistant_tokens_mask=True, enable_thinking=False)
            ids, mask = tokens['input_ids'], tokens['assistant_masks']
            assert len(ids) == len(mask) and any(mask)
            assert len(ids) <= max_length, f'{split}:{index + 1} 超过最大长度，不允许静默截断。'
            labels = [token for token, selected in zip(ids, mask) if selected]
            assert tokenizer.eos_token_id in labels, '回答必须包含结束符。'
            assert answer_body(tokenizer.decode(labels, skip_special_tokens=True)) == messages[-1]['content'].strip(), f'{split}:{index + 1} 回答掩码不匹配。'
            lengths.append(len(ids))
        report[split] = {'count': len(lengths), 'min_tokens': min(lengths), 'max_tokens': max(lengths),
                         'longest_index': lengths.index(max(lengths)), 'mean_tokens': sum(lengths) / len(lengths),
                         'truncated': 0, 'assistant_masks_verified': True}
    return report, manifest


def check_trainer_labels(trainer, original):
    """再核验 SFTTrainer 实际整理后的 labels，而非只检查预处理假设。"""
    tokenizer = trainer.processing_class
    assert len(trainer.train_dataset) == len(original)
    for processed, raw in zip(trainer.train_dataset, original):
        batch = trainer.data_collator([processed])
        labels = batch['labels'][0]
        selected = labels[labels != -100].tolist()  # -100 的位置不计算监督损失。
        assert tokenizer.eos_token_id in selected
        assert answer_body(tokenizer.decode(selected, skip_special_tokens=True)) == raw['messages'][-1]['content'].strip()


class ProgressWriter(TrainerCallback):
    """将标准训练器提供的进度写到项目目录，便于查看后台训练。"""

    def __init__(self, directory):
        self.directory, self.started = directory, time.time()

    def on_log(self, args, state, control, logs=None, **kwargs):
        metrics = logs or {}
        for key in ('loss', 'grad_norm', 'eval_loss'):
            if key in metrics and not math.isfinite(float(metrics[key])):
                raise RuntimeError(f'{key} 出现非有限值，停止训练。')
        record = {'status': 'training', 'step': state.global_step, 'total_steps': state.max_steps,
                  'epoch': state.epoch, 'elapsed_seconds': round(time.time() - self.started, 1),
                  'metrics': metrics, 'gpu_peak_gib': round(torch.cuda.max_memory_allocated() / 1024 ** 3, 3)}
        with (self.directory / 'progress.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        write_json(self.directory / 'status.json', record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
