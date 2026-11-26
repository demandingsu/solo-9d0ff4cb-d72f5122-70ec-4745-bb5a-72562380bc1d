"""运行配置：全部可通过环境变量覆盖。"""

from __future__ import annotations

import os


def _default_db_path() -> str:
    # 容器内由 compose 指向 /data/requisition.db；
    # 本地裸跑默认落到工作目录的 data/ 下。
    return os.getenv("DB_PATH", "data/requisition.db")


DB_PATH: str = _default_db_path()

# 是否开放 POST /api/admin/reset。Compose 演示环境打开，默认关闭。
ENABLE_RESET: bool = os.getenv("ENABLE_RESET", "0") == "1"

# SQLite 锁等待（秒）。确认单使用 BEGIN IMMEDIATE 串行化，
# 并发请求会在数据库锁上排队而不是立刻失败。
DB_BUSY_TIMEOUT_MS: int = int(os.getenv("DB_BUSY_TIMEOUT_MS", "30000"))
