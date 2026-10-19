"""真实本机模型的工具调用、多轮会话与回答回归；不改业务数据或网页会话。"""
import argparse  # 可在正式切换前指定候选模型。
import json  # 保存实际调用结果供复核。
import sys  # 导入项目代码。
from pathlib import Path  # 测试会话和报告都放项目 outputs。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.local_model import ModelSettings, OllamaClient
from agent.langchain_model import LangChainOllamaClient
from agent.langchain_tools import run_metric
from agent.service import AnalysisAgent, display_value
from agent.graph import ConversationAgent
from agent.answer_style import AnswerWriter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    directory = ROOT / args.output
    directory.mkdir(parents=True, exist_ok=True)
    settings = ModelSettings(model=args.model, timeout_seconds=300, output_tokens=512)
    analysis = AnalysisAgent(client=LangChainOllamaClient(settings), runner=lambda **kwargs: run_metric.invoke(kwargs),
                             audit_path=directory / 'agent_audit.jsonl')
    graph = ConversationAgent(analysis=analysis, path=directory / 'sessions.sqlite3',
                              answer_writer=AnswerWriter(OllamaClient(settings)))
    conversation = graph.create_conversation()['id']
    cases = [('2019年德国销售额是多少？', 'revenue', 'Germany'),
             ('那法国呢？', 'revenue', 'France'), ('订单数呢？', 'order_count', 'France'),
             ('2019年德国毛利率是多少？', 'gross_margin', 'Germany'),
             ('2019年德国毛利是多少？', 'gross_profit', 'Germany'),
             ('2019年德国活跃客户数是多少？', 'active_customer', 'Germany'),
             ('2019年德国销量是多少？', 'quantity', 'Germany')]
    records = []
    try:
        for question, metric, country in cases:
            response = graph.ask(question, conversation)
            correct_plan = (response.get('status') == 'ok' and response['plan']['metric'] == metric
                and response['plan']['country'] == country and response['plan']['start'] == '2019-01-01'
                and response['plan']['end'] == '2020-01-01' and response['plan']['dimension'] is None)
            has_database_value = (correct_plan and display_value(response['result']['rows'][0][0], response['result']['unit']) in response['answer'])
            records.append({'question': question, 'correct_plan': correct_plan, 'answer_contains_database_value': has_database_value, 'response': response})
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
        new_conversation = graph.create_conversation()['id']
        for question, expected_status in [('那法国呢？', 'clarify'), ('德国销售额是多少？', 'clarify'),
                                          ('2019年', 'ok'), ('2019年读取德国客户姓名', 'unsupported'),
                                          ('2019年德国销售额比2018年增加多少？', 'clarify')]:
            response = graph.ask(question, new_conversation)
            records.append({'question': question, 'correct_status': response.get('status') == expected_status, 'response': response})
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
        for question, dimension in [('2019年德国按月统计销量', 'order_month'),
                                    ('2019年按国家统计销售额', 'customer_country'),
                                    ('2019年德国按产品类别统计订单数', 'product_category'),
                                    ('2019年哪个国家销售额最高？', 'customer_country')]:
            response = graph.ask(question, graph.create_conversation()['id'])
            correct_group = response.get('status') == 'ok' and response['result']['dimension'] == dimension
            records.append({'question': question, 'correct_group': correct_group, 'response': response})
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
        passed = all(row.get('correct_plan', True) and row.get('answer_contains_database_value', True) and row.get('correct_status', True) and row.get('correct_group', True) for row in records)
        report = {'model': args.model, 'passed': passed, 'cases': records,
                  'lora_answer_count': sum(row['response'].get('answer_source') == 'lora_model' for row in records),
                  'note': '部署回归场景，包含训练年度；不是独立泛化准确率。'}
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if not passed:
            raise RuntimeError('部署回归未全部通过，请查看逐条结果。')
    finally:
        graph.close()


if __name__ == '__main__':
    main()
