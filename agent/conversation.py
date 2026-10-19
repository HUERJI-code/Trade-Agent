"""将追问中缺少的条件补为可独立校验的问题，不改写用户明确给出的条件。"""
import re  # 识别字段是否已经明确出现，避免历史覆盖本轮条件。
from agent.planner import mentioned_countries, explicit_range  # 复用原有日期、国家识别。

METRIC_WORDS = {'revenue': '销售额', 'gross_profit': '毛利', 'gross_margin': '毛利率',
                'order_count': '订单数', 'active_customer': '活跃客户数', 'quantity': '销量'}
DIMENSION_WORDS = {'order_month': '按月', 'customer_country': '按国家', 'product_category': '按产品类别'}


def complete_question(question, previous):
    """基于最近确认的计划补齐省略条件；多个年份、相对日期等歧义仍交给原校验器。"""
    if not previous or previous.get('action') != 'query':
        return question, []
    period, reason = explicit_range(question)
    country = mentioned_countries(question)
    metric = bool(re.search(r'销售|毛利|订单|客户数|客户下单|销量|件数|多少件', question))
    all_countries = bool(re.search(r'全部|所有国家|各国|每个国家|按国家|哪个国家', question))
    group = bool(re.search(r'按月|每月|月度|按.*(?:类别|品类)|各.*(?:类别|品类)|按国家|各国', question))
    referential = bool(re.search(r'那|呢|同样|继续|改成|改为|换成|换为|刚才|上述|这个|这段|这年', question))
    # 明确换话题时不继承；无业务含义的问题也不补入历史查询。
    if re.search(r'新问题|换个话题|重新开始', question) or not (period or country or metric or group or referential):
        return question, []
    # 完整的新问题默认采用自己的范围；省略或明确追问时才继承。
    followup = referential or not (period and metric)
    if not followup:
        return question, []
    additions, inherited = [], []
    if reason == 'missing_time':
        additions.append(f"时间从{previous['start']}至{previous['end']}（不含结束日）")
        inherited.append('time')
    if not country and not all_countries and previous.get('country') and previous.get('dimension') != 'customer_country':
        additions.append('客户国家：' + previous['country'])
        inherited.append('country')
    if not metric and previous.get('metric') in METRIC_WORDS:
        additions.append('指标：' + METRIC_WORDS[previous['metric']])
        inherited.append('metric')
    if not group and not re.search(r'不分组|合计|汇总|总共|总额', question) and previous.get('dimension') in DIMENSION_WORDS:
        additions.append(DIMENSION_WORDS[previous['dimension']])
        inherited.append('dimension')
    return question + ('；' + '；'.join(additions) if additions else ''), inherited
