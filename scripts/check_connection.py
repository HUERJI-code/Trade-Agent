# 文件作用：阶段 0/1：真实数据库冒烟检查。验证账号和权限、五表记录数、订单与日期总量，再用恒假条件验证写操作被数据库权限拒绝。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Live smoke test: expected data, narrow grants and harmless write-denial probes."""  # 说明它检查真实环境，写入探针即使被允许也匹配零行。
import sys  # 导入：sys 用于访问模块搜索路径、退出码或标准错误流。

from sqlalchemy import text  # 导入：text 用于将固定 SQL 包装为 SQLAlchemy 可执行语句。
from sqlalchemy.exc import DBAPIError  # 导入：DBAPIError 用于捕获底层数据库驱动错误。

from db_config import readonly_engine  # 导入：readonly_engine 用于创建带只读身份、权限和会话检查的数据库引擎。

EXPECTED = {'customers':15266,'products':2517,'stores':67,'exchange_rates':11215,'sales':62884}  # 固定该版本数据集的预期记录数；更换数据集后这些验收基准需要重新确认。

def check():  # 定义数据库检查入口。
    engine = readonly_engine()  # 创建统一只读引擎。
    try:  # 保证检查结束后清理连接池。
        with engine.connect() as connection:  # 打开一个真实连接。
            identity = connection.execute(text('SELECT CURRENT_USER(), DATABASE()')).one()  # 读取实际身份和数据库；text 将固定 SQL 包装为 SQLAlchemy 可执行对象；one 要求恰好一行。
            if tuple(identity) != ('trade_agent_readonly@127.0.0.1','trade_agent_demo'):  # 校验身份和库名完全匹配。
                raise ValueError('Unexpected database identity.')  # 不匹配就停止。
            grants = {row[0] for row in connection.execute(text('SHOW GRANTS'))}  # 读取授权，将各行文本组成集合。
            account = '`trade_agent_readonly`@`127.0.0.1`'  # 固定授权文本中的账号表示。
            allowed = {f'GRANT USAGE ON *.* TO {account}'} | {  # 允许账号 USAGE，即账号存在。
                f'GRANT SELECT ON `trade_agent_demo`.`{table}` TO {account}' for table in EXPECTED}  # 补上五张表各自的 SELECT，除此之外不应有其他授权。
            if grants != allowed:  # 精确比较权限集合。
                raise ValueError('Grants differ from the expected table-only SELECT privileges.')  # 发现与预期不同就失败。
            print(f'PASS identity and grants: {identity[0]} / {identity[1]}')  # 打印身份和权限检查通过，不打印密码。
            for table, expected in EXPECTED.items():  # 遍历各表及对应的预期行数。
                actual = connection.execute(text(f'SELECT COUNT(*) FROM `{table}`')).scalar_one()  # 执行 COUNT(*) 得到实际行数；表名来自固定 EXPECTED 字典。
                if actual != expected:  # 实际行数必须等于预期。
                    raise ValueError(f'{table}: row count mismatch.')  # 不一致则抛错。
                print(f'PASS {table}: {actual} rows')  # 打印当前表通过及行数。
            result = tuple(connection.execute(text('SELECT COUNT(DISTINCT order_number), MIN(order_date), MAX(order_date), SUM(quantity) FROM sales')).one())  # 一次读取去重订单数、最早日期、最晚日期、总销量。
            if (result[0],str(result[1]),str(result[2]),int(result[3])) != (26326,'2016-01-01','2021-02-20',197757):  # 将日期转文本、数量转整数后，与此数据版本的四个基准比较。
                raise ValueError('Order/date/quantity reconciliation failed.')  # 有任一差异就认为核对失败。
            print('PASS 26326 distinct orders; 2016-01-01 to 2021-02-20; 197757 units')  # 打印总量核对通过。
        # 注释：后面的探针全都匹配零行，即便权限意外放开也不改实际记录。
        # 注释：不发送 DROP/ALTER/CREATE 等结构变更探针。
        probes = {  # 建立三个写入拒绝探针。
            'UPDATE':'UPDATE sales SET quantity=quantity WHERE 1=0',  # UPDATE 的 WHERE 1=0 恒为假；SET quantity=quantity 也不会改变值。
            'DELETE':'DELETE FROM sales WHERE 1=0',  # DELETE 同样匹配零行，不会删除现有记录。
            'INSERT':'INSERT INTO sales SELECT * FROM sales WHERE 1=0',  # INSERT 从 sales 中选零行，所以不会插入记录；这里是受信任固定诊断 SQL，未通过用户 SQL 校验器。
        }  # 结束探针字典。
        for name, sql in probes.items():  # 逐个测试写入语句是否被拒绝。
            with engine.connect() as connection:  # 每次探针单独取连接。
                try:  # 捕获预期的数据库权限错误。
                    connection.execute(text(sql))  # 发送探针 SQL；我们期望这一行报拒绝，而非执行成功。
                except DBAPIError as error:  # 捕获数据库驱动异常。
                    if error.orig.args[0] != 1142:  # 只接受 MySQL 1142（命令被拒绝）作为权限验证证据。
                        raise ValueError(f'{name}: unexpected error, not proof of permission rejection.') from None  # 其他错误，例如语法或连接失败，不能证明只读权限有效。
                    print(f'PASS {name} denied by MySQL (1142)')  # 打印该操作确实被 MySQL 权限拒绝。
                else:  # 如果没有异常，说明权限探针竟然被允许。
                    connection.rollback()  # 回滚当前事务。
                    raise ValueError(f'{name} was allowed unexpectedly; probe affected zero rows.')  # 报告配置不符合只读预期，并说明探针没有影响记录。
        print('All live connection checks passed.')  # 所有检查通过时输出成功。
    finally:  # 无论检查成功与否都执行清理。
        engine.dispose()  # 释放连接池。

if __name__ == '__main__':  # 脚本直接运行时进入以下处理。
    try:  # 捕获运行入口的错误。
        check()  # 实际执行 check。
    except DBAPIError as error:  # 数据库错误只展示编号。
        print(f'Database check failed (MySQL code {error.orig.args[0]}). Check local configuration.',file=sys.stderr)  # 向标准错误流打印可安全展示的排查提示。
        sys.exit(1)  # 以失败码 1 退出。
    except ValueError as error:  # 处理程序自己定义的核对错误。
        print(str(error),file=sys.stderr)  # 输出这些不含凭据的核对说明。
        sys.exit(1)  # 以失败码退出。
