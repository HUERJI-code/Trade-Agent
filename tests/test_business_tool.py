# 文件作用：阶段 2 验证：配置错误、时间边界、去重、比例计算以及真实 CSV 对账。SQLite 内存数据用于构造边界，integration 测试只读访问 MySQL。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
from copy import deepcopy  # 导入：deepcopy 用于复制嵌套配置，避免测试或调用者误改原对象。
import csv  # 导入：csv 用于读取带表头的 CSV 记录。
from datetime import datetime  # 导入：datetime 用于解析或表示日期时间。
from decimal import Decimal  # 导入：Decimal 用于精确表示十进制金额，避免普通浮点误差。
import io  # 导入：io 用于在内存中模拟文件，供 CSV 解析或输出捕获。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。
import sqlite3  # 导入：sqlite3 用于使用内存 SQLite 构造业务边界样例。
import zipfile  # 导入：zipfile 用于读取 ZIP 压缩包内的原始文件。

import pytest  # 导入：pytest 用于使用 fixture、参数化样例、异常断言与测试标记。
import yaml  # 导入：yaml 用于安全解析业务 YAML 配置。

from database.connection import ROOT, readonly_engine  # 导入：ROOT 用于取得项目根目录常量；readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。
from tools.business_tool import BusinessDictionary, BusinessError, run_metric  # 导入：BusinessDictionary 用于读取、校验指标并生成查询；BusinessError 用于断言或处理业务规则错误；run_metric 用于执行完整业务指标查询流程。
from tools.schema_tool import get_schema  # 导入：get_schema 用于读取并过滤真实数据库结构。
from tools.sql_tool import SQLTool  # 导入：SQLTool 用于使用受校验、受限制并记日志的 SQL 执行器。

@pytest.fixture  # 注册业务配置夹具，每个需要 config 的测试获得一份解析结果。
def config():  # 定义测试配置来源。
    return yaml.safe_load((ROOT/'config'/'business_metrics.yaml').read_text(encoding='utf-8'))  # 读取正式 YAML 并用安全加载器转成字典；这是测试夹具，实际业务入口还会执行更严格的加载检查。

def test_six_confirmed_metrics(config,schema):  # 验证六个确认指标的基本结构。
    book = BusinessDictionary(config,schema)  # 用配置和离线 schema 创建业务字典，触发完整校验。
    assert book.approved  # 正式配置应已确认。
    assert len(book.context()['metrics'])==6  # 当前正式指标数量应恰好为 6。
    assert book.get_metric('order_count')['expression']=='COUNT(DISTINCT s.order_number)'  # 订单数必须使用 COUNT DISTINCT，防止按明细行计数。
    assert 'NULLIF' in book.get_metric('gross_margin')['expression']  # 展开后的毛利率必须含 NULLIF，保护零分母。

def test_draft_can_be_reviewed_but_not_executed(schema):  # 验证草稿能被明确允许阅读，但始终不能用于生成可执行查询。
    path = ROOT/'config'/'business_metrics.template.yaml'  # 定位保留为 draft 的模板文件。
    with pytest.raises(BusinessError,match='confirmation'):  # 默认加载草稿应报 BusinessError，说明文本应含 confirmation。
        BusinessDictionary.load(schema,path)  # 尝试以默认参数加载草稿，触发拒绝。
    book = BusinessDictionary.load(schema,path,allow_draft=True)  # 明确 allow_draft=True 后可以加载用于查看。
    assert book.get_metric('revenue')['status']=='draft'  # 返回的指标信息仍应标注 draft。
    with pytest.raises(BusinessError) as error:  # 捕获尝试执行草稿产生的业务异常。
        book.query('revenue',start='2019-01-01',end='2020-01-01')  # 即使参数完整，草稿 query 也应失败。
    assert error.value.code=='approval_required'  # 检查准确错误码为 approval_required。

@pytest.mark.parametrize('payload',[  # 对下面三种危险/歧义 YAML 输入分别运行测试。
    'schema_version: 1\nschema_version: 2',  # 同一个 schema_version 写两次，必须拒绝，不能后值静默覆盖前值。
    'value: !!python/object/apply:os.system [echo should_not_run]',  # 带 Python 对象构造标签的 YAML 应被拒绝；这里是测试文本，不执行 os.system。
    'a: &x [1,2]\nb: *x',  # 包含锚点和别名的配置也应被拒绝。
])  # 结束参数化输入列表。
def test_invalid_yaml_rejected(payload,schema,tmp_path):  # 定义不合法 YAML 加载测试。
    path = tmp_path/'invalid.yaml'  # 在 pytest 临时目录中指定测试文件。
    path.write_text(payload,encoding='utf-8')  # 将当前恶意/歧义样例写到临时文件。
    with pytest.raises(BusinessError):  # 声明应抛出业务异常。
        BusinessDictionary.load(schema,path)  # 调用真实加载器验证它拒绝该文件。

@pytest.mark.parametrize('expression',[  # 分别用下面的错误公式测试配置校验。
    'SUM(s.made_up_column)', 'SUM(s.quantity),COUNT(*)',  # 不存在的列，以及一个配置公式偷偷包含两个投影，都应拒绝。
    'SUM(s.quantity); DROP TABLE sales', 's.quantity',  # 附加 DROP 语句或直接使用未聚合的 quantity，都应拒绝。
    '(SELECT COUNT(*) FROM sales)', 'COUNT(*) FROM sales',  # 公式里嵌套 SELECT 或夹带 FROM 子句都应拒绝。
])  # 结束错误公式清单。
def test_invalid_metric_formulas(expression,config,schema):  # 定义错误公式测试。
    config['metrics']['quantity']['expression'] = expression  # 把销量公式替换为本次错误样例；修改的是测试夹具，不是磁盘正式 YAML。
    with pytest.raises(BusinessError):  # 期望创建业务字典时抛错。
        BusinessDictionary(config,schema)  # 构造字典以触发验证。

def test_labels_distinct_counts_and_joins_are_checked(config,schema):  # 验证估算标签、订单去重、正确关联和依赖无环。
    for mutate in [  # 开始列出四个专门破坏配置的小函数。
        lambda c:c['metrics']['revenue'].update(label='销售额'),  # 去掉销售额标签中的“估算”，应被拒绝。
        lambda c:c['metrics']['order_count'].update(expression='COUNT(s.order_number)'),  # 将订单去重改成普通 COUNT，应被拒绝。
        lambda c:c['joins']['p'].update(on='s.product_key=p.category_key'),  # 把产品关联改到类别编号，可能放大行数，应被拒绝。
        lambda c:c['metrics']['gross_margin'].update(numerator='gross_margin',denominator='gross_margin'),  # 让毛利率的分子分母都引用自己，构成循环，必须拒绝。
    ]:  # 结束破坏函数清单并逐项遍历。
        bad = deepcopy(config)  # 每次都深拷贝一份原始配置，避免不同错误相互影响。
        mutate(bad)  # 执行当前破坏函数。
        with pytest.raises(BusinessError):  # 期望无效配置抛出业务错误。
            BusinessDictionary(bad,schema)  # 创建字典触发校验。
    schema['tables']['products']['primary_key'] = []  # 另外故意移除测试 schema 中产品表的主键。
    with pytest.raises(BusinessError):  # 关联唯一性无法保证时也应抛错。
        BusinessDictionary(config,schema)  # 用正常配置加错误 schema 验证关系检查。

@pytest.mark.parametrize('start,end',[(None,None),('2019-01-01',None),('2019-02-30','2020-01-01'),  # 参数化日期错误：全缺、只缺结束日、不存在的日期。
                                      ('2019-01-01','2019-01-01'),('2020-01-01','2019-01-01'),  # 再测试相同起止日及开始晚于结束。
                                      ('2019-1-1','2020-01-01')])  # 测试不补零的 2019-1-1，当前接口只接受严格 ISO 格式。
def test_missing_or_invalid_dates(start,end,config,schema):  # 定义日期错误测试。
    with pytest.raises(BusinessError):  # 这些输入都必须得到 BusinessError。
        BusinessDictionary(config,schema).query('revenue',start=start,end=end)  # 尝试生成查询，检验输入边界。

def test_unknown_metric_country_and_dimension(config,schema):  # 验证不认识的指标、国家和分组维度不被猜测执行。
    book = BusinessDictionary(config,schema)  # 创建有效业务字典。
    with pytest.raises(BusinessError):  # 声明未知指标应失败。
        book.get_metric('net_profit')  # 请求未定义的 net_profit，不能误当 gross_profit。
    for extra in [{'country':'Mars'},{'dimension':'customer_name'}]:  # 分别测试不存在的国家 Mars 和不允许的客户姓名维度。
        with pytest.raises(BusinessError):  # 要求业务层直接拒绝。
            book.query('order_count',start='2019-01-01',end='2020-01-01',**extra)  # 用 **extra 展开本次附加参数，触发对应检查。

def test_context_is_a_copy(config,schema):  # 验证 context 返回的是副本。
    book = BusinessDictionary(config,schema)  # 构造正式业务字典。
    other = book.context()  # 取出外部可访问的配置副本。
    other['metrics']['revenue']['label'] = 'wrong'  # 故意修改副本中的显示名。
    assert book.get_metric('revenue')['label']=='估算销售额'  # 内部标签必须仍为“估算销售额”，证明深拷贝隔离有效。

def test_ratio_weighting_zero_and_date_boundaries(config,schema):  # 构造少量内存数据验证去重、日期边界、加权毛利率和除零。
    # 注释：边界用 SQLite 内存库，不向 MySQL 教学库写测试记录。
    book = BusinessDictionary(config,schema)  # 创建业务字典用于生成相同规则的 SQL。
    with sqlite3.connect(':memory:') as db:  # 连接仅存在内存中的 SQLite 库；with 管理事务，这不是 MySQL。
        # 下方多行字符串第 1 行：开始多行 SQL，创建只含测试所需字段的客户表；三引号内部是字符串，不能插入 Python 行注释。
        # 下方多行字符串第 2 行：创建产品表，用 REAL 测简单小数运算；真实 MySQL 使用 DECIMAL，另有独立对账。
        # 下方多行字符串第 3 行：开始创建销售明细表，包含订单号、行号和客户编号。
        # 下方多行字符串第 4 行：补充产品、数量和日期，结束销售建表语句。
        # 下方多行字符串第 5 行：插入一名德国测试客户。
        # 下方多行字符串第 6 行：插入三种产品：售价/成本分别 10/9、100/0、0/0，用于不等权重和零分母场景。
        # 下方多行字符串第 7 行：同一张 2019 订单有两条不同产品明细，检验去重和汇总。
        # 下方多行字符串第 8 行：另插恰好位于排除终点 2020-01-01 的订单及 2018 零售价订单，结束内存 SQL 字符串。
        db.executescript('''CREATE TABLE customers(customer_key INTEGER,country TEXT);
        CREATE TABLE products(product_key INTEGER,unit_price_usd REAL,unit_cost_usd REAL);
        CREATE TABLE sales(order_number INTEGER,line_item INTEGER,customer_key INTEGER,
          product_key INTEGER,quantity INTEGER,order_date TEXT);
        INSERT INTO customers VALUES(1,'Germany');
        INSERT INTO products VALUES(1,10.0,9.0),(2,100.0,0.0),(3,0.0,0.0);
        INSERT INTO sales VALUES(1,1,1,1,1,'2019-01-01'),(1,2,1,2,1,'2019-01-01'),
          (2,1,1,2,1,'2020-01-01'),(3,1,1,3,1,'2018-01-01');''')
        def value(name,start='2019-01-01',end='2020-01-01'):  # 定义本测试内部的执行辅助函数，默认查询 2019 全年。
            sql = book.query(name,start=start,end=end).replace('`trade_agent_demo`.','')  # 生成 MySQL 规范 SQL，再去掉教学库前缀，以便此处的简单语法在 SQLite 执行；不是完整的方言兼容层。
            return db.execute(sql).fetchone()  # 执行并取唯一结果行，返回“指标值、匹配明细行数”。
        assert value('order_count')==(1,2)  # Excludes end boundary and deduplicates lines.  # 两条明细属于一张订单，终点当天排除，因此应为 1 单、2 条明细。
        assert value('active_customer')==(1,2)  # 这两条明细只有一位客户，因此 1 客户、2 明细。
        assert value('revenue')==(110.0,2)  # 销售额应为 10+100=110。
        assert value('gross_profit')==(101.0,2)  # 毛利应为 (10-9)+(100-0)=101。
        assert value('gross_margin')[0]==pytest.approx(101/110)  # 毛利率应为 101/110，用 approx 容忍 SQLite 浮点误差，不能平均 10% 和 100%。
        assert value('gross_margin','2018-01-01','2019-01-01')==(None,1)  # 2018 有一条零售价记录，分母 0，毛利率应为 None，匹配数为 1。
        assert value('gross_margin','2017-01-01','2018-01-01')==(None,0)  # 2017 没有明细，毛利率为 None，匹配数为 0。
        assert value('order_count','2017-01-01','2018-01-01')==(0,0)  # 同样没有明细时订单 COUNT 返回 0，而不是 None。

def csv_reference():  # 定义从原始 CSV 独立计算预期结果的函数。
    """Independent source calculation: no dictionary formula or SQL reused."""  # 函数说明：不复用 YAML 公式或 SQL，避免拿同一种实现验证自己。
    with zipfile.ZipFile(ROOT/'Data'/'archive.zip') as archive:  # 直接打开原始 ZIP。
        def rows(name,encoding='utf-8-sig'):  # 定义读取 ZIP 内 CSV 的辅助函数，默认 UTF-8 编码。
            return list(csv.DictReader(io.StringIO(archive.read(name).decode(encoding))))  # 读取成员字节、解码、交给 DictReader，转成记录列表。
        customers = {r['CustomerKey']:r for r in rows('Customers.csv','cp1252')}  # 客户文件用 cp1252，建立客户编号到整条客户记录的索引。
        products = {r['ProductKey']:r for r in rows('Products.csv')}  # 建立产品编号到产品记录的索引。
        selected = [r for r in rows('Sales.csv') if  # 开始筛选 Sales.csv 的销售记录。
                    datetime.strptime(r['Order Date'],'%m/%d/%Y').year==2019  # 按原始月/日/年解析下单日，只取 2019 年。
                    and customers[r['CustomerKey']]['Country']=='Germany']  # 通过客户编号找到客户国家，只保留 Germany。
    def money(value):  # 定义原始金额文本到 Decimal 的独立转换函数。
        return Decimal(value.replace('$','').replace(',','').strip())  # 去美元符号、千位逗号和首尾空白，保留十进制精度。
    revenue,profit,quantity = Decimal(0),Decimal(0),0  # 初始化销售额、毛利为 Decimal 零，销量为整数零。
    categories = {}  # 准备保存每个产品类别的金额与毛利。
    for row in selected:  # 逐条遍历所选销售明细。
        product,n = products[row['ProductKey']],int(row['Quantity'])  # 取对应产品和整数销售数量。
        price,cost = money(product['Unit Price USD']),money(product['Unit Cost USD'])  # 从源产品记录读取售价与成本并转换。
        revenue += n*price  # 独立累计数量乘标价得到估算销售额。
        profit += n*(price-cost)  # 独立累计数量乘价差得到估算毛利。
        quantity += n  # 累计销售件数。
        sums = categories.setdefault(product['Category'],[Decimal(0),Decimal(0)])  # 首次遇到类别时初始化两个 Decimal 累加器；已存在时取出原列表。
        sums[0] += n*price  # 为该类别累计销售额。
        sums[1] += n*(price-cost)  # 为该类别累计毛利。
    return {'revenue':revenue,'gross_profit':profit,'gross_margin':profit/revenue,  # 返回全局销售额、毛利、汇总毛利率。
            'order_count':len({r['Order Number'] for r in selected}),  # 用 Python 集合对原始订单号去重计数。
            'active_customer':len({r['CustomerKey'] for r in selected}),  # 用集合对原始客户编号去重计数。
            'quantity':quantity,'lines':len(selected),'categories':categories}  # 补上销量、明细数和分品类汇总，供 MySQL 对账。

@pytest.mark.integration  # 标记真实数据库集成测试。
def test_all_metrics_match_independent_csv(tmp_path):  # 定义六指标与原始 CSV 独立核对。
    expected = csv_reference()  # 先从原始文件算出预期值。
    engine = readonly_engine()  # 创建真实只读 MySQL 引擎。
    try:  # 保证清理资源。
        schema = get_schema(engine)  # 读取实际数据库结构。
        book = BusinessDictionary.load(schema)  # 加载正式指标字典。
        tool = SQLTool(engine,audit_path=tmp_path/'business.jsonl')  # 创建执行器，日志写临时文件。
        for name in book.context()['metrics']:  # 逐一核对正式字典中的所有指标。
            result = tool.run_sql(book.query(name,start='2019-01-01',end='2020-01-01',country='Germany'))  # 生成并执行相同条件的 2019 德国查询。
            actual,line_count = result.rows[0]  # 从结果第一行取指标值和匹配明细数。
            assert line_count==expected['lines']  # 明细数必须与原始 CSV 选出的行数一致。
            if name=='gross_margin':  # 毛利率需要处理数据库除法的小数舍入。
                assert abs(Decimal(actual)-expected[name]) <= Decimal('0.0000005')  # 用 Decimal 比较，允许不超过 0.0000005 的绝对比率误差。
            else:  # 其他金额与计数进入精确比较分支。
                assert Decimal(str(actual))==expected[name]  # 统一转 Decimal，要求与独立结果严格相同。
        result = tool.run_sql(book.query('gross_margin',start='2019-01-01',end='2020-01-01',country='Germany',dimension='product_category'))  # 另外按产品类别分组查询毛利率，验证分组后的汇总比例。
        assert len(result.rows)==len(expected['categories'])  # 数据库类别数应与源文件类别数一致。
        for category,margin,_ in result.rows:  # 逐行取类别与毛利率，忽略当前不需要的明细数。
            revenue,profit = expected['categories'][category]  # 取独立计算的该类别销售额和毛利。
            assert abs(Decimal(margin)-profit/revenue) <= Decimal('0.0000005')  # 对每个类别比较汇总毛利/销售额，同样容忍数据库小数舍入。
    finally:  # 无论断言是否通过都执行清理。
        engine.dispose()  # 释放引擎。

@pytest.mark.integration  # 标记需要 MySQL 的覆盖范围测试。
def test_coverage_and_required_time():  # 验证缺日期、完全无覆盖和部分覆盖三种情况。
    with pytest.raises(BusinessError) as caught:  # 捕获缺少日期的业务错误。
        run_metric('revenue')  # 不传日期调用 revenue，应该报错而非默认为所有时间。
    assert caught.value.code=='time_range_required'  # 检查错误码确为 time_range_required。
    outside = run_metric('order_count',start='2026-01-01',end='2027-01-01')  # 请求历史样例中不存在的 2026 全年订单数。
    assert outside['status']=='outside_data_coverage' and outside['rows']==[]  # 必须标为超出覆盖且 rows 为空，不能声称 2026 没有订单。
    partial = run_metric('order_count',start='2015-01-01',end='2017-01-01')  # 请求从样例开始前的 2015 到 2017，只有部分日期有数据。
    assert partial['data_coverage']['partial']  # 要求返回 partial=True，提示范围未被完全覆盖。
