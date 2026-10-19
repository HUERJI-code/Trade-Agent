"""在固定测试集上对比 Ollama 模型的原始回答；指标是自动检查，不冒充人工准确率。"""
import argparse  # 指定模型标签、输出报告和样本数。
import hashlib  # 固定测试集和模型指纹。
import json  # 保存逐条原始生成结果。
import sys  # 将项目模块加入导入路径。
import time  # 记录耗时。
from collections import Counter  # 按问题类型统计失败原因。
from pathlib import Path  # 所有报告保存在 D 盘。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.local_model import OllamaClient, ModelSettings  # 复用本机请求限制。
from agent.answer_style import check_answer, number_tokens  # 原始模型结果不经过生产回退，再独立评分。


def score(text, expected, context):
    issues = check_answer(text, context)
    margin_basis = True
    if context.get('metric_id') == 'gross_margin' and context.get('display') and not context.get('ranking'):
        margin_basis = ('汇总估算毛利' in text and '汇总估算销售额' in text and context['display'] in text
                        and text.index('汇总估算毛利') < text.index(context['display']))
    return {'automatic_fact_checks_pass': not issues, 'issues': issues,
        'reference_numbers_match': number_tokens(text) == number_tokens(expected),
        'exact_reference_match': ''.join(text.split()) == ''.join(expected.split()),
        'concise': 0 < len(text) <= 220,
        'bold_main_value': not context.get('display') or f"**{context['display']}**" in text,
        'margin_basis_before_value': margin_basis}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--limit', type=int, default=0, help='0 表示全部 206 条；非零只做明确标注的子集评估')
    args = parser.parse_args()
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    dataset_path = ROOT / 'Data/answer_style/v1/test.json'
    records = json.loads(dataset_path.read_text(encoding='utf-8'))
    if args.limit:
        records = records[:args.limit]
    # 请求地址沿用受限客户端；评估模型标签来自本机已安装目录，不下载远程模型。
    client = OllamaClient(ModelSettings(timeout_seconds=300))
    installed = client._request('/api/tags')['models']
    selected = next((item for item in installed if item['name'] == args.model), None)
    if selected is None:
        raise RuntimeError('评估模型尚未导入本机 Ollama。')
    results_path = output / 'generations.jsonl'
    existing = [json.loads(line) for line in results_path.read_text(encoding='utf-8').splitlines()] if results_path.exists() else []
    dataset_sha = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    assert all(row['model_digest'] == selected['digest'] and row['dataset_sha256'] == dataset_sha for row in existing)
    results = {row['index']: row for row in existing}
    for index, record in enumerate(records):
        if index in results:
            continue  # 中断后只补做未评估的样本。
        started = time.perf_counter()
        context = json.loads(record['messages'][1]['content'])['context']
        reply = client._request('/api/chat', {'model': args.model, 'messages': record['messages'][:-1],
            'think': False, 'stream': False, 'keep_alive': '5m',
            'options': {'temperature': 0, 'seed': 42, 'num_ctx': 4096, 'num_predict': 384}})
        text = reply.get('message', {}).get('content', '').strip()
        expected = record['messages'][-1]['content']
        metrics = score(text, expected, context)
        if not reply.get('done') or reply.get('done_reason') == 'length':
            metrics['automatic_fact_checks_pass'] = False
            metrics['issues'].append('incomplete_generation')
        row = {'index': index, 'model': args.model, 'model_digest': selected['digest'], 'dataset_sha256': dataset_sha,
            'context': context, 'question': json.loads(record['messages'][1]['content'])['question'],
            'reference': expected, 'generated': text, 'metrics': metrics, 'elapsed_seconds': round(time.perf_counter() - started, 2)}
        with results_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        results[index] = row
        print(json.dumps({'done': len(results), 'total': len(records), 'index': index, 'metrics': metrics}, ensure_ascii=False), flush=True)
    for row in results.values():
        incomplete = 'incomplete_generation' in row['metrics']['issues']
        row['metrics'] = score(row['generated'], row['reference'], row['context'])  # 原文不变，恢复时统一评分规则。
        if incomplete:
            row['metrics']['automatic_fact_checks_pass'] = False
            row['metrics']['issues'].append('incomplete_generation')
    results_path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for _, row in sorted(results.items())), encoding='utf-8')
    keys = ['automatic_fact_checks_pass', 'reference_numbers_match', 'exact_reference_match', 'concise', 'bold_main_value', 'margin_basis_before_value']
    display_rows = [row for row in results.values() if row['context'].get('display')]
    margin_rows = [row for row in display_rows if row['context'].get('metric_id') == 'gross_margin' and not row['context'].get('ranking')]
    summary = {'model': args.model, 'model_digest': selected['digest'], 'samples': len(records), 'dataset_sha256': dataset_sha,
        'generation': {'temperature': 0, 'seed': 42, 'num_ctx': 4096, 'num_predict': 384},
        'rates': {key: sum(row['metrics'][key] for row in results.values()) / len(records) for key in keys},
        'applicable_style_rates': {
            'bold_main_value': {'count': len(display_rows), 'rate': sum(row['metrics']['bold_main_value'] for row in display_rows) / len(display_rows) if display_rows else None},
            'margin_basis_before_value': {'count': len(margin_rows), 'rate': sum(row['metrics']['margin_basis_before_value'] for row in margin_rows) / len(margin_rows) if margin_rows else None}},
        'issue_counts': dict(Counter(issue for row in results.values() for issue in row['metrics']['issues'])),
        'by_status': {status: {'count': sum(row['context']['status'] == status for row in results.values()),
            'automatic_fact_checks_pass': sum(row['metrics']['automatic_fact_checks_pass'] for row in results.values() if row['context']['status'] == status)
                / sum(row['context']['status'] == status for row in results.values())}
            for status in sorted({row['context']['status'] for row in results.values()})},
        'note': '自动规则与参考答案匹配率，不等于业务准确率；同一事实两种问法相关，需人工检查失败样本和反事实样本。'}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
