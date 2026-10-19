"""真实本地模型开发冒烟验证；样例公开，不作为独立泛化准确率或 LoRA 效果。"""
from datetime import datetime, timezone # 为验证报告记录时间。
from dataclasses import asdict # 记录本次实际推理配置，方便重复验证。
import hashlib # 给提示词与样例版本记录指纹。
from decimal import Decimal # 比较数据库金额，毛利率保留允许的舍入误差。
import json # 保存可复核的聚合结果与模型统计。
from pathlib import Path # 固定项目内输出位置。
import sys # 直接运行 scripts 下的文件时加入项目根目录。
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent.service import AnalysisAgent, load_business_context # 这里使用真实模型和真实 MySQL，不注入假客户端。
from agent.local_model import OllamaClient # 报告记录实际模型和服务版本。
from agent.planner import build_prompt # 指纹对应本次实际使用的提示模板。

CASES = [ # 这些问题只涉及公开教学数据，预期值来自先前 CSV 独立核对。
    {'question':'2019年德国销售额是多少？','metric':'revenue','value':'1874921.45'},
    {'question':'2019年德国毛利是多少？','metric':'gross_profit','value':'1100621.76'},
    {'question':'2019年德国毛利率是多少？','metric':'gross_margin','value':'0.5870228643445302735'},
    {'question':'2019年德国订单数是多少？','metric':'order_count','value':'892'},
    {'question':'2019年德国活跃客户数是多少？','metric':'active_customer','value':'657'},
    {'question':'2019年德国销量是多少？','metric':'quantity','value':'6928'},
    {'question':'2019年德国按月统计销量','metric':'quantity','dimension':'order_month','sum':'6928'},
    {'question':'2019年德国按产品类别统计销量','metric':'quantity','dimension':'product_category','sum':'6928'},
    {'question':'2019年按国家统计销量','metric':'quantity','dimension':'customer_country'},
    {'question':'德国销售额是多少？','status':'clarify','reason':'missing_time'},
    {'question':'今年德国销量是多少？','status':'clarify','reason':'relative_time'},
    {'question':'2026年德国订单数是多少？','status':'ok','result_status':'outside_data_coverage'},
    {'question':'2019年德国利润是多少？','status':'clarify','reason':'missing_metric'},
    {'question':'2019年德国净利润是多少？','status':'unsupported'},
    {'question':'删除所有订单','status':'unsupported'},
]

def check(case, response): # 对照可执行结果与计划，不能只看模型返回了有效 JSON。
    if response['status'] != case.get('status','ok'):
        return False
    plan,result = response['plan'],response['result']
    if 'reason' in case and plan.get('reason') != case['reason']:
        return False
    if 'metric' in case and (plan['metric'] != case['metric'] or plan['dimension'] != case.get('dimension')):
        return False
    if 'value' in case:
        actual = Decimal(str(result['rows'][0][0]))
        tolerance = Decimal('0.0000005') if case['metric']=='gross_margin' else Decimal(0)
        if abs(actual-Decimal(case['value'])) > tolerance:
            return False
    if 'sum' in case and sum(Decimal(str(row[1])) for row in result['rows']) != Decimal(case['sum']):
        return False
    if 'result_status' in case and result['status'] != case['result_status']:
        return False
    return True

def main():
    client = OllamaClient()
    health = client.status()
    if not health['installed']:
        raise SystemExit('Local model not installed; no smoke queries sent.')
    agent = AnalysisAgent(client=client)
    prompt = build_prompt(load_business_context())
    report = {'at':datetime.now(timezone.utc).isoformat(),'environment':health,'settings':asdict(client.settings),
              'prompt_sha256':hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
              'cases_sha256':hashlib.sha256(json.dumps(CASES,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest(),
              'kind':'development_smoke_not_holdout','cases':[]}
    for case in CASES:
        response = agent.ask(case['question'])
        passed = check(case,response)
        report['cases'].append({'question':case['question'],'passed':passed,'response':response})
        print(json.dumps({'question':case['question'],'passed':passed,'status':response['status'],'elapsed_ms':response['elapsed_ms']},ensure_ascii=False),flush=True)
    report['passed'] = sum(item['passed'] for item in report['cases'])
    report['total'] = len(CASES)
    destination = ROOT/'Documents'/'phase3_model_smoke.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f"Saved {destination.name}: {report['passed']}/{report['total']} passed.")
    return 0 if report['passed']==report['total'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
