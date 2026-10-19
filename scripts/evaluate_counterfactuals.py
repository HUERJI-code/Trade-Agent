"""保持问题不变，只替换可信结果，检查模型是否跟随当前事实而非背诵。"""
import argparse
import copy
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.local_model import OllamaClient, ModelSettings
from agent.answer_style import check_answer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--model', required=True)
args = parser.parse_args()
client = OllamaClient(ModelSettings(model=args.model, timeout_seconds=300))
records = json.loads((ROOT / 'Data/answer_style/v1/train.json').read_text(encoding='utf-8'))
changes = {'revenue': ('123456.78', '123,456.78 美元'), 'gross_profit': ('-1234.56', '-1,234.56 美元'),
           'gross_margin': ('0.1234', '12.34%'), 'order_count': ('123', '123 单'),
           'active_customer': ('45', '45 位'), 'quantity': ('678', '678 件')}
results = []
for metric, (value, display) in changes.items():
    selected = next(row for row in records if (lambda c: c.get('metric_id') == metric and c.get('country') == '德国'
        and c.get('period') == '2019年' and c.get('status') == 'ok' and c.get('display') and not c.get('ranking'))
        (json.loads(row['messages'][1]['content'])['context']))
    for variant in ('original', 'changed'):
        messages = copy.deepcopy(selected['messages'][:-1])
        data = json.loads(messages[1]['content'])
        if variant == 'changed':
            data['context'].update(value=value, display=display)
        messages[1]['content'] = json.dumps(data, ensure_ascii=False)
        response = client._request('/api/chat', {'model': args.model, 'messages': messages, 'think': False, 'stream': False,
            'options': {'temperature': 0, 'seed': 42, 'num_ctx': 4096, 'num_predict': 384}})
        text = response.get('message', {}).get('content', '')
        issues = check_answer(text, data['context'])
        if not response.get('done') or response.get('done_reason') == 'length':
            issues.append('incomplete_generation')
        results.append({'metric': metric, 'variant': variant, 'question': data['question'], 'expected_display': data['context']['display'],
                        'generated': text, 'passed': not issues, 'issues': issues})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
output = ROOT / 'outputs/evaluation/counterfactual_lora.json'
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({'model': args.model, 'passed': all(row['passed'] for row in results), 'cases': results,
    'note': '六个已知问题各测试原事实和替换事实；不是独立问数泛化准确率。'}, ensure_ascii=False, indent=2), encoding='utf-8')
