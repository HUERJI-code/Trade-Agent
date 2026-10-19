"""离线核验导出的训练数据及查询快照；不连接数据库、不调用模型。"""

import hashlib  # 检查文件和事实快照完整性。
import json  # 读取 JSON 数组、JSONL 与来源信息。
import re  # 检查加粗数值确实存在于输入上下文。
from collections import Counter  # 汇总独立核验数量。
from decimal import Decimal  # 重新计算显示值和排名，不依赖生成脚本。
from pathlib import Path  # 固定检查项目内的 v1 数据。

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'Data/answer_style/v1'
COUNTRIES = {'Australia': '澳大利亚', 'Canada': '加拿大', 'France': '法国', 'Germany': '德国', 'Italy': '意大利', 'Netherlands': '荷兰', 'United Kingdom': '英国', 'United States': '美国'}


def formatted(value, unit):
    """独立重算显示格式，用于发现生成时取错列或单位的问题。"""
    number = Decimal(str(value))
    if unit == 'USD':
        return f'{number:,.2f} 美元'
    if unit == 'ratio':
        return f'{number * 100:.2f}%'
    return f'{int(number):,} ' + {'orders': '单', 'customers': '位', 'units': '件'}[unit]


def main():
    """逐条验证输入、目标、快照、跨划分隔离与格式等价性。"""
    manifest = json.loads((DATA / 'manifest.json').read_text(encoding='utf-8'))
    provenance = json.loads((DATA / 'provenance.json').read_text(encoding='utf-8'))
    metadata = {(r['split'], r['record_number']): r for r in provenance['records']}
    counts, group_splits, fact_splits, inputs = Counter(), {}, {}, set()
    for name, expected in manifest['report']['files_sha256'].items():
        assert hashlib.sha256((DATA / name).read_bytes()).hexdigest() == expected, name
    for fact in provenance['facts'].values():
        canonical = json.dumps(fact['result'], ensure_ascii=False, sort_keys=True).encode('utf-8')
        assert hashlib.sha256(canonical).hexdigest() == fact['result_sha256']
        assert not {'sql', 'trace_id', 'coverage_trace_id'} & fact['result'].keys()
    for split in ('train', 'validation', 'test'):
        records = json.loads((DATA / f'{split}.json').read_text(encoding='utf-8'))
        lines = [json.loads(line) for line in (DATA / f'{split}.jsonl').read_text(encoding='utf-8').splitlines()]
        assert records == lines
        assert len(records) == manifest['report']['split_stats'][split]['count']
        for index, record in enumerate(records, 1):
            meta = metadata[(split, index)]
            assert group_splits.setdefault(meta['group'], split) == split
            assert set(record) == {'messages'}
            messages = record['messages']
            assert [m['role'] for m in messages] == ['system', 'user', 'assistant']
            assert all(set(m) == {'role', 'content'} and isinstance(m['content'], str) and m['content'] for m in messages)
            identity = json.dumps(messages[:2], ensure_ascii=False, sort_keys=True)
            assert identity not in inputs
            inputs.add(identity)
            payload = json.loads(messages[1]['content'])
            context, answer = payload['context'], messages[2]['content']
            displays = [context['display']] if 'display' in context else []
            displays.extend(row['display'] for row in context.get('rows', []))
            for marked in re.findall(r'\*\*(.*?)\*\*', answer):
                if re.search(r'\d', marked):
                    assert marked in displays, (meta['id'], marked)
            if context.get('status') in ('query_failed', 'outside_data_coverage', 'undefined_metric', 'truncated'):
                assert not displays, meta['id']
            if meta['source'] == 'database':
                assert fact_splits.setdefault(meta['fact_id'], split) == split
                fact = provenance['facts'][meta['fact_id']]
                result, args = fact['result'], fact['arguments']
                assert manifest['year_split'][args['start'][:4]] == split
                assert context['metric_id'] == args['name']
                assert context['start_inclusive'] == args['start'] and context['end_exclusive'] == args['end']
                if args['dimension'] is None and result['status'] == 'ok':
                    assert context['value'] == result['rows'][0][0]
                    assert context['display'] == formatted(context['value'], result['unit'])
                    assert context['data_coverage'] == result['data_coverage']
                elif args['dimension'] == 'customer_country':
                    comparable = [row for row in result['rows'] if row[1] is not None]
                    choose = max if context['ranking'] == '最高' else min
                    value = choose(Decimal(row[1]) for row in comparable)
                    assert context['display'] == formatted(value, result['unit'])
                    assert context['winners'] == [COUNTRIES[row[0]] for row in comparable if Decimal(row[1]) == value]
                    assert context['compared_groups'] == len(comparable) and not result['limit_reached']
                elif args['dimension'] == 'order_month':
                    expected_rows = [{'month': row[0], 'value': row[1], 'display': formatted(row[1], result['unit'])} for row in sorted(result['rows'], key=lambda row: row[0])]
                    assert context['rows'] == expected_rows and context['table_available']
                elif result['status'] == 'outside_data_coverage':
                    assert context['status'] == result['status'] and not result['rows']
                else:
                    raise AssertionError(meta['id'])
            counts[meta['source']] += 1
    assert sum(counts.values()) == len(metadata) == 1110
    report = {'status': 'passed', 'samples_checked': len(metadata), 'sources': dict(counts), 'unique_query_snapshots': len(provenance['facts']),
              'checks': ['JSON 与 JSONL 一致', '消息结构完整', '无重复输入', '同一事实组与查询快照没有跨划分', '真实数值、单位、月度结果、排名与快照一致', '加粗数值均来自上下文', '文件与快照摘要一致'],
              'limitations': ['未训练或评测模型', '不是 tokenizer 长度检查', '同一模板的改写不是独立事实', '模拟及流程情境不代表数据库实测结果']}
    (DATA / 'audit_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
