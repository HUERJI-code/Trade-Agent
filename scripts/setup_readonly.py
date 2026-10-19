# 文件作用：阶段 0：一次性创建应用专用只读账号，将随机密码保存到 .env。它使用管理员身份配置权限；Agent 日常运行使用创建后的只读身份。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""One-time local provisioning, separate from Agent runtime access."""  # 说明这是一次性账号配置，与运行期查询分离。
import argparse  # 导入：argparse 用于解析终端中的 --参数。
import getpass  # 导入：getpass 用于在终端输入密码时不回显。
import secrets  # 导入：secrets 用于生成难以预测的随机只读密码。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。

import pymysql  # 导入：pymysql 用于与 MySQL 通信，也提供驱动异常类型。

ROOT = Path(__file__).resolve().parents[1]  # 取得项目根目录，用于保存 .env。
DB = 'trade_agent_demo'  # 固定允许配置的教学数据库。
USER = 'trade_agent_readonly'  # 固定应用账号名称。
HOST = '127.0.0.1'  # 固定本机地址；MySQL 账号同时包含用户名和主机部分。
TABLES = ('customers','products','stores','exchange_rates','sales')  # 明确只读账号被授予 SELECT 的五张表。

def main():  # 定义一次性账号创建入口。
    parser = argparse.ArgumentParser(description=__doc__)  # 创建命令行参数解析器。
    parser.add_argument('--empty-root-password',action='store_true')  # --empty-root-password 表示本机 root 的密码为空，不是让新只读账号使用空密码。
    args = parser.parse_args()  # 解析命令行开关。
    env_file = ROOT/'.env'  # 指定项目中的 .env 路径。
    if env_file.exists():  # 检测配置文件是否已经存在。
        raise SystemExit('.env already exists; no account or configuration changes made.')  # 已有配置就退出，避免覆盖现有凭据。
    root_password = '' if args.empty_root_password else getpass.getpass('MySQL root password: ')  # 根据开关使用空 root 密码，或通过不回显的 getpass 提示输入。
    password = secrets.token_urlsafe(32)  # 生成基于 32 字节随机数据的 URL 安全文本密码；长度不等于恰好 32 个字符。
    try:  # 开始捕获数据库配置错误。
        with pymysql.connect(host=HOST,user='root',password=root_password,port=3306,  # 用本机 root 连接 MySQL；此管理员连接只用于初始化账号。
                             connect_timeout=5,read_timeout=10,write_timeout=10,autocommit=True) as admin:  # 设置连接、读、写超时；autocommit=True 使授权相关操作及时提交。
            with admin.cursor() as cursor:  # 打开数据库游标，用它发送参数化 SQL。
                cursor.execute('SELECT 1 FROM mysql.user WHERE User=%s',(USER,))  # 按用户名检查账号是否已存在；%s 参数由驱动处理，(USER,) 是单元素元组。
                if cursor.fetchone():  # 查到任何记录都认为账号已存在。
                    raise SystemExit('Account already exists; no password or grants changed.')  # 退出，不更改已有账号的密码或权限。
                cursor.execute('SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA=%s',(DB,))  # 查询目标库中现有表名，数据库名作为参数传入。
                if {row[0] for row in cursor.fetchall()} != set(TABLES):  # 实际表名集合必须恰好等于五张预期表。
                    raise SystemExit('Expected imported tables are missing or changed.')  # 结构不符合预期则停止初始化。
                # 注释：先保存新凭据再创建账号，以免后续错误导致丢失随机密码。
                # 注释：使用独占创建，保护已经存在的配置。
                with env_file.open('x',encoding='utf-8') as config:  # 'x' 模式只创建新文件；即使检查后有人创建了 .env，这里也不会覆写它。
                    config.write(f'DB_HOST={HOST}\nDB_PORT=3306\nDB_USER={USER}\nDB_PASSWORD={password}\nDB_NAME={DB}\n')  # 向 .env 写入五个连接字段；字符串中的反斜杠 n 代表换行。此讲解不读取生成后的真实密码。
                cursor.execute('CREATE USER %s@%s IDENTIFIED BY %s',(USER,HOST,password))  # 创建固定用户@固定主机，并通过参数传递密码。
                for table in TABLES:  # 逐一处理允许读取的五张表。
                    cursor.execute(f'GRANT SELECT ON `{DB}`.`{table}` TO %s@%s',(USER,HOST))  # 只授予该表 SELECT；表名来自固定列表，用户和主机通过参数传递。
        print('Created trade_agent_readonly@127.0.0.1 with SELECT on five tables only.')  # 输出账号创建成功的说明。
        print('Generated password saved to .env; password is not printed.')  # 只说明密码保存位置，不打印密码内容。
    except pymysql.MySQLError as error:  # 捕获 PyMySQL 的数据库错误。
        # 注释：不直接打印可能带 SQL 或密码的原始异常。
        code = error.args[0] if error.args else 'unknown'  # 只取错误编号；没有编号则用 unknown。
        raise SystemExit(f'Provisioning failed (MySQL code {code}). Keep .env if created; diagnose before retrying.') from None  # 安全地退出并提示保留已创建配置排查；from None 隐藏底层异常链。建账号与写文件不是同一个原子事务。

if __name__ == '__main__':  # 直接运行该脚本才执行入口。
    main()  # 调用 main 完成一次性配置。
