# 文件作用：阶段 0：读取 ZIP 内的 CSV，检查数据，再生成建库和导入 SQL。输入是原始压缩包；输出是 SQL 和 JSON 报告。本文件运行时不连接 MySQL。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Validate the Kaggle archive and build a MySQL 8 import; never connect to a DB."""  # 模块说明：验证 Kaggle 数据包、生成 MySQL 8 导入文件，不连接数据库。
import csv  # 导入：csv 用于读取带表头的 CSV 记录。
import hashlib  # 导入：hashlib 用于计算 SHA-256 文件或查询指纹。
import io  # 导入：io 用于在内存中模拟文件，供 CSV 解析或输出捕获。
import json  # 导入：json 用于在 Python 对象与 JSON 文本之间转换。
import zipfile  # 导入：zipfile 用于读取 ZIP 压缩包内的原始文件。
from datetime import datetime  # 导入：datetime 用于解析或表示日期时间。
from decimal import Decimal  # 导入：Decimal 用于精确表示十进制金额，避免普通浮点误差。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。

ROOT = Path(__file__).resolve().parents[1]  # __file__ 是当前脚本路径；resolve() 得到绝对路径；parents[1] 从 scripts 向上取项目根目录。ROOT 不依赖终端当前目录。
ARCHIVE = ROOT / 'Data' / 'archive.zip'  # Path 的 / 表示拼接路径；得到原始压缩包的位置。
OUT = ROOT / 'Data' / 'prepared'  # 指定生成的 SQL 和数据检查报告放在 Data/prepared 中。
DB = 'trade_agent_demo'  # 固定教学数据库名称；它同时用于生成 SQL 和报告。
# 说明下面每个字段用四项描述：CSV 原名、数据库列名、Python 转换方式、MySQL 类型。
SPECS = {  # 建立 SPECS 字典，作为五张表的建表与转换规则。
    'customers': ('Customers.csv', [  # customers 对应 Customers.csv；后面的列表依次列出字段映射。
        ('CustomerKey','customer_key','int','INT'), ('Gender','gender','str','VARCHAR(20)'),  # 客户编号转整数存 INT；性别保留字符串，数据库最多 20 个字符。
        ('Name','customer_name','str','VARCHAR(255)'), ('City','city','str','VARCHAR(255)'),  # 姓名和城市分别改为 customer_name、city，保留字符串，最多 255 个字符。导入这些列不代表未来模型可以读取它们。
        ('State Code','state_code','str','VARCHAR(50)'), ('State','state','str','VARCHAR(255)'),  # 州代码与州名称保留文本；代码最长 50 字符、名称最长 255 字符。
        ('Zip Code','zip_code','str','VARCHAR(50)'), ('Country','country','str','VARCHAR(100)'),  # 邮编按字符串保存，避免丢失开头的 0；国家列最长 100 字符。
        ('Continent','continent','str','VARCHAR(100)'), ('Birthday','birthday','date','DATE')],  # 洲名称保留文本；生日由原始月/日/年转换为数据库 DATE。这里结束客户字段列表。
        'PRIMARY KEY (customer_key)'),  # 客户编号是主键：同一客户编号不能重复，不能为空。
    'products': ('Products.csv', [  # products 对应 Products.csv，开始定义产品字段。
        ('ProductKey','product_key','int','INT'), ('Product Name','product_name','str','VARCHAR(255)'),  # 产品编号转 INT；产品名称保留最长 255 字符的文本。
        ('Brand','brand','str','VARCHAR(100)'), ('Color','color','str','VARCHAR(50)'),  # 品牌最多 100 字符，颜色最多 50 字符。
        ('Unit Cost USD','unit_cost_usd','decimal','DECIMAL(12,2)'),  # 单位美元生产成本用 Decimal 转换，存 DECIMAL(12,2)：共 12 位数字，其中 2 位小数。
        ('Unit Price USD','unit_price_usd','decimal','DECIMAL(12,2)'),  # 单位美元标价采用相同的精确小数类型。它不是销售表中的实际成交价。
        ('SubcategoryKey','subcategory_key','int','INT'), ('Subcategory','subcategory','str','VARCHAR(255)'),  # 子类别编号转整数，子类别名称保留文本。
        ('CategoryKey','category_key','int','INT'), ('Category','category','str','VARCHAR(255)')],  # 大类别编号和名称分别存整数、文本；结束产品字段列表。
        'PRIMARY KEY (product_key), CHECK (unit_cost_usd >= 0), CHECK (unit_price_usd >= 0)'),  # 产品编号唯一，并在数据库层要求成本和标价都不能小于 0。
    'stores': ('Stores.csv', [  # stores 对应 Stores.csv，开始定义门店。
        ('StoreKey','store_key','int','INT'), ('Country','country','str','VARCHAR(100)'),  # 门店编号转整数，门店国家保留文本；门店国家与客户国家是两个不同口径。
        ('State','state','str','VARCHAR(255)'), ('Square Meters','square_meters','int','INT'),  # 门店州名保留文本；门店面积转整数，空面积由后面的 NULLABLE 允许保留。
        ('Open Date','open_date','date','DATE')], 'PRIMARY KEY (store_key)'),  # 开店时间转 DATE；门店编号作为主键。
    'exchange_rates': ('Exchange_Rates.csv', [  # exchange_rates 对应 Exchange_Rates.csv。
        ('Date','rate_date','date','DATE'), ('Currency','currency_code','str','VARCHAR(3)'),  # 汇率日期转 DATE；币种代码保留 3 字符文本，例如 USD。
        ('Exchange','exchange_rate','decimal','DECIMAL(18,8)')],  # 汇率用 DECIMAL(18,8)，保留 8 位小数；结束汇率字段列表。
        'PRIMARY KEY (rate_date, currency_code), CHECK (exchange_rate > 0)'),  # 日期和币种共同组成主键，同一天同一币种只能一条；汇率必须大于 0。
    'sales': ('Sales.csv', [  # sales 对应 Sales.csv；每行代表订单中的一条商品明细，不一定是一整张订单。
        ('Order Number','order_number','int','INT'), ('Line Item','line_item','int','INT'),  # 订单号与行号都转整数，稍后共同组成主键。
        ('Order Date','order_date','date','DATE'), ('Delivery Date','delivery_date','date','DATE'),  # 下单日和交付日都转 DATE；交付日允许缺失，下单日不允许。
        ('CustomerKey','customer_key','int','INT'), ('StoreKey','store_key','int','INT'),  # 客户编号、门店编号转整数，用于关联对应的表。
        ('ProductKey','product_key','int','INT'), ('Quantity','quantity','int','INT'),  # 产品编号和购买数量转整数。
        ('Currency Code','currency_code','str','VARCHAR(3)')],  # 订单币种保留三字符代码；结束销售字段列表。
        'PRIMARY KEY (order_number, line_item), '  # 订单号加行号共同唯一；同一订单可以有多行，因此不能将明细行数当作订单数。
        'FOREIGN KEY (customer_key) REFERENCES customers(customer_key), '  # 销售的客户编号必须在 customers 表中存在。相邻字符串会被 Python 自动拼接。
        'FOREIGN KEY (product_key) REFERENCES products(product_key), '  # 销售的产品编号必须在 products 表中存在。
        'FOREIGN KEY (store_key) REFERENCES stores(store_key), '  # 销售的门店编号必须在 stores 表中存在。
        'FOREIGN KEY (order_date, currency_code) REFERENCES exchange_rates(rate_date, currency_code), '  # 下单日期加币种必须能匹配汇率表的一条记录；这里只定义关联，没有进行金额换算。
        'CHECK (quantity > 0)'),  # 数据库要求销售数量大于 0；结束 sales 的配置。
}  # 结束整个 SPECS 字典。
NULLABLE = {('sales','delivery_date'), ('stores','square_meters')}  # 只有销售交付日期、门店面积允许为空；用“表名、列名”二元组防止同名字段混淆。
KEYS = {'customers': ('customer_key',), 'products': ('product_key',), 'stores': ('store_key',),  # 为客户、产品、门店记录主键字段；单元素元组末尾的逗号不可省略。
        'exchange_rates': ('rate_date','currency_code'), 'sales': ('order_number','line_item')}  # 为汇率与销售记录复合主键，供 Python 在导入前检查重复。

def require(condition, message):  # 定义 require：传入一个判断结果和错误说明，用来集中处理数据校验失败。
    if not condition:  # 如果条件为假，就执行下一行。
        raise ValueError(message)  # 抛出 ValueError，停止本次准备，避免继续生成貌似正常的数据文件。

def convert(value, kind):  # 定义转换函数：value 是 CSV 文本，kind 是 int/date/decimal/str 等转换标记。
    value = value.strip()  # 去掉文本前后的空格。
    if not value:  # 空字符串或只有空白的原始值进入此分支。
        return None  # 用 Python 的 None 表示缺失；稍后生成 SQL 时变成 NULL。
    if kind == 'int':  # 检查是否要求转为整数。
        return int(value)  # 转为 Python int；非法整数会抛错。
    if kind == 'date':  # 检查是否要求转为日期。
        return datetime.strptime(value, '%m/%d/%Y').date().isoformat()  # 按月/日/年解析，取日期部分，再转为 YYYY-MM-DD。2 月 30 日这样的无效日期会报错。
    if kind == 'decimal':  # 检查是否为精确小数类型。
        return Decimal(value.replace('$', '').replace(',', ''))  # 去掉美元符号和千位逗号，用 Decimal 保存精确十进制金额，避免浮点误差。
    return value  # 其他类型原样返回经过 strip 的字符串，例如邮编 00123。

def literal(value):  # 定义 literal：把一个已转换的 Python 值编码成 SQL 的值表达式。
    if value is None:  # 检查值是否是缺失值 None。
        return 'NULL'  # 返回 SQL 关键字 NULL；不是带引号的字符串 'NULL'。
    if isinstance(value, (int, Decimal)):  # 整数与 Decimal 可以作为数字字面量写入 SQL。
        return str(value)  # 将数字转成十进制文本，例如 1234.50。
    # 说明文本采用 UTF-8 十六进制表示，避免引号、反斜杠、分号改变 SQL 结构。
    return "CONVERT(X'" + value.encode('utf-8').hex() + "' USING utf8mb4)" if value else "''"  # 非空文本先编码为 UTF-8 字节再转十六进制，MySQL 用 CONVERT(... USING utf8mb4) 还原；空文本返回 SQL 空字符串。

def prepare():  # 定义整个数据准备流程，只有被调用才会实际读取和写入文件。
    tables, profile = {}, {}  # tables 保存各表转换后的行；profile 保存各表的行数、编码、哈希和空值数量。
    with zipfile.ZipFile(ARCHIVE) as archive:  # 打开 ZIP；with 结束时关闭压缩包句柄。不会将整个 ZIP 解压到任意目录。
        for table, (filename, columns, _) in SPECS.items():  # 按配置顺序处理每张表；_ 表示当前循环不使用建表约束文本。
            raw = archive.read(filename)  # 读取 ZIP 内指定 CSV 成员的原始字节。
            encoding = 'utf-8-sig'  # 首先尝试兼容 UTF-8 BOM 的 utf-8-sig 编码。
            try:  # 开始捕获本次解码可能出现的异常。
                content = raw.decode(encoding)  # 把原始字节解码为字符串。
            except UnicodeDecodeError:  # 只有 UTF-8 解码失败，才进入备用编码分支。
                encoding = 'cp1252'  # 备用使用 cp1252，适用于此数据包的 Customers.csv。
                content = raw.decode(encoding)  # 重新解码；若仍失败，错误向外传播，不悄悄替换字符。
            reader = csv.DictReader(io.StringIO(content))  # StringIO 把字符串包装成文件对象；DictReader 将每条 CSV 记录变成以表头为键的字典。
            require(reader.fieldnames == [c[0] for c in columns], f'{filename}: unexpected columns')  # 检查表头名称及顺序与 SPECS 完全一致；c[0] 取每项配置的 CSV 字段名。
            rows = [{name: convert(source[field], kind) for field, name, kind, _ in columns} for source in reader]  # 嵌套推导式：逐条读取记录，再逐字段重命名、转换，最终得到由数据库字段字典组成的列表。
            require(bool(rows), f'{filename}: empty file')  # 要求 CSV 至少有一条数据记录。
            for row in rows:  # 逐行检查转换后的数据。
                for _, name, _, dtype in columns:  # 逐列取数据库列名和类型，跳过当前不需要的配置元素。
                    value = row[name]  # 取当前行当前字段的值。
                    require(value is not None or (table,name) in NULLABLE, f'{table}.{name}: unexpected null')  # 缺失值只有出现在 NULLABLE 中才允许。
                    if value is not None and dtype.startswith('VARCHAR'):  # 如果字段非空且是 VARCHAR，检查文本长度。
                        require(len(value) <= int(dtype[8:-1]), f'{table}.{name}: text too long')  # dtype[8:-1] 从 VARCHAR(255) 提取 255；超过容量就报错，不让数据库截断。
                    if isinstance(value, Decimal):  # Decimal 值进入金额/汇率精度检查。
                        precision, scale = map(int, dtype[8:-1].split(','))  # 从 DECIMAL(12,2) 取出 precision=12、scale=2。
                        require(value.is_finite() and value >= 0, f'{table}.{name}: invalid decimal')  # 要求小数是有限数且非负，排除 NaN、无穷大和负值。
                        require(value < 10 ** (precision-scale) and value == value.quantize(Decimal(1).scaleb(-scale)), f'{table}.{name}: decimal overflow/rounding')  # 限制整数部分范围，并验证按声明的小数位数取整后值不变；避免导入时溢出或被舍入。
            unique = {tuple(r[k] for k in KEYS[table]) for r in rows}  # 用每行的主键组成元组，再放入集合自动去重。
            require(len(unique) == len(rows), f'{table}: duplicate primary key')  # 去重后主键数必须等于记录数，否则存在重复主键。
            tables[table] = rows  # 保存当前表所有转换后的记录，供外键校验和 SQL 输出使用。
            profile[table] = {'file':filename, 'rows':len(rows), 'encoding':encoding,  # 记录源文件名、记录数和实际采用的编码。
                              'sha256':hashlib.sha256(raw).hexdigest(),  # 计算当前 CSV 字节的 SHA-256 指纹，供追踪数据版本。
                              'nulls':{c[1]:sum(r[c[1]] is None for r in rows) for c in columns}}  # 逐列统计值为 None 的行数；布尔值 True 在 sum 中按 1 计数。
    for field, parent in [('customer_key','customers'),('product_key','products'),('store_key','stores')]:  # 依次检查销售表的客户、产品、门店外键关系。
        allowed = {r[field] for r in tables[parent]}  # 提取父表所有合法编号组成集合，提高后续查找效率。
        require(all(r[field] in allowed for r in tables['sales']), f'sales: missing {parent} reference')  # 要求每条销售明细的编号都在父表中；all 要求所有判断都为真。
    rates = {(r['rate_date'],r['currency_code']) for r in tables['exchange_rates']}  # 建立所有“汇率日期、币种”组合的集合。
    require(all((r['order_date'],r['currency_code']) in rates for r in tables['sales']), 'sales: missing exchange rate')  # 要求每条销售的下单日和币种能在汇率表找到对应组合。
    require(all(r['quantity'] > 0 for r in tables['sales']), 'sales: nonpositive quantity')  # 销售数量必须全部为正数。
    require(all(r['exchange_rate'] > 0 for r in tables['exchange_rates']), 'nonpositive exchange rate')  # 汇率必须全部为正数；前面的 Decimal 通用检查只排除了负数，这里额外排除 0。
    sales = tables['sales']  # 给销售行列表起一个简短变量名，方便生成汇总统计。
    summary = {'database': DB, 'source':'https://www.kaggle.com/datasets/bhavikjikadara/global-electronics-retailers',  # 建立整体报告，记录目标数据库和数据来源网址；该行不访问网络。
               'archive_sha256':hashlib.sha256(ARCHIVE.read_bytes()).hexdigest(),  # 为整个原始 ZIP 计算 SHA-256 指纹。
               'tables':profile, 'orders':len({r['order_number'] for r in sales}),  # 保存各表画像，并对订单号去重计算真实订单数。
               'date_min':min(r['order_date'] for r in sales), 'date_max':max(r['order_date'] for r in sales),  # 计算下单日最早和最晚值；统一 ISO 日期文本可按字典顺序比较。
               'countries':sorted({r['country'] for r in tables['customers']}),  # 从客户表提取国家、去重、排序。
               'quantity_total':sum(r['quantity'] for r in sales),  # 合计所有销售数量，不是合计销售明细行数。
               'validation':'CSV types, nulls, unique keys and foreign keys passed; MySQL import not yet run.'}  # 注明这些是文件侧检查，尚未证明 MySQL 导入成功。
    OUT.mkdir(parents=True, exist_ok=True)  # 创建输出目录；parents=True 会创建缺失的父目录，exist_ok=True 允许目录已存在。
    with (OUT/'import_global_electronics.sql').open('w',encoding='utf-8',newline='\n') as sql:  # 以 UTF-8 覆写生成 SQL 文件，固定换行为 LF；with 负责关闭文件。
        sql.write('-- Generated teaching-data import. Use scripts/import_mysql.py in batch mode.\n')  # 向 SQL 写入说明注释，指明推荐的导入脚本。
        sql.write('-- Existing database causes an error; do not use mysql --force.\n')  # 写入已有数据库应报错停止的提醒；没有生成 DROP DATABASE。
        sql.write("SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';\n")  # 生成严格 SQL 模式，要求数据库对无效日期、溢出等问题报错；这一刻只是写文本。
        sql.write(f'CREATE DATABASE `{DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_cs;\nUSE `{DB}`;\n')  # 生成建库和选择库语句；utf8mb4 支持完整 Unicode，as_cs 表示重音与大小写敏感；已有同名库会失败。
        for table, (_, columns, constraints) in SPECS.items():  # 依次取每张表的列和约束，开始生成 CREATE TABLE。
            fields = ',\n'.join(f'  `{name}` {dtype}' + (' NULL' if (table,name) in NULLABLE else ' NOT NULL') for _,name,_,dtype in columns)  # 拼出每列的名称、类型和 NULL/NOT NULL；反引号包住固定字段名。
            sql.write(f'CREATE TABLE `{table}` (\n{fields},\n  {constraints}\n) ENGINE=InnoDB;\n')  # 写入 CREATE TABLE，附主键、外键、检查约束，使用支持事务和外键的 InnoDB。
        sql.write('START TRANSACTION;\n')  # 写入数据插入事务的开始语句；前面的建库建表 DDL 不会因此变成可统一回滚。
        for table, (_, columns, _) in SPECS.items():  # 再次按表顺序处理，父表先于销售表插入，以满足外键。
            names = ','.join(f'`{c[1]}`' for c in columns)  # 按配置顺序生成 INSERT 列名清单。
            for start in range(0,len(tables[table]),200):  # 每 200 行生成一批 INSERT，避免一条语句过大。
                values = ',\n'.join('('+','.join(literal(r[c[1]]) for c in columns)+')' for r in tables[table][start:start+200])  # 取当前批次，逐行逐字段用 literal 编码，拼成多组括号包围的值。
                sql.write(f'INSERT INTO `{table}` ({names}) VALUES\n{values};\n')  # 写入本批 INSERT 语句；此处没有执行数据库写入。
        sql.write('COMMIT;\n')  # 全部 INSERT 后写入 COMMIT，供导入时提交事务。
        checks = []  # 建立用于导入后核对行数的 SQL 列表。
        for table in SPECS:  # 为每张业务表生成一条核对查询。
            checks.append(f"SELECT '{table}' AS table_name, COUNT(*) AS actual_rows, {len(tables[table])} AS expected_rows, COUNT(*) = {len(tables[table])} AS passed FROM `{table}`")  # 查询实际行数，与本次 CSV 行数比较；passed 为 1 代表该表行数一致。
        sql.write('\nUNION ALL\n'.join(checks)+';\n')  # 用 UNION ALL 合并五张表的核对结果，并写到 SQL 文件尾部。
        sql.write('SELECT COUNT(DISTINCT order_number) AS distinct_orders, MIN(order_date) AS first_order, MAX(order_date) AS last_order, SUM(quantity) AS quantity_total FROM sales;\n')  # 再生成去重订单数、最早日期、最晚日期和销量总数的核对查询。
    summary['sql_sha256'] = hashlib.sha256((OUT/'import_global_electronics.sql').read_bytes()).hexdigest()  # SQL 文件关闭后读取其字节计算指纹，后续导入脚本会校验此指纹。
    (OUT/'data_profile.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')  # 将完整画像保存为格式化 JSON，中文保持原样。
    print(json.dumps(summary,ensure_ascii=False,indent=2))  # 把同样的报告打印到终端供检查。
    return summary  # 返回 summary 字典，其他 Python 代码也能调用并取得结果。

if __name__ == '__main__':  # 仅当此文件是直接运行入口时，才执行下一行；被 import 时不会自动准备数据。
    prepare()  # 调用 prepare，执行文件验证与 SQL 生成流程。
