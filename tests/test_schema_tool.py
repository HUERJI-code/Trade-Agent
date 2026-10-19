# 文件作用：用模拟数据库反射器验证结构过滤。这里只测 schema 工具，不连接真实 MySQL。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
from unittest.mock import Mock, patch  # 导入：Mock 用于模拟数据库反射器等对象，不进行真实访问；patch 用于在测试期间临时替换函数、路径或命令行参数。

import pytest  # 导入：pytest 用于使用 fixture、参数化样例、异常断言与测试标记。

from scripts.prepare_data import SPECS  # 导入：SPECS 用于复用导入阶段的表字段映射。
from tools.schema_tool import get_schema  # 导入：get_schema 用于读取并过滤真实数据库结构。

def fake_inspector():  # 定义创建假 Inspector 的辅助函数。
    inspector = Mock()  # Mock 可以模拟方法调用和返回值，不需要真实数据库。
    inspector.get_table_names.return_value = list(SPECS)  # 让表名查询返回 SPECS 中的五张表。
    inspector.get_columns.side_effect = lambda table,**kwargs: [  # 让 get_columns 根据调用传入的表名动态返回字段列表；side_effect 可以是函数。
        {'name':name,'type':dtype,'nullable':False} for _,name,_,dtype in SPECS[table][1]]  # 用导入映射构造字段，模拟时全部设置不可空。
    inspector.get_pk_constraint.return_value = {'constrained_columns':[]}  # 此组测试不关心主键，模拟为空列表。
    inspector.get_foreign_keys.return_value = []  # 同样将外键模拟为空列表。
    return inspector  # 返回已经配置好的假反射器。

def test_metadata_filters_private_customer_fields():  # 测试客户个人字段会从工具输出中被过滤。
    inspector = fake_inspector()  # 创建模拟反射器。
    with patch('tools.schema_tool.inspect',return_value=inspector):  # 在 with 范围内将 schema_tool.inspect 替换为返回假对象的函数。
        result = get_schema(Mock())  # 调用真实 get_schema 逻辑，但底层使用 Mock，因此不连接数据库。
    assert {c['name'] for c in result['tables']['customers']['columns']} == {'customer_key','country','continent'}  # 要求客户表最终只暴露编号、国家、洲。
    assert len(result['tables']) == 5  # 要求仍包含五张业务表。

def test_missing_required_column_is_not_invented():  # 测试真实结构缺字段时工具应该失败，而不是猜测字段。
    inspector = fake_inspector()  # 创建新的假反射器。
    inspector.get_columns.side_effect = lambda *args,**kwargs: []  # 故意让所有表的列查询都返回空列表。
    with patch('tools.schema_tool.inspect',return_value=inspector),pytest.raises(ValueError):  # 同时替换 inspect，并声明接下来必须抛出 ValueError。
        get_schema(Mock())  # 触发 get_schema，若未抛异常测试就失败。
