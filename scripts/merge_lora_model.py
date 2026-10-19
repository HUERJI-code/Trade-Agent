"""逐层用标准 PEFT 合并 LoRA，避免一次加载完整 8B 模型。"""
import gc  # 及时释放分片中间张量。
import hashlib  # 记录适配器指纹。
import json  # 配置和权重索引。
import shutil  # 复制原始推理 tokenizer。
from training_support import ROOT, write_json  # 缓存全部位于 D 盘。
import torch  # 每次处理一个线性层。
from peft import LoraConfig, get_peft_model  # 合并算法由 PEFT 实现。
from safetensors import safe_open  # 按名称读取单个张量。
from safetensors.torch import load_file, save_file  # 小适配器和输出分片。
import yaml  # 使用同一份训练配置。


def merge_weight(weight, adapter_a, adapter_b, config):
    """把当前真实线性层交给 PEFT，保留标准库的精度与合并行为。"""
    linear = torch.nn.Linear(weight.shape[1], weight.shape[0], bias=False, device='meta', dtype=weight.dtype)
    linear.weight = torch.nn.Parameter(weight, requires_grad=False)  # 挂入原始 BF16 权重。
    block = torch.nn.Module()
    block.add_module('projection', linear)  # 临时容器只有当前层。
    lora = LoraConfig(r=config['r'], lora_alpha=config['lora_alpha'], lora_dropout=config['lora_dropout'],
                      target_modules=['projection'], bias='none')
    adapted = get_peft_model(block, lora)
    layer = adapted.base_model.model.projection
    with torch.no_grad():
        layer.lora_A['default'].weight.copy_(adapter_a)  # 覆盖初始化，使用训练后的 A。
        layer.lora_B['default'].weight.copy_(adapter_b)  # 使用同一层训练后的 B。
    merged = adapted.merge_and_unload(safe_merge=True)  # 标准 PEFT 安全合并。
    return merged.projection.weight.detach().to(torch.float16)  # 只转换这一层。


def main():
    config = yaml.safe_load((ROOT / 'config/lora_training.yaml').read_text(encoding='utf-8'))
    run, base = ROOT / config['output_directory'], ROOT / config['model_directory']
    status = json.loads((run / 'status.json').read_text(encoding='utf-8'))
    assert status['status'] == 'completed', '必须先完成训练。'
    verified = json.loads((base / 'download_verified.json').read_text(encoding='utf-8'))
    assert verified['revision'] == config['model_revision']
    target = ROOT / 'models/huggingface/Qwen3-8B-answer-merged-v1'
    if (target / 'merge_report.json').exists():
        print('已有完整合并报告。', flush=True)
        return
    target.mkdir(parents=True, exist_ok=True)
    adapter_path = run / 'adapter/adapter_model.safetensors'
    adapter = load_file(adapter_path, device='cpu')  # 约 87 MB。
    adapter_config = json.loads((run / 'adapter/adapter_config.json').read_text(encoding='utf-8'))
    assert adapter_config['peft_type'] == 'LORA' and adapter_config['bias'] == 'none'
    for option in ('rank_pattern', 'alpha_pattern', 'use_dora', 'use_rslora', 'use_qalora',
                   'fan_in_fan_out', 'modules_to_save', 'lora_bias', 'layer_replication'):
        assert not adapter_config.get(option), f'此合并脚本未实现配置 {option}'
    torch.set_num_threads(4)
    original_index = json.loads((base / 'model.safetensors.index.json').read_text(encoding='utf-8'))
    input_index = original_index['weight_map']
    output_map, buffer, files, used = {}, {}, [], set()
    size = 0

    def flush():
        nonlocal buffer, size
        if not buffer:
            return
        filename = f'merged-part-{len(files) + 1:05d}.safetensors'
        save_file(buffer, target / filename, metadata={'format': 'pt'})
        output_map.update({name: filename for name in buffer})
        files.append(filename)
        buffer, size = {}, 0
        gc.collect()
        print(f'已保存分片 {len(files)}；已合并 {len(used) // 2} 个 LoRA 层。', flush=True)

    for shard_name in sorted(set(input_index.values())):
        with safe_open(base / shard_name, framework='pt', device='cpu') as shard:
            for name in sorted(shard.keys()):
                weight = shard.get_tensor(name)
                prefix = 'base_model.model.' + name.removesuffix('.weight')
                key_a, key_b = prefix + '.lora_A.weight', prefix + '.lora_B.weight'
                if key_a in adapter:
                    assert key_b in adapter and weight.ndim == 2
                    exported = merge_weight(weight, adapter[key_a], adapter[key_b], adapter_config)
                    used.update((key_a, key_b))
                else:
                    assert key_b not in adapter
                    exported = weight.to(torch.float16) if weight.is_floating_point() else weight.clone()
                assert torch.isfinite(exported).all(), f'非有限权重：{name}'
                weight_bytes = exported.numel() * exported.element_size()
                if size + weight_bytes > 900_000_000:
                    flush()  # 最大的 embedding 单独成片。
                buffer[name] = exported.contiguous()
                size += weight_bytes
                del weight, exported
    flush()
    assert used == set(adapter), '必须合并所有适配器张量。'
    assert set(output_map) == set(input_index), '不能遗漏原始权重。'
    final_map = {}
    for number, filename in enumerate(files, start=1):
        final = f'model-{number:05d}-of-{len(files):05d}.safetensors'
        (target / filename).replace(target / final)
        final_map.update({name: final for name, source in output_map.items() if source == filename})
    write_json(target / 'model.safetensors.index.json', {'metadata': original_index['metadata'], 'weight_map': final_map})
    model_config = json.loads((base / 'config.json').read_text(encoding='utf-8'))
    model_config['torch_dtype'] = 'float16'
    write_json(target / 'config.json', model_config)
    for filename in ('tokenizer.json', 'tokenizer_config.json', 'vocab.json', 'merges.txt',
                     'added_tokens.json', 'special_tokens_map.json', 'chat_template.jinja', 'generation_config.json'):
        if (base / filename).exists():
            shutil.copy2(base / filename, target / filename)  # 使用原始推理/工具模板。
    write_json(target / 'merge_report.json', {'status': 'merged', 'base_revision': config['model_revision'],
        'adapter': str(adapter_path.parent), 'training_steps': status['step'], 'merge_dtype': 'bfloat16', 'export_dtype': 'float16',
        'adapter_sha256': hashlib.sha256(adapter_path.read_bytes()).hexdigest(), 'merged_layers': len(used) // 2,
        'tensor_count': len(final_map), 'shards': len(files), 'method': 'per-layer PEFT merge_and_unload(safe_merge=True)',
        'original_base_preserved': True})
    print(f'完整模型合并完成：{target}', flush=True)


if __name__ == '__main__':
    main()
