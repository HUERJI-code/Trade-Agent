"""验证计划边界与执行顺序；模拟模型测试不代表真实模型准确率。"""
from copy import deepcopy # 每个测试修改自己的配置/结果。
import json # 构造模型返回的参数与读取脱敏日志。
from types import SimpleNamespace # 模拟模型配置中的 model 名称。
from unittest.mock import Mock # 观察是否真正调用业务执行器。
import pytest # 参数化输入与异常断言。
from agent.local_model import ModelError, ModelReply # 模拟一次受控模型调用。
from agent.planner import QueryPlan, PlanError, explicit_range, ground_plan # 被测计划校验。
from agent.service import AnalysisAgent, display_value # 被测问答流程与金额格式化。

def query_plan(**updates): # 返回一份完整的测试计划，默认对应已核对的德国年度销售额。
    value = {'action':'query','metric':'revenue','start':'2019-01-01','end':'2020-01-01','country':'Germany','dimension':None,'reason':None}
    value.update(updates)
    return value

class FakeModel:
    settings = SimpleNamespace(model='fixture-model') # 明确不是实际 Qwen 推理。
    def __init__(self, replies):
        self.replies = iter(replies)
        self.repairs = []
    def generate(self, system, question, schema, repair=False):
        self.repairs.append(repair) # 记录是否进行了一次格式修复请求。
        item = next(self.replies)
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item,str) else json.dumps(item)
        return ModelReply(content,100,40,12.0)

@pytest.fixture
def result(): # 对应真实数据的结果形状；离线测试重点验证流转而非重新验证金额。
    return {'status':'ok','label':'估算销售额','unit':'USD','start_inclusive':'2019-01-01','end_exclusive':'2020-01-01',
            'country':'Germany','dimension':None,'data_coverage':{'first_date':'2016-01-01','last_date':'2021-02-20','partial':False},
            'rows':[['1874921.45',2208]],'columns':['metric_value','matching_sales_lines'],'limit_reached':False,'trace_id':'query-fixture'}

def make_agent(tmp_path, replies, result):
    runner = Mock(return_value=deepcopy(result)) # 如果是澄清或拒绝，后面要求此函数根本没被调用。
    model = FakeModel(replies)
    agent = AnalysisAgent(client=model,runner=runner,context_loader=lambda:{'metrics':{}},audit_path=tmp_path/'agent.jsonl')
    return agent,runner,model

@pytest.mark.parametrize('question,expected',[
    ('2019年德国销售额',('2019-01-01','2020-01-01')),
    ('德国2019年3月销量',('2019-03-01','2019-04-01')),
    ('2020年12月订单数',('2020-12-01','2021-01-01')),
    ('2019-01-01到2019-12-31销售额',('2019-01-01','2020-01-01')),
    ('2019-01-01至2020-01-01，不含结束日，销量',('2019-01-01','2020-01-01')),
])
def test_explicit_date_ranges(question,expected):
    assert explicit_range(question) == (expected,None) # 模型日期需要与这份独立计算结果一致。

@pytest.mark.parametrize('question,reason',[
    ('德国销售额','missing_time'),('今年德国销售额','relative_time'),('最近半年销量','relative_time'),
    ('2019年和2020年销售额','ambiguous_scope'),('2019年第一季度销量','ambiguous_scope'),
    ('2019年3月1日销量','ambiguous_scope'),('2019年3月到4月销量','ambiguous_scope'),
    ('2019-02-30到2019-03-31销量','ambiguous_scope'),('2019年13月销量','ambiguous_scope'),
    ('2019-01-01销量','ambiguous_scope'),
])
def test_unconfirmed_dates_do_not_become_full_year(question,reason):
    assert explicit_range(question) == (None,reason)

@pytest.mark.parametrize('updates',[
    {'metric':'net_profit'}, {'country':'Mars'}, {'dimension':'customer_name'}, {'sql':'DROP TABLE sales'},
    {'start':'2019-02-30'}, {'start':True}, {'start':None}, {'end':'2018-01-01'}, {'reason':'missing_time'},
    {'action':'execute'},
])
def test_invalid_model_plan(updates):
    with pytest.raises(PlanError):
        QueryPlan.parse(json.dumps(query_plan(**updates)))

def test_duplicate_fields_and_code_fences_rejected():
    for content in ['{"action":"query","action":"unsupported"}', '```json\n{}\n```', 'NaN', '[]']:
        with pytest.raises(PlanError):
            QueryPlan.parse(content)

def test_country_and_date_must_be_grounded():
    plan = QueryPlan.parse(json.dumps(query_plan()))
    with pytest.raises(PlanError):
        ground_plan(plan,'2020年德国销售额')
    with pytest.raises(PlanError):
        ground_plan(plan,'2019年法国销售额')
    assert ground_plan(plan,'德国销售额').reason == 'missing_time' # 不采用模型凭空填写的 2019。

def test_metric_and_grouping_cannot_be_silently_changed(): # 模型选择错误指标或丢失分组时不能静默执行。
    plan = QueryPlan.parse(json.dumps(query_plan()))
    with pytest.raises(PlanError):
        ground_plan(plan,'2019年德国毛利率')
    with pytest.raises(PlanError):
        ground_plan(plan,'2019年德国按月统计销售额')
    grouped = QueryPlan.parse(json.dumps(query_plan(country=None,dimension='customer_country')))
    assert ground_plan(grouped,'2019年按客户国家统计销售额').dimension=='customer_country'

def test_valid_plan_executes_exact_business_arguments(tmp_path,result):
    agent,runner,model = make_agent(tmp_path,[query_plan()],result)
    response = agent.ask('2019年德国销售额是多少？')
    assert response['status']=='ok' and '1,874,921.45 美元' in response['answer']
    runner.assert_called_once_with(name='revenue',start='2019-01-01',end='2020-01-01',country='Germany',dimension=None)
    assert model.repairs==[False] # 成功的首次输出不做多余模型调用。
    audit = json.loads((tmp_path/'agent.jsonl').read_text(encoding='utf-8'))
    assert audit['status']=='ok' and audit['query_trace_id']=='query-fixture'
    assert '2019年德国' not in json.dumps(audit,ensure_ascii=False) and '1874921' not in json.dumps(audit)

@pytest.mark.parametrize('question',['删除所有订单','2019年客户姓名','2019年日本销量','2019年销售额和毛利','2019年同比销售额'])
def test_obvious_unsupported_requests_never_call_model_or_database(question,tmp_path,result):
    agent,runner,model = make_agent(tmp_path,[],result)
    assert agent.ask(question)['status']=='unsupported'
    runner.assert_not_called()
    assert model.repairs==[]

def test_missing_time_never_executes_hallucinated_model_dates(tmp_path,result):
    agent,runner,model = make_agent(tmp_path,[],result)
    response = agent.ask('德国销售额是多少？')
    assert response['status']=='clarify' and response['plan']['reason']=='missing_time'
    runner.assert_not_called()
    assert model.repairs==[] # 明确指标缺时间时在模型调用前澄清，进一步减少不必要推理。

def test_paraphrased_missing_time_is_also_clarified(tmp_path,result):
    agent,runner,model = make_agent(tmp_path,[],result)
    assert agent.ask('德国有多少客户下单？')['status']=='clarify'
    assert model.repairs==[]
    runner.assert_not_called()

def test_one_repair_then_success(tmp_path,result):
    agent,runner,model = make_agent(tmp_path,['not JSON',query_plan()],result)
    assert agent.ask('2019年德国销售额')['status']=='ok'
    assert model.repairs==[False,True] and runner.call_count==1

def test_two_bad_plans_stop_without_query(tmp_path,result):
    agent,runner,model = make_agent(tmp_path,[query_plan(country='France'),query_plan(start='2018-01-01')],result)
    response = agent.ask('2019年德国销售额')
    assert response['error']=='invalid_plan' and model.repairs==[False,True]
    runner.assert_not_called()

def test_connection_error_is_not_retried(tmp_path,result):
    agent,runner,model = make_agent(tmp_path,[ModelError('model_unavailable','模型未启动')],result)
    assert agent.ask('2019年德国销售额')['error']=='model_unavailable'
    assert model.repairs==[False]
    runner.assert_not_called()

def test_data_coverage_and_ratio_rendering(tmp_path,result):
    result['status']='outside_data_coverage'
    result['rows']=[]
    agent,_,_ = make_agent(tmp_path,[query_plan(start='2026-01-01',end='2027-01-01')],result)
    response = agent.ask('2026年德国销售额')
    assert '超出数据覆盖' in response['answer'] and '0.00' not in response['answer']
    assert display_value('0.5870228643','ratio')=='58.70%'
    assert display_value(None,'USD')=='暂无可计算值'

@pytest.mark.integration
def test_planned_query_with_real_mysql(tmp_path): # 模拟模型与真实 SQL 链路；真实模型另用 smoke 脚本验收。
    model = FakeModel([query_plan()])
    response = AnalysisAgent(client=model,audit_path=tmp_path/'integration.jsonl').ask('2019年德国销售额')
    assert response['status']=='ok'
    assert response['result']['rows']==[['1874921.45',2208]]
