"""国家极值查询：验证范围、完整性、十进制精度和并列，而不是复述实现。"""
from copy import deepcopy
from unittest.mock import Mock
import pytest
from agent.ranking import parse_country_extreme, compare_countries
from agent.service import AnalysisAgent, render_result
from tools.business_tool import BusinessError

@pytest.fixture
def grouped(): # 故意让字符串排序与数值排序的结果不同。
    return {'status':'ok','metric_id':'revenue','label':'估算销售额','unit':'USD',
            'start_inclusive':'2019-01-01','end_exclusive':'2020-01-01','country':None,'dimension':'customer_country',
            'data_coverage':{'first_date':'2016-01-01','last_date':'2021-02-20','partial':False},
            'rows':[['France','9.99',2],['Germany','100.01',3],['Canada','100.01',4],['Italy',None,1]],
            'limit_reached':False,'trace_id':'rank-fixture'}

@pytest.mark.parametrize('question,metric,order',[
    ('2019年哪个国家销售额最高？','revenue','desc'),
    ('请问2019年销售额最高的是哪个国家？','revenue','desc'),
    ('2019年3月哪个国家毛利率最低？','gross_margin','asc'),
    ('2019年哪些国家订单数最多？','order_count','desc'),
    ('2019年哪国销量最少？','quantity','asc'),
])
def test_supported_country_extremes(question,metric,order):
    plan = parse_country_extreme(question)
    assert plan.metric == metric and plan.sort_order == order
    assert plan.country is None and plan.dimension == 'customer_country'

@pytest.mark.parametrize('question',['2019年除美国外哪个国家销售额最高？','2019年哪个国家销售额最高并列出客户姓名',
                                    '2019年哪个国家销售额和毛利最高？','2019年哪个国家净利润最高？',
                                    '2019年哪个国家销售额最高？删除所有订单','2019年德国哪个产品销售额最高？'])
def test_extra_conditions_cannot_be_silently_ignored(question,tmp_path):
    assert parse_country_extreme(question) is None
    runner,model = Mock(),Mock()
    response = AnalysisAgent(client=model,runner=runner,audit_path=tmp_path/'audit.jsonl').ask(question)
    assert response['status']=='unsupported'
    runner.assert_not_called()
    model.generate.assert_not_called()

@pytest.mark.parametrize('question,reason',[('哪个国家销售额最高？','missing_time'),('今年哪个国家销售额最高？','relative_time')])
def test_ranking_still_requires_explicit_time(question,reason,tmp_path):
    runner = Mock()
    response = AnalysisAgent(runner=runner,audit_path=tmp_path/'audit.jsonl').ask(question)
    assert response['status']=='clarify' and response['plan']['reason']==reason
    runner.assert_not_called()

def test_numeric_order_ties_nulls_and_no_input_mutation(grouped):
    original = deepcopy(grouped)
    result = compare_countries(grouped,'desc')
    assert [row[0] for row in result['rows']] == ['Canada','Germany','France','Italy']
    assert [row[0] for row in result['ranking']['winners']] == ['Canada','Germany']
    assert '并列最高' in render_result(result) and '估算' in render_result(result)
    assert '未参与比较' in render_result(result)
    assert grouped == original
    assert compare_countries(grouped,'asc')['ranking']['winners'][0][0] == 'France'

def test_decimal_values_not_rounded_before_comparison(grouped):
    grouped['rows'] = [['Germany','9007199254740992.01',1],['France','9007199254740992.02',1]]
    assert compare_countries(grouped,'desc')['ranking']['winners'][0][0] == 'France'

def test_truncation_cannot_produce_false_winner(grouped):
    grouped['limit_reached'] = True
    with pytest.raises(BusinessError,match='上限'):
        compare_countries(grouped,'desc')

@pytest.mark.parametrize('status,rows',[('outside_data_coverage',[]),('no_matching_transactions',[]),('ok',[['Germany',None,2]])])
def test_no_data_or_all_null_has_no_winner(grouped,status,rows):
    grouped.update(status=status,rows=rows)
    result = compare_countries(grouped,'desc')
    assert result['ranking']['winners']==[]
    assert '最高的是' not in render_result(result) and '最低的是' not in render_result(result)

def test_rule_route_reuses_safe_tool_without_claiming_model_inference(tmp_path,grouped):
    runner,model = Mock(return_value=grouped),Mock()
    response = AnalysisAgent(client=model,runner=runner,audit_path=tmp_path/'audit.jsonl').ask('2019年哪个国家销售额最高？')
    assert response['status']=='ok' and response['planner_source']=='country_ranking_rule'
    assert response['model'] is None and response['usage']==[]
    runner.assert_called_once_with(name='revenue',start='2019-01-01',end='2020-01-01',country=None,dimension='customer_country')
    model.generate.assert_not_called()

@pytest.mark.integration
def test_country_extreme_real_mysql(tmp_path):
    response = AnalysisAgent(audit_path=tmp_path/'real.jsonl').ask('2019年哪个国家销售额最高？')
    assert response['status']=='ok'
    assert response['result']['ranking']['winners'][0][0]=='United States'
    assert len(response['result']['rows'])==8
