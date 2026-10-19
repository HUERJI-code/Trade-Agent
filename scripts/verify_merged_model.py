"""核对低内存合并的七种目标层及未训练层，检查结果与 LoRA 公式一致。"""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from training_support import write_json
import torch
from safetensors import safe_open
from safetensors.torch import load_file

torch.set_num_threads(4)
base = ROOT / 'models/huggingface/Qwen3-8B'
merged = ROOT / 'models/huggingface/Qwen3-8B-answer-merged-v1'
run = ROOT / 'outputs/training/answer_style_qwen3_8b_sft_v1'
old_index = json.loads((base / 'model.safetensors.index.json').read_text())['weight_map']
new_index = json.loads((merged / 'model.safetensors.index.json').read_text())['weight_map']
assert set(old_index) == set(new_index)
adapter = load_file(run / 'adapter/adapter_model.safetensors')
config = json.loads((run / 'adapter/adapter_config.json').read_text())
records = []
modules = ['self_attn.' + key for key in ['q_proj','k_proj','v_proj','o_proj']]
modules += ['mlp.' + key for key in ['gate_proj','up_proj','down_proj']]
for name in ['model.layers.0.' + key + '.weight' for key in modules] + ['model.embed_tokens.weight', 'model.layers.0.input_layernorm.weight']:
    with safe_open(base / old_index[name], framework='pt', device='cpu') as source:
        old = source.get_tensor(name)
        with safe_open(merged / new_index[name], framework='pt', device='cpu') as exported:
            actual = exported.get_tensor(name)
            prefix = 'base_model.model.' + name.removesuffix('.weight')
            key_a, key_b = prefix + '.lora_A.weight', prefix + '.lora_B.weight'
            if key_a in adapter:
                delta = (adapter[key_b].float() @ adapter[key_a].float()) * (config['lora_alpha'] / config['r'])
                expected = (old + delta.to(old.dtype)).to(torch.float16)  # PEFT 0.18.1 safe_merge 先把增量转回基座精度。
                del delta
            else:
                expected = old.to(torch.float16)
            assert torch.equal(actual, expected), name
            records.append({'tensor': name, 'matches_formula_exactly': True, 'has_adapter': key_a in adapter})
            del expected, actual
        del old
write_json(merged / 'merge_verification.json', {'passed': True, 'tensor_catalog_complete': True, 'checks': records})
print(json.dumps({'passed': True, 'checks': len(records)}, ensure_ascii=False))
