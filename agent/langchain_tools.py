"""向模型展示的工具目录；SQL 仍由已有业务工具生成。"""
from typing import Annotated, Literal  # 枚举限制取值，Annotated 把字段说明加入工具 schema。
from langchain.tools import tool  # 将 Python 函数转成带 JSON 参数结构的工具。
from tools.business_tool import run_metric as execute_metric  # 保留原有公式、SQL 校验和只读执行。


@tool
def run_metric(name: Literal['revenue', 'gross_profit', 'gross_margin', 'order_count', 'active_customer', 'quantity'],
               start: Annotated[str, '开始日期，格式 YYYY-MM-DD，包含当天'],
               end: Annotated[str, '结束日期，格式 YYYY-MM-DD，不包含当天'],
               country: Annotated[str | None, '只在问题指定某个国家时填英文名称。全部国家或仅要求按国家分组时必须为 null，不能填写 all、全部或任意国家。'] = None,
               dimension: Annotated[Literal['customer_country', 'product_category', 'order_month'] | None,
                   '按月统计必须选 order_month；按国家统计选 customer_country；按产品类别统计选 product_category；未要求分组用 null。'] = None) -> dict:
    """查询一个业务指标。start 包含当天，end 不含当天；country 为英文客户国家或 null 表示全部。

    revenue 是估算销售额，gross_profit 是估算毛利，gross_margin 是估算毛利率，
    order_count 是去重订单数，active_customer 是活跃客户数，quantity 是销量。
    条件缺失时先调用 clarify_query；不得虚构日期或提交 SQL。
    country 是国家筛选；只有用户明确要求分组才填写 dimension，否则 dimension=null。
    """
    return execute_metric(name, start=start, end=end, country=country, dimension=dimension)


@tool
def clarify_query(reason: Literal['missing_time', 'relative_time', 'missing_metric', 'ambiguous_scope']) -> dict:
    """用户条件不足或有歧义时请求补充，不查询数据库。"""
    return {'action': 'clarify', 'reason': reason}


@tool
def unsupported_query(reason: Literal['unsupported_request'] = 'unsupported_request') -> dict:
    """问题超出当前业务工具能力时说明不支持，不执行数据库操作。"""
    return {'action': 'unsupported', 'reason': reason}


QUERY_TOOLS = [run_metric, clarify_query, unsupported_query]  # 只有这个目录中的工具会展示给模型。
