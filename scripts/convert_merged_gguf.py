"""调用固定版本 llama.cpp 的官方脚本，将完整 HF 模型导出 GGUF。"""
import json
import runpy
import sys
from training_support import ROOT, write_json
import torch

source = ROOT / '.runtime/llama-source-b6500/llama.cpp-a7a98e0fffed794396b3fbad4dcdbbc184963645/convert_hf_to_gguf.py'
merged = ROOT / 'models/huggingface/Qwen3-8B-answer-merged-v1'
assert json.loads((merged / 'merge_verification.json').read_text())['passed']
output = ROOT / 'models/gguf'
output.mkdir(parents=True, exist_ok=True)
final = output / 'trade-agent-qwen3-8b-lora-v1-f16.gguf'
partial = final.with_suffix('.gguf.part')
torch.set_num_threads(4)
sys.argv = [str(source), str(merged), '--outfile', str(partial), '--outtype', 'f16']
runpy.run_path(str(source), run_name='__main__')
partial.replace(final)
write_json(output / 'conversion_report.json', {'status': 'converted', 'llama_cpp': 'b6500',
    'commit': 'a7a98e0fffed794396b3fbad4dcdbbc184963645', 'input': str(merged), 'output': str(final), 'dtype': 'F16'})
print(final)
