"""生成已确认风格的 LoRA 数据 v1；只读 MySQL，不调用或训练模型。"""

import hashlib  # 为事实快照及输出文件计算摘要，方便核验来源。
import json  # 同时导出 JSON 数组和逐行 JSONL。
import random  # 使用固定种子打乱顺序，避免训练数据按指标成块排列。
import sys  # 定位项目模块，不依赖调用目录。
from collections import Counter  # 汇总划分、场景与来源数量。
from datetime import datetime, timezone  # 保存生成时间。
from decimal import Decimal  # 精确比较金额、比例与并列值。
from pathlib import Path  # 输出只能落在项目内。

import yaml  # 使用已有业务字典中的定义与限制。

ROOT = Path(__file__).resolve().parents[1]  # 项目根目录在 D 盘。
sys.path.insert(0, str(ROOT))  # 使 scripts 作为命名空间包可导入。
from scripts.build_answer_style_preview import SYSTEM  # 复用用户确认过的风格要求。
from agent.ranking import COUNTRY_LABELS  # 复用真实国家的中英文映射。
from agent.service import display_value  # 用现有 Decimal 格式化逻辑生成金额和百分比。
from tools.business_tool import run_metric  # 所有真实数据经只读工具和 SQL 校验取得。

OUTPUT = ROOT / 'Data' / 'answer_style' / 'v1'  # 与已审阅的 v0 文件分开保存。
METRICS = ('revenue', 'gross_profit', 'gross_margin', 'order_count', 'active_customer', 'quantity')
SCOPES = [*COUNTRY_LABELS, None]  # 八个客户国家，另加全部国家汇总。
YEAR_SPLIT = {2016: 'train', 2017: 'validation', 2018: 'train', 2019: 'train', 2020: 'test', 2021: 'train', 2014: 'validation', 2015: 'train', 2026: 'test'}
NAMES = {'revenue': '销售额', 'gross_profit': '毛利', 'gross_margin': '毛利率', 'order_count': '订单数', 'active_customer': '活跃客户数', 'quantity': '销量'}


def digest(value):
    """把结构化数据规范序列化后取摘要，顺序不同的字典视为同一份事实。"""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def market(country):
    """把工具国家参数转换为自然中文，明确全部国家是数据覆盖范围内的国家。"""
    return COUNTRY_LABELS[country] if country else '数据覆盖的全部客户国家'


class DatasetBuilder:
    """收集查询快照和候选消息；审计信息与真正训练消息分开保存。"""

    def __init__(self):
        self.definitions = yaml.safe_load((ROOT / 'config/business_metrics.yaml').read_text(encoding='utf-8'))['metrics']
        self.samples = []  # 含来源与划分的完整记录。
        self.facts = {}  # 相同 SQL 参数只查一次，并保留可离线核验的汇总结果。

    def fetch(self, metric, year, country=None, dimension=None):
        """只查询一个明确年度，缓存排除 SQL、审计标识后的结果。"""
        args = {'name': metric, 'start': f'{year}-01-01', 'end': f'{year + 1}-01-01', 'country': country, 'dimension': dimension}
        key = digest(args)
        if key not in self.facts:
            result = run_metric(**args)
            safe = {k: v for k, v in result.items() if k not in ('sql', 'trace_id', 'coverage_trace_id')}
            self.facts[key] = {'arguments': args, 'result': safe, 'result_sha256': digest(safe)}
            if len(self.facts) % 30 == 0:
                print(f'已完成 {len(self.facts)} 个只读查询，已组织 {len(self.samples)} 条样例。', flush=True)
        return key, self.facts[key]['result']

    def context(self, metric, year, country):
        """统一回答阶段输入结构，避免不同来源的关键字段采用不同含义。"""
        definition = self.definitions[metric]
        return {'status': 'ok', 'period': f'{year}年', 'start_inclusive': f'{year}-01-01', 'end_exclusive': f'{year + 1}-01-01',
                'country': market(country), 'metric_id': metric, 'metric': definition['label'], 'unit': definition['unit'],
                'definition': definition['description'], 'limitations': definition['limitations']}

    def add(self, group, split, scene, source, questions, context, answer, fact_id=None):
        """同一事实的两种问法放在同一个 group 和 split 中。"""
        for question in questions:
            messages = [{'role': 'system', 'content': SYSTEM},
                        {'role': 'user', 'content': json.dumps({'question': question, 'context': context}, ensure_ascii=False)},
                        {'role': 'assistant', 'content': answer}]
            self.samples.append({'id': f'answer-v1-{len(self.samples) + 1:05}', 'group': group, 'split': split,
                                 'scene': scene, 'source': source, 'fact_id': fact_id, 'messages': messages})

    def scalar_answer(self, metric, context):
        """语序遵循用户确认风格；句子由可靠事实填充，不由模型臆造。"""
        period, scope, value = context['period'], context['country'], context['display']
        templates = {
            'revenue': '{period}，{scope}的估算销售额为 **{value}**。这是按产品标价计算的金额。',
            'gross_profit': '{period}，按销售数量乘以产品标价与单位生产成本的差额后汇总，{scope}的估算毛利为 **{value}**，还没有扣除其他经营费用。',
            'gross_margin': '{period}，按汇总估算毛利除以汇总估算销售额计算，{scope}的估算毛利率为 **{value}**。',
            'order_count': '{period}，{scope}共计 **{value}**。当同一订单包含多件商品时，也只计一单。',
            'active_customer': '{period}，{scope}有 **{value}**客户下过单。当同一客户多次下单时，也只计一位。',
            'quantity': '{period}，{scope}共售出 **{value}**商品。由于数据不包含退货数量，这个结果没有扣除退货。',
        }
        return templates[metric].format(period=period, scope=scope, value=value)

    def build_real(self):
        """覆盖六个指标、所有市场、完整年度、部分年度、分组与极值。"""
        for year in range(2016, 2022):
            for country in SCOPES:
                scope = market(country)
                for metric in METRICS:
                    fact_id, result = self.fetch(metric, year, country)
                    assert result['status'] == 'ok' and not result['limit_reached'], '真实汇总异常，需要人工检查。'
                    context = self.context(metric, year, country)
                    context.update(value=result['rows'][0][0], display=display_value(result['rows'][0][0], result['unit']), data_coverage=result['data_coverage'])
                    assert context['value'] is not None, '空值需要专用回答，不能套用成功文案。'
                    if result['data_coverage']['partial']:
                        assert year == 2021 and result['data_coverage']['last_date'] == '2021-02-20'
                        context.update(status='partial_coverage', requested_period='2021年全年', period='2021年1月1日至2月20日')
                    answer = self.scalar_answer(metric, context)
                    if context['status'] == 'partial_coverage':
                        answer += '数据只到2月20日，这不是2021年全年的结果。'
                    questions = [f'{year}年{scope}的{NAMES[metric]}是多少？', f'帮我查一下{year}年{scope}的{NAMES[metric]}。']
                    self.add(f'year:{year}', YEAR_SPLIT[year], 'partial_coverage' if year == 2021 else 'scalar', 'database', questions, context, answer, fact_id)

        for year in range(2016, 2021):
            for metric in METRICS:
                fact_id, result = self.fetch(metric, year, dimension='customer_country')
                assert result['status'] == 'ok' and not result['limit_reached'] and not result['data_coverage']['partial']
                rows = [row for row in result['rows'] if row[1] is not None]
                for direction, selector in [('最高', max), ('最低', min)]:
                    extreme = selector(Decimal(row[1]) for row in rows)
                    winners = [COUNTRY_LABELS[row[0]] for row in rows if Decimal(row[1]) == extreme]
                    context = self.context(metric, year, None)
                    context.update(value=str(extreme), display=display_value(extreme, result['unit']), ranking=direction,
                                   winners=winners, compared_groups=len(rows), null_groups=len(result['rows']) - len(rows), complete=True)
                    winner = '、'.join(winners)
                    tied, equality = ('并列', '均为') if len(winners) > 1 else ('', '为')
                    answer = f'{year}年，在有可计算结果的 {len(rows)} 个客户国家中，**{winner}**的{context["metric"]}{tied}{direction}，{equality} **{context["display"]}**。'
                    if context['null_groups']:
                        answer += f'另有 {context["null_groups"]} 个国家的指标为空，未参与比较。'
                    self.add(f'year:{year}', YEAR_SPLIT[year], 'country_extreme', 'database',
                             [f'{year}年哪个国家{NAMES[metric]}{direction}？', f'请查询{year}年{NAMES[metric]}{direction}的国家。'], context, answer, fact_id)

            for country in ('Germany', 'United Kingdom'):
                for metric in ('revenue', 'quantity'):
                    fact_id, result = self.fetch(metric, year, country, 'order_month')
                    rows = sorted(result['rows'], key=lambda row: row[0])
                    assert len(rows) == 12 and not result['limit_reached'] and all(row[1] is not None for row in rows)
                    context = self.context(metric, year, country)
                    context.update(table_available=True, dimension='order_month', group_count=12,
                                   rows=[{'month': row[0], 'value': row[1], 'display': display_value(row[1], result['unit'])} for row in rows])
                    first, last = context['rows'][0], context['rows'][-1]
                    answer = f'{year}年，{market(country)}1月的{context["metric"]}为 **{first["display"]}**，12月为 **{last["display"]}**。下表列出了全年12个月的结果，可以逐月查看。'
                    self.add(f'year:{year}', YEAR_SPLIT[year], 'monthly_table', 'database',
                             [f'{year}年{market(country)}每个月的{NAMES[metric]}是多少？', f'按月列出{year}年{market(country)}的{NAMES[metric]}。'], context, answer, fact_id)

        for year in (2014, 2015, 2026):
            for country in SCOPES:
                fact_id, result = self.fetch('order_count', year, country)
                assert result['status'] == 'outside_data_coverage'
                context = self.context('order_count', year, country)
                context.update(status='outside_data_coverage', value=None, data_coverage=result['data_coverage'])
                answer = f'这份数据只覆盖2016年1月1日至2021年2月20日，查不到{year}年{market(country)}的订单。这表示缺少数据，不能据此认定订单数为零。'
                self.add(f'year:{year}', YEAR_SPLIT[year], 'outside_data_coverage', 'database',
                         [f'{year}年{market(country)}有多少订单？', f'查一下{year}年{market(country)}的订单数。'], context, answer, fact_id)

    def build_edges(self):
        """模拟边界与反事实用于学习遵循输入，不代表真实商业数据。"""
        for year in range(2016, 2021):
            for country in ('Germany', 'France', 'United States'):
                scope = market(country)
                for scene, metric in [('no_matching_transactions', 'revenue'), ('zero_orders', 'order_count'), ('undefined_metric', 'gross_margin'), ('counterfactual_value', 'revenue'), ('partial_coverage_simulated', 'revenue')]:
                    context = self.context(metric, year, country)
                    if scene == 'no_matching_transactions':
                        context.update(status=scene, value=None, matching_sales_lines=0, data_coverage_complete=True)
                        answer = f'在这份数据中，没有找到{scope}{year}年的销售记录，因此没有可报告的估算销售额。'
                    elif scene == 'zero_orders':
                        context.update(status='no_matching_transactions', value=0, display='0 单', matching_sales_lines=0, data_coverage_complete=True)
                        answer = f'{year}年，在这份数据中没有找到{scope}的订单，按当前筛选条件统计为 **0 单**。'
                    elif scene == 'undefined_metric':
                        context.update(status=scene, value=None, reason='汇总估算销售额为零')
                        answer = f'{year}年，{scope}的汇总估算销售额为零，无法据此计算毛利率，结果应为空，不能写成0%。'
                    else:
                        value = Decimal((year - 2010) * 17321 + SCOPES.index(country) * 977) + Decimal('0.45')
                        context.update(value=str(value), display=display_value(value, 'USD'))
                        if scene == 'partial_coverage_simulated':
                            context.update(status='partial_coverage', requested_period=f'{year}年全年', period=f'{year}年1月1日至2月20日',
                                           data_coverage={'first_date': '2016-01-01', 'last_date': f'{year}-02-20', 'partial': True})
                        answer = self.scalar_answer(metric, context)
                        if scene == 'partial_coverage_simulated':
                            answer += f'数据只到2月20日，这不是{year}年全年的结果。'
                    self.add(f'year:{year}', YEAR_SPLIT[year], scene, 'synthetic',
                             [f'{year}年{scope}的{NAMES[metric]}是多少？', f'帮我查一下{year}年{scope}的{NAMES[metric]}。'], context, answer)

            for scene in ('tied_extreme', 'truncated'):
                context = self.context('revenue', year, None)
                if scene == 'tied_extreme':
                    value = Decimal((year - 2010) * 20000)
                    context.update(ranking='最高', winners=['德国', '法国'], compared_groups=8, null_groups=0, complete=True, value=str(value), display=display_value(value, 'USD'))
                    answer = f'{year}年，在参与比较的 8 个客户国家中，**德国和法国并列第一**，估算销售额均为 **{context["display"]}**。'
                else:
                    context.update(status='truncated', complete=False, reason='国家分组结果触及返回上限')
                    answer = f'这次返回的国家数据不完整，暂时无法确定{year}年哪个国家的估算销售额最高，需要取得完整分组结果后再比较。'
                self.add(f'year:{year}', YEAR_SPLIT[year], scene, 'synthetic',
                         [f'{year}年哪个国家销售额最高？', f'请查询{year}年销售额最高的国家。'], context, answer)

    def build_flows(self):
        """编写澄清和失败场景；这是回答阶段训练，不宣称前置规则已识别每种问法。"""
        for year in range(2016, 2021):
            for country in ('Germany', 'France'):
                scope = market(country)
                cases = [
                    ('missing_metric', [f'{year}年{scope}的情况怎么样？', f'帮我看看{scope}{year}年的情况。'],
                     {'status': 'clarify', 'reason': 'missing_metric', 'period': f'{year}年', 'country': scope, 'supported_examples': ['估算销售额', '订单数', '销量'], 'conversation_memory': False},
                     f'你想了解{scope}{year}年的销售额、订单数，还是销量？请重新提交一个具体问题，例如“{year}年{scope}有多少订单？”。'),
                    ('net_profit_unavailable', [f'{year}年{scope}净利润是多少？', f'帮我算算{scope}{year}年的净利润。'],
                     {'status': 'unsupported', 'reason': 'net_profit_unavailable', 'period': f'{year}年', 'country': scope, 'missing_data': '完整经营费用', 'available_alternative': '估算毛利'},
                     f'这份数据缺少完整经营费用，无法算出{scope}{year}年的净利润。可以查询同年的估算毛利，但它不等于净利润。'),
                    ('query_failed', [f'{year}年{scope}的销售额是多少？', f'帮我查一下{year}年{scope}的销售额。'],
                     {'status': 'query_failed', 'result_available': False, 'period': f'{year}年', 'country': scope},
                     f'这次查询没有成功，暂时无法给出{scope}{year}年的销售额。请稍后重新提交这个问题。'),
                ]
                for scene, questions, context, answer in cases:
                    self.add(f'year:{year}', YEAR_SPLIT[year], scene, 'authored_flow', questions, context, answer)
        for index, country in enumerate(SCOPES):
            scope = market(country)
            split = 'validation' if index == 0 else 'test' if index == 1 else 'train'
            context = {'status': 'clarify', 'reason': 'missing_time', 'country': scope, 'metric': '估算销售额', 'conversation_memory': False}
            self.add(f'missing-time:{country}', split, 'missing_time', 'authored_flow', [f'{scope}销售额是多少？', f'查一下{scope}的销售额。'], context,
                     f'你想查{scope}哪段时间的销售额？请补上时间后重新提交，例如“2019年{scope}销售额是多少？”。')

    def validate(self):
        """导出前检查格式、来源、精确值、划分和用户指定句式。"""
        groups, inputs, targets = {}, set(), {}
        for sample in self.samples:
            assert groups.setdefault(sample['group'], sample['split']) == sample['split'], '同一事实组跨数据集。'
            messages = sample['messages']
            assert [m['role'] for m in messages] == ['system', 'user', 'assistant']
            payload = json.loads(messages[1]['content'])
            key = digest(messages[:2])
            assert key not in inputs, '重复训练输入。'
            inputs.add(key)
            context, answer = payload['context'], messages[2]['content']
            if context.get('display'):
                assert context['display'] in answer, '目标回答丢失精确值。'
            if sample['source'] == 'database':
                fact = self.facts[sample['fact_id']]
                assert fact['result_sha256'] == digest(fact['result'])
                if 'display' in context and fact['result']['dimension'] is None:
                    assert context['display'] == display_value(fact['result']['rows'][0][0], fact['result']['unit'])
            else:
                assert sample['fact_id'] is None
            if context.get('metric_id') in ('revenue', 'gross_profit', 'gross_margin') and context.get('display'):
                assert '估算' in answer
            targets.setdefault(key, answer)
        approved = ['2019年，按汇总估算毛利除以汇总估算销售额计算，德国的估算毛利率为 **58.70%**。',
                    '2019年，德国共计 **892 单**。当同一订单包含多件商品时，也只计一单。']
        assert all(text in targets.values() for text in approved), '没有保留用户确认的原句。'
        return {'samples': len(self.samples), 'fact_queries': len(self.facts), 'split_groups_disjoint': True, 'duplicate_inputs': 0, 'approved_examples_exact': True}

    def write(self):
        """每个划分提供两种等价格式；来源和检查报告单独保存。"""
        report = self.validate()
        OUTPUT.mkdir(parents=True, exist_ok=True)
        checksums, split_stats, ordered_samples = {}, {}, []
        for split in ('train', 'validation', 'test'):
            samples = [s for s in self.samples if s['split'] == split]
            random.Random(42).shuffle(samples)
            records = [{'messages': s['messages']} for s in samples]
            split_stats[split] = {'count': len(samples), 'scenes': dict(Counter(s['scene'] for s in samples)), 'sources': dict(Counter(s['source'] for s in samples))}
            contents = {'json': json.dumps(records, ensure_ascii=False, indent=2) + '\n', 'jsonl': ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records)}
            for extension, content in contents.items():
                path = OUTPUT / f'{split}.{extension}'
                path.write_text(content, encoding='utf-8')
                checksums[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            for line, sample in enumerate(samples, 1):
                ordered_samples.append({**{k: v for k, v in sample.items() if k != 'messages'}, 'record_number': line})
        (OUTPUT / 'provenance.json').write_text(json.dumps({'records': ordered_samples, 'facts': self.facts}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        report.update(split_stats=split_stats, files_sha256=checksums)
        (OUTPUT / 'validation_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        manifest = {'version': 'v1', 'generated_at': datetime.now(timezone.utc).isoformat(), 'style': '用户已确认；计算口径在结果前，计数规则使用完整条件句',
                    'training_started': False, 'task': 'grounded_answer_style', 'generation': '人工编写句式 + 只读数据库结果 + 明确标记的模拟情境；未调用模型生成',
                    'split_method': '按查询年度整体隔离，同年度的全部国家、指标、汇总、分组、改写与反事实属于同一划分；缺时间按国家分组',
                    'year_split': YEAR_SPLIT, 'seed': 42, 'dataset': 'Kaggle Global Electronics Retailers (fictional teaching dataset)',
                    'source_url': 'https://www.kaggle.com/datasets/bhavikjikadara/global-electronics-retailers',
                    'business_dictionary_sha256': hashlib.sha256((ROOT / 'config/business_metrics.yaml').read_bytes()).hexdigest(),
                    'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'report': report}
        (OUTPUT / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        # 精选所有场景及用户确认的两个原句，便于阅读而不用打开整个训练文件。
        selected = [s for s in self.samples if s['messages'][2]['content'] in (
            '2019年，按汇总估算毛利除以汇总估算销售额计算，德国的估算毛利率为 **58.70%**。',
            '2019年，德国共计 **892 单**。当同一订单包含多件商品时，也只计一单。')][::2]
        seen = {s['scene'] for s in selected}
        for sample in self.samples:
            if sample['scene'] not in seen:
                selected.append(sample)
                seen.add(sample['scene'])
        lines = ['# LoRA 回答训练数据 v1：精选样例', '', '已按用户确认风格生成；只准备数据，未训练。模拟样例不代表数据库事实。完整说明见《LoRA回答训练数据_v1说明》。', '']
        for sample in selected:
            payload = json.loads(sample['messages'][1]['content'])
            lines.extend([f'## {sample["id"]} · {sample["scene"]}', '', f'来源：{sample["source"]}；划分：{sample["split"]}', '',
                          f'**问题：** {payload["question"]}', '', sample['messages'][2]['content'], ''])
        (ROOT / 'Documents/LoRA回答训练数据_v1精选.md').write_text('\n'.join(lines), encoding='utf-8')
        print(json.dumps({'output': str(OUTPUT), **report}, ensure_ascii=False), flush=True)


def main():
    """保护已生成版本；本次脚本不覆盖 v0，也不重复覆盖已完成的 v1。"""
    if OUTPUT.exists() and any(OUTPUT.iterdir()):
        raise SystemExit('v1 目录已有文件；请保留已有版本，明确更改输出版本后再生成。')
    builder = DatasetBuilder()
    builder.build_real()
    builder.build_edges()
    builder.build_flows()
    builder.write()


if __name__ == '__main__':
    main()
