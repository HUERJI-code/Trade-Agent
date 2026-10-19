# 文件作用：阶段 0：生成结构报告，展示真实表、字段、约束与固定聚合统计，不输出客户个人记录。文件尾部部分文字是阶段 0 时的进度快照，不能当成当前阶段的进度。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Generate Phase 0 metadata report with fixed, bounded aggregate queries."""  # 模块说明：只用固定且返回量受限的统计生成阶段 0 报告。
from datetime import datetime  # 导入：datetime 用于解析或表示日期时间。
import sys  # 导入：sys 用于访问模块搜索路径、退出码或标准错误流。

from sqlalchemy import inspect, text  # 导入：inspect 用于反射真实数据库表、列、主键和外键；text 用于将固定 SQL 包装为 SQLAlchemy 可执行语句。
from sqlalchemy.exc import DBAPIError  # 导入：DBAPIError 用于捕获底层数据库驱动错误。

from db_config import ROOT, readonly_engine  # 导入：ROOT 用于取得项目根目录常量；readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。
from prepare_data import SPECS  # 导入：SPECS 用于复用导入阶段的表字段映射。

def generate_report():  # 定义生成报告的入口。
    engine = readonly_engine()  # 创建统一只读引擎。
    lines = ['# 教学数据库结构报告','',f'检查时间：{datetime.now().isoformat(timespec="seconds")}（本机时间）。',  # 准备 Markdown 行列表，写标题及本机检查时间，精确到秒。
             '','来源：用户提供的 Kaggle Global Electronics Retailers 数据包。以下结构与统计直接读取本机 MySQL 的 trade_agent_demo。',  # 写数据来源和本机数据库名称。
             '','使用 trade_agent_readonly，只展示字段、约束和聚合统计，不展示客户个人记录。',  # 说明使用只读账号，只展示结构与聚合，不展示客户行记录。
             '','## 表结构与源字段映射','']  # 写表结构章节标题与空行。
    sensitive = {'customer_name','birthday','city','zip_code','state_code','state'}  # 定义在客户表报告中标注的个人信息候选列；它是报告标记列表，不等于运行期白名单。
    try:  # 开始数据库读取，保证随后清理。
        inspector = inspect(engine)  # 创建真实数据库反射器。
        if set(inspector.get_table_names()) != set(SPECS):  # 要求实际表集合与导入规则中的表集合恰好一致。
            raise ValueError('Unexpected schema; report not generated.')  # 结构不符就拒绝生成报告。
        with engine.connect() as connection:  # 打开连接执行固定统计查询。
            for table, (source, mappings, _) in SPECS.items():  # 遍历每张表的源文件和字段映射。
                fields = {name:original for original,name,_,_ in mappings}  # 把映射反转为“数据库列名→原始 CSV 表头”，用于报告对照。
                count = connection.execute(text(f'SELECT COUNT(*) FROM `{table}`')).scalar_one()  # 读取当前表实际行数。
                pk = inspector.get_pk_constraint(table)['constrained_columns']  # 读取当前表真实主键字段。
                lines += [f'### {table}','',f'来源 {source}；记录数 {count:,}；主键：{", ".join(pk)}。','',  # 向报告追加表名、来源、千位分隔的记录数与主键。
                          '| 数据库字段 | 原始字段 | 类型 | 可为空 | 客户个人信息候选 |',  # 定义 Markdown 表格的列标题。
                          '|---|---|---|---|---|']  # 写表格分隔行。
                for col in inspector.get_columns(table):  # 逐列读取数据库实际列信息。
                    flag = '是' if table == 'customers' and col['name'] in sensitive else ''  # 仅当是客户表且列位于 sensitive 集合时标注“是”。
                    lines.append(f'| {col["name"]} | {fields[col["name"]]} | {col["type"]} | {"是" if col["nullable"] else "否"} | {flag} |')  # 生成一行表格，含数据库字段、原始名、类型、是否可空及候选标记。
                lines.append('')  # 追加空行，结束当前表字段表格。
                for fk in inspector.get_foreign_keys(table):  # 遍历实际外键约束。
                    lines.append(f'- 外键：{", ".join(fk["constrained_columns"])} → {fk["referred_table"]}({", ".join(fk["referred_columns"])})。')  # 把外键写成来源列→目标表(列)的人类可读形式。
                lines.append('')  # 追加空行分隔。
            for title, sql in [  # 开始执行固定的四类聚合核对查询。
                ('订单日期范围','SELECT MIN(order_date), MAX(order_date), COUNT(DISTINCT order_number) FROM sales'),  # 查询销售日期范围与去重订单数。
                ('交付日期缺失条数','SELECT COUNT(*) FROM sales WHERE delivery_date IS NULL'),  # 统计缺失交付日期的销售明细行数；缺失不自动表示未完成。
                ('门店面积缺失条数','SELECT COUNT(*) FROM stores WHERE square_meters IS NULL'),  # 统计缺失面积的门店数。
                ('汇率日期范围','SELECT MIN(rate_date), MAX(rate_date) FROM exchange_rates')]:  # 查询汇率日期范围，结束查询清单。
                values = connection.execute(text(sql)).one()  # 执行当前查询，取得唯一结果行。
                lines.append(f'- {title}：'+ ' / '.join(map(str,values))+'。')  # 将结果值转成文本，以斜杠分隔写入报告。
            countries = connection.execute(text('SELECT DISTINCT country FROM customers ORDER BY country LIMIT 50')).scalars().all()  # 列出最多 50 个去重客户国家，排序后读取首列。
            currencies = connection.execute(text('SELECT DISTINCT currency_code FROM sales ORDER BY currency_code LIMIT 20')).scalars().all()  # 列出最多 20 个去重订单币种。
            lines += [f'- 客户国家：{", ".join(countries)}。',f'- 订单币种：{", ".join(currencies)}。']  # 将国家和币种清单加入报告。
    finally:  # 读取成功或失败都会执行清理。
        engine.dispose()  # 释放连接池。
    lines += ['','## 分析边界与待确认口径','',  # 数据库读取完成后追加分析边界章节。
              '- 订单明细主键为订单号与行号，订单数需去重，不能用明细行数代替。',  # 提醒订单明细与订单数的区别。
              '- 产品表金额单位为 USD。unit_price_usd 是标价；销售表没有实际成交价。数量乘标价是已确认的教学估算口径，展示时必须标注估算。',  # 提醒产品标价与实际成交额的区别。
              '- unit_cost_usd 为单位生产成本；缺少历史成本、折扣、税费、运费与退货记录。无法直接确认净利润或成本变动原因。',  # 解释成本字段不完整，不能直接确认净利润或原因。
              '- 已确认按产品 USD 标价计算，不按订单币种再次折算；汇率表已导入但当前指标未启用换汇。',  # 提醒不要对已有美元标价重复换汇。
              '- 日期是 2016 至 2021 年的历史样例；2026 年等区间不能编造答案。',  # 说明样例历史日期范围，不能捏造其他年份。
              '- 国家筛选已确认使用客户国家，下单日期用于时间筛选；交付日期为空不等于订单未完成。',  # 正式口径保存在 config/business_metrics.yaml。
              '- 客户姓名、生日、详细位置等字段已由应用工具白名单屏蔽；数据库只读账号本身仍可读五张完整教学表。',  # 数据库表级只读与应用字段白名单是两层不同的限制。
              '- 本报告仅核对数据结构与聚合统计；当前应用实现进度请查看 README.md 与各阶段验收报告。','']  # 结构报告不再写死应用阶段，避免重新生成时输出过期进度。
    path = ROOT/'Documents'/'schema_report.md'  # 确定报告输出到 Documents/schema_report.md。
    path.write_text('\n'.join(lines),encoding='utf-8')  # 用换行拼接 Markdown 内容并覆写报告。
    print('Generated Documents/schema_report.md from the live readonly database.')  # 输出生成成功的提示。

if __name__ == '__main__':  # 直接运行此脚本时才执行入口。
    try:  # 捕获数据库错误。
        generate_report()  # 调用报告生成函数。
    except DBAPIError as error:  # 处理底层数据库错误；这里没有单独捕获 ValueError。
        print(f'Inspection failed (MySQL code {error.orig.args[0]}). No report written.',file=sys.stderr)  # 输出数据库错误编号并说明本次没有写出报告；旧报告文件可能仍在。
        sys.exit(1)  # 以失败码退出。
