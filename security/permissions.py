# 文件作用：阶段 1：固定的应用访问规则。数据库账号能读五张完整表；本文件进一步规定查询工具允许读取哪些列，以及查询的硬性资源上限。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Explicit teaching-data access policy; never sourced from model/user messages."""  # 说明访问规则由代码定义，不能由模型消息或普通用户查询临时扩大。
from dataclasses import dataclass  # 导入：dataclass 用于自动生成数据容器的初始化等方法。
from types import MappingProxyType  # 导入：MappingProxyType 用于创建不可直接修改的字典视图。

DATABASE = 'trade_agent_demo'  # 唯一允许的业务库名称。
READONLY_USER = 'trade_agent_readonly'  # 应用应使用的专用只读账号名。
ALLOWED_COLUMNS = MappingProxyType({  # MappingProxyType 将字典包装成只读映射；每张表的列用不可变元组保存。
    'customers': ('customer_key', 'country', 'continent'),  # customers 仅允许编号、国家、洲；姓名、生日、城市等没有出现在允许列表中。
    'products': ('product_key', 'product_name', 'brand', 'color', 'unit_cost_usd',  # products 开放编号、名称、品牌、颜色、单位成本。
                 'unit_price_usd', 'subcategory_key', 'subcategory', 'category_key', 'category'),  # 继续列出产品美元标价、子类别和大类别字段。
    'stores': ('store_key', 'country', 'state', 'square_meters', 'open_date'),  # stores 开放门店编号、国家、州、面积和开店日期。
    'exchange_rates': ('rate_date', 'currency_code', 'exchange_rate'),  # exchange_rates 开放汇率日期、币种和汇率值。
    'sales': ('order_number', 'line_item', 'order_date', 'delivery_date',  # sales 开放订单号、行号、下单日期和交付日期。
              'customer_key', 'store_key', 'product_key', 'quantity', 'currency_code'),  # 继续开放销售中的关联编号、数量和币种。
})  # 结束列白名单的字典及其只读包装。

@dataclass(frozen=True)  # dataclass 自动生成初始化等方法；frozen=True 禁止对象创建后普通方式修改字段。
class QueryPolicy:  # 定义查询策略类，将资源限制集中放在一起。
    max_rows: int = 2000  # 默认最多返回 2000 行；是输出行数上限，不等于最多扫描 2000 行。
    timeout_ms: int = 5000  # 默认查询超时 5000 毫秒，即 5 秒。
    max_sql_chars: int = 20000  # SQL 输入文本最大 20000 个字符。
    max_ast_nodes: int = 1000  # 解析后的语法树最大 1000 个节点，限制结构复杂度。
    max_table_references: int = 8  # 最多引用 8 次实体表；同一表多次关联会多次计数。
    max_offset: int = 10000  # 最大跳过 10000 行；深分页会增加数据库工作量。

    def __post_init__(self):  # dataclass 初始化后自动调用此函数，检查传入限制是否合法。
        for name, upper in [('max_rows',2000), ('timeout_ms',10000),  # 依次检查每个配置项；行数硬上限 2000、超时硬上限 10000 毫秒。
                            ('max_sql_chars',20000), ('max_ast_nodes',1000),  # SQL 文本和语法树节点也各有不可突破的硬上限。
                            ('max_table_references',8), ('max_offset',10000)]:  # 补齐表引用数与偏移量的硬上限。
            value = getattr(self,name)  # 按字段名取得当前策略对象的实际值。
            if type(value) is not int or not 1 <= value <= upper:  # 要求值严格为 int（因此排除 bool），并位于 1 到对应硬上限之间。
                raise ValueError(f'{name} is outside the supported range.')  # 越界或类型不对则抛错，调用者不能通过自定义策略无限放宽限制。

DEFAULT_POLICY = QueryPolicy()  # 创建默认策略对象，供连接、校验、执行三个模块共享。
