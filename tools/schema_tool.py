# 文件作用：阶段 1：从真实数据库读取结构，再按列白名单生成工具可见的 schema。schema 是表、字段和关系的说明，不是客户数据记录。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Read real metadata, then expose only approved business columns."""  # 说明先读真实元数据，再过滤为业务允许的列。
from sqlalchemy import inspect  # 导入：inspect 用于反射真实数据库表、列、主键和外键。

from security.permissions import ALLOWED_COLUMNS, DATABASE  # 导入：ALLOWED_COLUMNS 用于取得固定表/列白名单；DATABASE 用于取得固定教学数据库名。

def get_schema(engine):  # 定义 schema 工具，参数 engine 是数据库连接引擎。
    inspector = inspect(engine)  # 创建 SQLAlchemy Inspector，用数据库反射功能读取实际结构。
    available = set(inspector.get_table_names(schema=DATABASE))  # 查询教学库中的表名，转成集合。
    if not set(ALLOWED_COLUMNS) <= available:  # 集合 <= 表示子集；要求允许的业务表全都存在，不因额外表存在就将其开放。
        raise ValueError('Required business tables are missing from the live database.')  # 缺表时失败，不凭空生成不存在的表。
    result = {'database':DATABASE,'tables':{}}  # 准备输出结构：数据库名和按表名索引的表信息。
    for table, allowed in ALLOWED_COLUMNS.items():  # 只遍历固定白名单中的表及其允许字段。
        columns = {c['name']:c for c in inspector.get_columns(table,schema=DATABASE)}  # 读取该表实际列信息，整理为以列名为键的字典。
        if not set(allowed) <= columns.keys():  # 要求全部允许列都真实存在。
            raise ValueError('Required business columns are missing from the live database.')  # 缺少必要字段时失败，不猜测字段。
        safe_columns = [{'name':name,'type':str(columns[name]['type']).split(' COLLATE')[0],  # 仅复制允许列的名称与类型；从类型显示文本中去掉 COLLATE 部分，方便 SQL 解析器使用。
                         'nullable':bool(columns[name]['nullable'])} for name in allowed]  # 保存列是否允许为空；按白名单顺序生成 safe_columns。
        pk = inspector.get_pk_constraint(table,schema=DATABASE).get('constrained_columns') or []  # 读取主键包含哪些列；没有值时使用空列表。
        relationships = []  # 准备安全外键关系列表。
        for fk in inspector.get_foreign_keys(table,schema=DATABASE):  # 逐条处理数据库返回的真实外键。
            target = fk['referred_table']  # 取外键指向的目标表。
            if (fk.get('referred_schema') in (None,DATABASE) and target in ALLOWED_COLUMNS  # 目标库必须为本库或未另指定，目标表必须也在白名单中。
                    and set(fk['constrained_columns']) <= set(allowed)  # 外键的来源列必须全部可见。
                    and set(fk['referred_columns']) <= set(ALLOWED_COLUMNS[target])):  # 外键的目标列也必须全部可见；三项条件同时满足才公布关系。
                relationships.append({'columns':fk['constrained_columns'],'target_table':target,  # 记录外键来源字段和目标表。
                                      'target_columns':fk['referred_columns']})  # 记录目标字段，完成关系对象。
        result['tables'][table] = {'columns':safe_columns,  # 为当前表保存过滤后的列。
                                   'primary_key':pk if set(pk) <= set(allowed) else [],  # 只有主键所有字段都允许可见，才返回主键清单，否则返回空列表。
                                   'foreign_keys':relationships}  # 保存通过过滤的外键关系。
    return result  # 返回完整、经过过滤的结构字典。
