"""SQLite 连接、建表与事务辅助。

并发正确性的关键点：
* 每个请求一个独立连接（FastAPI 同步端点在线程池中运行）；
* 试配使用一个显式 ``BEGIN`` 只读事务，所有读取落在同一快照上，
  对应需求里的“按同一时点的余量和领料状态计算”；
* 确认使用 ``BEGIN IMMEDIATE``，在事务一开始就拿到写锁，
  “检查样书领料状态 + 检查余量 + 整单扣料/标记”在同一个串行化
  事务内完成，冲突整体 ROLLBACK。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

from .config import DB_BUSY_TIMEOUT_MS, DB_PATH

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    page_count  INTEGER NOT NULL CHECK (page_count > 0),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_pages (
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page_no     INTEGER NOT NULL,
    edition_no  TEXT NOT NULL,
    PRIMARY KEY (book_id, page_no)
);

CREATE TABLE IF NOT EXISTS samples (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id               INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    code                  TEXT NOT NULL,
    bound_to_page         INTEGER NOT NULL CHECK (bound_to_page >= 0),
    status                TEXT NOT NULL DEFAULT 'available'
                              CHECK (status IN ('available', 'requisitioned')),
    requisition_order_id  INTEGER,
    created_at            TEXT NOT NULL,
    UNIQUE (book_id, code)
);
CREATE INDEX IF NOT EXISTS idx_samples_book ON samples(book_id);

CREATE TABLE IF NOT EXISTS piles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id             INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    code                TEXT NOT NULL,
    start_page          INTEGER NOT NULL CHECK (start_page >= 1),
    end_page            INTEGER NOT NULL,
    page_editions       TEXT NOT NULL,  -- JSON 数组，第 0 项对应 start_page
    copies_total        INTEGER NOT NULL CHECK (copies_total >= 0),
    copies_remaining    INTEGER NOT NULL CHECK (copies_remaining >= 0),
    created_at          TEXT NOT NULL,
    UNIQUE (book_id, code)
);
CREATE INDEX IF NOT EXISTS idx_piles_book ON piles(book_id);

-- 试配方案留痕（只是方案，不占纸、不改领料状态），方便 /docs 一键确认与重发。
CREATE TABLE IF NOT EXISTS trial_plans (
    plan_id               TEXT PRIMARY KEY,
    book_id               INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    fingerprint           TEXT NOT NULL,  -- 草稿可重复、可同指纹；唯一约束只在已确认单上
    sample_count          INTEGER NOT NULL,
    request_json          TEXT NOT NULL,  -- 可直接 POST /api/requisitions 的请求体
    result_json           TEXT NOT NULL,  -- 完整试配结果（逐本取帖单）
    created_at            TEXT NOT NULL,
    confirmed_order_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trial_plans_fp ON trial_plans(fingerprint);

CREATE TABLE IF NOT EXISTS requisition_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no        TEXT NOT NULL UNIQUE,
    book_id         INTEGER NOT NULL REFERENCES books(id),
    request_id      TEXT,
    fingerprint     TEXT NOT NULL UNIQUE,  -- 同方案重发的幂等键
    status          TEXT NOT NULL DEFAULT 'confirmed',
    sample_count    INTEGER NOT NULL,
    total_pieces    INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    response_json   TEXT NOT NULL DEFAULT ''
);
-- 客户端自带 request_id 时的唯一约束（仅对非空值）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_request_id
    ON requisition_orders(request_id) WHERE request_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS requisition_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES requisition_orders(id) ON DELETE CASCADE,
    sample_id   INTEGER NOT NULL REFERENCES samples(id),
    pile_id     INTEGER NOT NULL REFERENCES piles(id),
    quantity    INTEGER NOT NULL CHECK (quantity > 0),
    start_page  INTEGER NOT NULL,
    end_page    INTEGER NOT NULL,
    UNIQUE (order_id, sample_id, pile_id)
);
CREATE INDEX IF NOT EXISTS idx_items_order ON requisition_items(order_id);

-- 扣料流水，用于核对余量。
CREATE TABLE IF NOT EXISTS stock_movements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES requisition_orders(id),
    pile_id         INTEGER NOT NULL REFERENCES piles(id),
    delta           INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_pile ON stock_movements(pile_id);
"""


def connect() -> sqlite3.Connection:
    """新建一个自动提交模式的连接，事务由代码里的显式 BEGIN 控制。

    FastAPI 同步 yield 依赖的 __enter__/端点/__exit__ 可能被 anyio
    分派到线程池中的不同工作线程，因此这里必须 check_same_thread=False。
    每个请求独占一个连接，且进入/使用/退出是串行的，不存在同连接并发访问；
    确认事务另由 BEGIN IMMEDIATE + 条件更新保证跨请求互斥。
    """
    conn = sqlite3.connect(
        DB_PATH,
        timeout=DB_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,  # 自动提交；显式 BEGIN / COMMIT
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(DB_BUSY_TIMEOUT_MS)}")
    return conn


def init_db() -> None:
    path = Path(DB_PATH)
    if path.parent and str(path.parent) not in ("", "."):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        # WAL：读写不互相阻塞；即使文件系统不支持也只是退回普通回滚日志，
        # BEGIN IMMEDIATE + busy_timeout 仍然保证确认事务串行化。
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一连接，请求结束关闭。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False) -> Iterator[None]:
    """简单事务上下文。immediate=True 时先取写锁。"""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def run_immediate(
    conn: sqlite3.Connection,
    work: Callable[[sqlite3.Connection], T],
    *,
    retries: int = 5,
) -> T:
    """在 ``BEGIN IMMEDIATE`` 事务中执行 work。

    遇到 SQLite 写锁冲突（busy_timeout 之后仍 locked）时，整体回滚并重试，
    work 必须是可重放的纯事务体。HTTPException 等业务异常直接向上抛，
    由调用方转成 409/422，且已经完成 ROLLBACK。
    """
    delay = 0.02
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = work(conn)
            conn.execute("COMMIT")
            return result
        except Exception as exc:  # noqa: BLE001 - 必须先回滚再决定重试/抛出
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            last_error = exc
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.5)
                    continue
            raise
    assert last_error is not None
    raise last_error
