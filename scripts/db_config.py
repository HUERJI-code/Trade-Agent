# 文件作用：兼容旧的阶段 0 脚本。把项目根目录加入 Python 模块搜索路径，再转用阶段 1 的统一数据库连接实现，避免保留两套不同的连接逻辑。
# 阅读提示：中文注释仅解释现有实现；多行字符串的逐行说明放在字符串前，字符串原文保持不变。
"""Compatibility imports for the existing Phase 0 command-line scripts."""  # 说明这是兼容入口，供既有命令行脚本导入。
import sys  # 导入：sys 用于访问模块搜索路径、退出码或标准错误流。
from pathlib import Path  # 导入：Path 用于以对象方式拼接、读取和写入文件路径。

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 将项目根目录放到模块搜索路径最前面，使从 scripts 直接运行时也能找到 database 和 security 包。
from database.connection import ROOT, database_url, readonly_engine  # 从统一连接模块重新导出 ROOT、database_url、readonly_engine；本文件不再单独实现数据库配置。
