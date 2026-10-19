"""检查错误金额、国家、估算口径和数据边界不会被自然语言输出掩盖。"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from agent.answer_style import check_answer, period_label, result_context, AnswerWriter
from agent.local_model import ModelSettings
from scripts.evaluate_answer_model import score


def test_reference_answers_pass_applicable_checks():
    records = json.loads((Path(__file__).resolve().parents[1] / 'Data/answer_style/v1/test.json').read_text(encoding='utf-8'))
    for row in records:
        context = json.loads(row['messages'][1]['content'])['context']
        text = row['messages'][-1]['content']
        result = score(text, text, context)
        assert result['automatic_fact_checks_pass'], result
        assert result['margin_basis_before_value']


def test_changed_value_country_and_estimate_are_rejected():
    context = dict(status='ok', period='2019年', country='德国', display='100.00 美元', unit='USD')
    assert not check_answer('2019年，德国的估算销售额为 **100.00 美元**。', context)
    assert 'missing_or_changed_display' in check_answer('2019年，德国的估算销售额为 **200.00 美元**。', context)
    assert 'unexpected_country' in check_answer('2019年，法国的估算销售额为 **100.00 美元**。', context)
    assert 'missing_estimate' in check_answer('2019年，德国的销售额为 **100.00 美元**。', context)


def test_fact_checks_ignore_spacing_but_preserve_sign_and_number_boundaries():
    context = dict(status='ok', period='2019年', country='德国', display='100.00 美元', unit='USD')
    assert not check_answer('2019年，德国估算销售额为100.00美元。', context)
    assert 'missing_or_changed_display' in check_answer('2019年，德国估算销售额为-100.00美元。', context)
    assert 'missing_or_changed_display' in check_answer('2019年，德国估算销售额为1100.00美元。', context)
    partial = {**context, 'status': 'partial_coverage'}
    assert not check_answer('2019年，德国估算销售额为100.00美元，未覆盖全年。', partial)


def test_date_boundaries_keep_exclusive_end():
    assert period_label('2019-01-01', '2020-01-01') == '2019年'
    assert period_label('2020-02-01', '2020-03-01') == '2020年2月'
    assert period_label('2021-01-01', '2021-02-21') == '2021年1月1日至2021年2月20日'


def test_partial_result_context_reports_actual_available_period():
    result = dict(status='ok', start_inclusive='2021-01-01', end_exclusive='2022-01-01', country='Germany',
                  metric_id='revenue', label='估算销售额', unit='USD', dimension=None, rows=[['100.00', 1]],
                  data_coverage={'first_date': '2016-01-01', 'last_date': '2021-02-20', 'partial': True}, limit_reached=False)
    context = result_context(result)
    assert context['period'] == '2021年1月1日至2021年2月20日'
    assert context['requested_period'] == '2021年'
    assert context['status'] == 'partial_coverage' and context['display'] == '100.00 美元'


def test_answer_writer_preserves_database_result_when_model_is_wrong_or_malformed():
    result = dict(status='ok', start_inclusive='2019-01-01', end_exclusive='2020-01-01', country='Germany',
                  metric_id='revenue', label='估算销售额', unit='USD', dimension=None, rows=[['100.00', 1]],
                  data_coverage={'first_date': '2016-01-01', 'last_date': '2021-02-20', 'partial': False}, limit_reached=False,
                  sql='SELECT internal_sql_not_for_answer_model')
    response = dict(status='ok', answer='原始可核对回答', result=result)
    client = SimpleNamespace(settings=ModelSettings(), _request=Mock(side_effect=[
        {'done': True, 'message': {'content': '2019年，德国的估算销售额为 **200.00 美元**。'}},
        {'done': True, 'message': None},
        {'done': True, 'message': {'content': '2019年，德国的估算销售额为 **100.00 美元**。'}}]))
    writer = AnswerWriter(client)
    for _ in range(2):
        rejected = writer.rewrite('2019年德国销售额', response)
        assert rejected['answer'] == '原始可核对回答' and rejected['result'] == result
        assert rejected['answer_source'] == 'verified_template_fallback'
    accepted = writer.rewrite('2019年德国销售额', response)
    assert accepted['answer_source'] == 'lora_model' and '**100.00 美元**' in accepted['answer']
    assert 'internal_sql' not in json.dumps(client._request.call_args.args[1])
