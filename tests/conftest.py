# 文件作用：离线测试共享的 schema 夹具，不需要连接 MySQL。pytest 按参数名自动把 schema 注入测试函数。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
import pytest  # 导入：pytest 用于使用 fixture、参数化样例、异常断言与测试标记。

from scripts.prepare_data import SPECS, KEYS  # 导入：SPECS 用于复用导入阶段的表字段映射；KEYS 用于复用各表主键定义。
from security.permissions import ALLOWED_COLUMNS, DATABASE  # 导入：ALLOWED_COLUMNS 用于取得固定表/列白名单；DATABASE 用于取得固定教学数据库名。

@pytest.fixture  # 把下面函数注册为 fixture，测试函数声明 schema 参数即可取得一个新结构对象。
def schema():  # 定义测试用数据库结构，不从真实数据库读取。
    result = {'database':DATABASE,'tables':{  # 构造数据库名和表字典的外层结构。
        table:{'columns':[{'name':name,'type':dtype,'nullable':False}  # 为每张表构造字段名、类型、可空信息；此离线夹具统一用 nullable=False，不模拟所有真实空值属性。
                          for _,name,_,dtype in spec[1] if name in ALLOWED_COLUMNS[table]],  # 从导入 SPECS 中读取列，但仅保留运行期白名单允许的列。
               'primary_key':list(KEYS[table]),'foreign_keys':[]}  # 从 KEYS 取得主键；外键先置空。
        for table,spec in SPECS.items()}}  # 遍历全部表，结束嵌套字典推导式。
    result['tables']['sales']['foreign_keys'] = [  # 为 sales 添加测试需要的外键关系。
        {'columns':[key],'target_table':table,'target_columns':[key]}  # 每个关系指定来源列、目标表和目标列。
        for key,table in [('customer_key','customers'),('product_key','products'),('store_key','stores')]]  # 生成客户、产品、门店三条外键；此夹具未模拟汇率外键，真实结构测试另行检查四条外键。
    return result  # 将结构返回给请求此 fixture 的测试。
