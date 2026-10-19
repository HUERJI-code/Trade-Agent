"""处理明确的国家最高/最低问题；规则识别公开标记，不伪装成模型推理。"""
from dataclasses import dataclass # 在原查询计划上补充排序方向。
from decimal import Decimal # 直接比较数据库十进制值，不按字符串或浮点数排序。
import re # 只接受完整、明确的句式，不能忽略额外筛选条件。
from agent.planner import QueryPlan, explicit_range # 沿用已确认的日期规则。
from tools.business_tool import BusinessError # 无法可靠比较时返回明确的业务错误。

METRIC_NAMES = {'销售额':'revenue', '销售金额':'revenue', '毛利':'gross_profit',
                '毛利率':'gross_margin', '订单数':'order_count', '活跃客户数':'active_customer',
                '客户数':'active_customer', '销量':'quantity', '件数':'quantity'}
COUNTRY_LABELS = {'Australia':'澳大利亚', 'Canada':'加拿大', 'France':'法国', 'Germany':'德国',
                  'Italy':'意大利', 'Netherlands':'荷兰', 'United Kingdom':'英国', 'United States':'美国'}

@dataclass(frozen=True)
class CountryExtremePlan(QueryPlan): # 只由下方受控解析器创建，模型不能输出额外排序参数。
    sort_order: str = 'desc' # desc 取最大值，asc 取最小值。

def parse_country_extreme(question):
    text = re.sub(r'\s+', '', question).strip('？?。！!') # 允许空格和末尾问号，保留其他条件文字。
    metric = '(?P<metric>'+'|'.join(sorted(METRIC_NAMES, key=len, reverse=True))+')'
    direction = '(?P<direction>最高|最低|最多|最少)'
    time = r'(?P<time>(?:\d{4}年(?:\d{1,2}月)?|今年|去年|前年|本月|上月)?)' # 先支持明确年/月；缺时间仍澄清。
    prefix = r'(?:请问|请告诉我|查询|请查询)?'+time+'[，,]?'
    patterns = [
        prefix+r'(?:哪个国家|哪一个国家|哪些国家|哪国)的?'+metric+direction+r'(?:呢|吗)?',
        prefix+metric+direction+r'的?(?:是|为)?(?:哪个国家|哪一个国家|哪些国家|哪国)',
        prefix+direction+r'的?'+metric+r'(?:来自|属于)(?:哪个国家|哪些国家|哪国)',
    ]
    match = next((found for pattern in patterns if (found := re.fullmatch(pattern, text))), None)
    if match is None:
        return None # 不把复杂问题简化成最高值；交回原有流程保守处理。
    period, reason = explicit_range(question)
    if reason:
        return QueryPlan('clarify', None, None, None, None, None, reason)
    order = 'desc' if match['direction'] in ('最高', '最多') else 'asc'
    return CountryExtremePlan('query', METRIC_NAMES[match['metric']], *period, None, 'customer_country', None, order)

def compare_countries(result, order):
    if order not in ('asc', 'desc') or result['dimension'] != 'customer_country' or result['country'] is not None:
        raise BusinessError('invalid_ranking', '国家比较必须使用全部客户国家的分组结果。')
    if result['limit_reached']:
        raise BusinessError('incomplete_ranking', '分组结果触及返回上限，不能可靠判断最高或最低国家。')
    rows = result['rows'] # 保留每个国家的汇总，不让前端把最高结果误当成全体。
    comparable = [(row, Decimal(str(row[1]))) for row in rows if row[1] is not None]
    if any(not value.is_finite() for _, value in comparable):
        raise BusinessError('invalid_ranking', '指标包含无效数值，无法比较。')
    comparable.sort(key=lambda pair: str(pair[0][0])) # 同值按国家名称稳定排序。
    comparable.sort(key=lambda pair: pair[1], reverse=order == 'desc')
    winners = [row for row, value in comparable if value == comparable[0][1]] if comparable else [] # 并列全部保留。
    return {**result, 'rows':[row for row, _ in comparable]+[row for row in rows if row[1] is None],
            'ranking':{'operation':'country_extreme', 'sort_order':order, 'winners':winners,
                       'compared_groups':len(comparable), 'null_groups':len(rows)-len(comparable),
                       'method':'Decimal comparison after complete grouped SQL result'}} # SQL 原文仍是真实分组查询。
