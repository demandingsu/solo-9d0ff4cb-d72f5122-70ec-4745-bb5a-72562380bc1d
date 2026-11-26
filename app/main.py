"""样书散帖领料 API —— FastAPI 入口。

演示主线（/docs 上可直接 Try it out）：
1. ``POST /api/demo/seed`` 一键造演示数据；
2. ``POST /api/books/{book_id}/trial-matches`` 试配，查看最多同时补齐的样书与取帖单；
3. ``POST /api/plans/{plan_id}/confirm`` 确认试配方案（整单扣料、标记已领料）；
4. 再次调用同一确认地址，返回原领料单（幂等重发）；
5. 交错确认/争用冲突会返回 409，整单退回，余量与领料状态不变。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import services
from .config import ENABLE_RESET
from .db import get_db, init_db, run_immediate, transaction
from .schemas import (
    BookCreate,
    ConfirmRequest,
    ConfirmResponse,
    PileBatchCreate,
    PileCreate,
    SampleBatchCreate,
    TrialRequest,
    TrialResult,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Compose 演示用：空库首次启动时自动种入一份争用样例。
    import os

    if os.getenv("AUTO_SEED", "0") == "1":
        from .db import connect as _connect

        conn = _connect()
        try:
            empty = conn.execute("SELECT COUNT(*) AS c FROM books").fetchone()["c"] == 0
            if empty:
                with transaction(conn, immediate=True):
                    services.seed_demo(conn)
        finally:
            conn.close()
    yield


app = FastAPI(
    title="样书散帖领料 API",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "装订员用散帖补齐样书的领料服务。\n\n"
        "- 录入：全书逐页目标版号、各本样书已配末页、各垛散帖的连续页码/逐页版号/份数\n"
        "- 试配：整帖取用、从缺页连续接到末页、逐页版号相符；用最大流求**最多能同时补齐**的样书与逐本取帖单\n"
        "- 试配只读同一时点的余量与领料状态，**不占纸**\n"
        "- 确认：纸够且单上样书均未领料才整单扣料并标记；冲突整单退回\n"
        "- 幂等：同 request_id 或同方案指纹重发，返回原领料单\n\n"
        "推荐演示：先调 `POST /api/demo/seed`，再依次试配、确认、重发。"
    ),
)


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
@app.get("/", tags=["meta"], summary="服务与演示指引")
def root() -> dict[str, Any]:
    return {
        "service": "sample-book-signature-requisition",
        "docs": "/docs",
        "health": "/health",
        "demo_flow": [
            "POST /api/demo/seed",
            "POST /api/books/{book_id}/trial-matches",
            "POST /api/plans/{plan_id}/confirm",
            "POST /api/requisitions （直接确认；重发同方案返回原单）",
        ],
    }


@app.get("/health", tags=["meta"], summary="健康检查")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return {"status": "ok", "journal_mode": journal, "reset_enabled": ENABLE_RESET}


# --------------------------------------------------------------------------- #
# 图书与目标版号
# --------------------------------------------------------------------------- #
@app.post("/api/books", status_code=201, tags=["基础录入"], summary="录入图书与全书逐页目标版号")
def create_book(payload: BookCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    now = services.utcnow_iso()
    with transaction(conn, immediate=True):
        cur = conn.execute(
            "INSERT INTO books (title, page_count, created_at) VALUES (?, ?, ?)",
            (payload.title, len(payload.page_versions), now),
        )
        book_id = cur.lastrowid
        for page_no, edition in enumerate(payload.page_versions, start=1):
            conn.execute(
                "INSERT INTO book_pages (book_id, page_no, edition_no) VALUES (?, ?, ?)",
                (book_id, page_no, edition),
            )
    return get_book(book_id, conn)


@app.get("/api/books", tags=["基础录入"], summary="图书列表")
def list_books(conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM books ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/books/{book_id}", tags=["基础录入"], summary="图书详情（含逐页目标版号）")
def get_book(book_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(404, detail={"code": "book_not_found", "book_id": book_id})
    pages = [
        {"page_no": r["page_no"], "edition_no": r["edition_no"]}
        for r in conn.execute(
            "SELECT page_no, edition_no FROM book_pages WHERE book_id = ? ORDER BY page_no",
            (book_id,),
        )
    ]
    return {**dict(book), "page_versions": [p["edition_no"] for p in pages], "pages": pages}


# --------------------------------------------------------------------------- #
# 样书
# --------------------------------------------------------------------------- #
@app.post(
    "/api/books/{book_id}/samples",
    status_code=201,
    tags=["基础录入"],
    summary="批量录入样书已配末页",
)
def create_samples(
    book_id: int,
    payload: SampleBatchCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(404, detail={"code": "book_not_found", "book_id": book_id})

    now = services.utcnow_iso()
    created: list[dict[str, Any]] = []
    with transaction(conn, immediate=True):
        n_pages = int(
            conn.execute(
                "SELECT page_count FROM books WHERE id = ?", (book_id,)
            ).fetchone()["page_count"]
        )
        existing_codes = {
            r["code"]
            for r in conn.execute(
                "SELECT code FROM samples WHERE book_id = ?", (book_id,)
            ).fetchall()
        }
        base = conn.execute(
            "SELECT COUNT(*) AS c FROM samples WHERE book_id = ?", (book_id,)
        ).fetchone()["c"]
        for idx, s in enumerate(payload.samples, start=1):
            code = s.code or f"YB-{int(base) + idx:03d}"
            if code in existing_codes:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "duplicate_sample_code", "sample_code": code},
                )
            if s.bound_to_page > n_pages:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "bound_page_out_of_range",
                        "message": "已配末页不能大于全书总页数",
                        "sample_code": code,
                        "bound_to_page": s.bound_to_page,
                        "page_count": n_pages,
                    },
                )
            cur = conn.execute(
                """
                INSERT INTO samples (book_id, code, bound_to_page, status, created_at)
                VALUES (?, ?, ?, 'available', ?)
                """,
                (book_id, code, s.bound_to_page, now),
            )
            existing_codes.add(code)
            created.append(
                {
                    "id": cur.lastrowid,
                    "book_id": book_id,
                    "code": code,
                    "bound_to_page": s.bound_to_page,
                    "status": "available",
                }
            )
    return {"created_count": len(created), "samples": created}


@app.get("/api/books/{book_id}/samples", tags=["基础录入"], summary="样书列表（含领料状态）")
def list_samples(book_id: int, conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT s.*, o.order_no AS requisition_order_no
        FROM samples s
        LEFT JOIN requisition_orders o ON o.id = s.requisition_order_id
        WHERE s.book_id = ?
        ORDER BY s.id
        """,
        (book_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# 散帖垛
# --------------------------------------------------------------------------- #
def _create_pile(conn: sqlite3.Connection, book_id: int, pile: PileCreate, fallback_code: str) -> dict[str, Any]:
    book = conn.execute("SELECT page_count FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(404, detail={"code": "book_not_found", "book_id": book_id})
    n_pages = int(book["page_count"])
    end_page = pile.start_page + len(pile.page_versions) - 1
    if end_page > n_pages:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pile_pages_out_of_range",
                "message": f"散帖页码 {pile.start_page}–{end_page} 超出全书 {n_pages} 页",
            },
        )
    code = pile.code or fallback_code
    try:
        cur = conn.execute(
            """
            INSERT INTO piles
                (book_id, code, start_page, end_page, page_editions,
                 copies_total, copies_remaining, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                book_id,
                code,
                pile.start_page,
                end_page,
                json.dumps(pile.page_versions),
                pile.copies,
                pile.copies,
                services.utcnow_iso(),
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "duplicate_pile_code", "pile_code": code},
        ) from exc
    row = conn.execute("SELECT * FROM piles WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _pile_dict(row)


def _pile_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["page_editions"] = json.loads(d["page_editions"])
    return d


@app.post("/api/books/{book_id}/piles", status_code=201, tags=["基础录入"], summary="录入一垛散帖")
def create_pile(
    book_id: int, payload: PileCreate, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    with transaction(conn, immediate=True):
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM piles WHERE book_id = ?", (book_id,)
        ).fetchone()["c"]
        return _create_pile(conn, book_id, payload, f"D-{int(count) + 1:02d}")


@app.post(
    "/api/books/{book_id}/piles/batch",
    status_code=201,
    tags=["基础录入"],
    summary="批量录入散帖垛",
)
def create_piles_batch(
    book_id: int,
    payload: PileBatchCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    created = []
    with transaction(conn, immediate=True):
        base = conn.execute(
            "SELECT COUNT(*) AS c FROM piles WHERE book_id = ?", (book_id,)
        ).fetchone()["c"]
        for i, pile in enumerate(payload.piles, start=1):
            created.append(_create_pile(conn, book_id, pile, f"D-{int(base) + i:02d}"))
    return {"created_count": len(created), "piles": created}


@app.get("/api/books/{book_id}/piles", tags=["基础录入"], summary="散帖垛列表（含当前余量）")
def list_piles(book_id: int, conn: sqlite3.Connection = Depends(get_db)) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM piles WHERE book_id = ? ORDER BY id", (book_id,)
    ).fetchall()
    return [_pile_dict(r) for r in rows]


@app.get("/api/piles/{pile_id}/movements", tags=["基础录入"], summary="某垛散帖的扣料流水")
def pile_movements(pile_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    pile = conn.execute("SELECT * FROM piles WHERE id = ?", (pile_id,)).fetchone()
    if pile is None:
        raise HTTPException(404, detail={"code": "pile_not_found", "pile_id": pile_id})
    movements = [
        dict(r)
        for r in conn.execute(
            """
            SELECT sm.id, sm.order_id, o.order_no, sm.delta,
                   sm.balance_after, sm.created_at
            FROM stock_movements sm
            JOIN requisition_orders o ON o.id = sm.order_id
            WHERE sm.pile_id = ?
            ORDER BY sm.id
            """,
            (pile_id,),
        )
    ]
    return {"pile": _pile_dict(pile), "movements": movements}


# --------------------------------------------------------------------------- #
# 试配（只读快照，不占纸；方案落 trial_plans 仅为便于确认/重发）
# --------------------------------------------------------------------------- #
@app.post(
    "/api/books/{book_id}/trial-matches",
    response_model=TrialResult,
    tags=["试配"],
    summary="选一批样书试配：最大同时补齐数 + 逐本取帖单（不占纸）",
)
def trial_match(
    book_id: int,
    payload: TrialRequest | None = Body(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    req = payload or TrialRequest()
    # 显式只读事务：所有 SELECT 处于同一 SQLite 快照。
    with transaction(conn, immediate=False):
        result = services.compute_trial(conn, book_id, req.sample_ids or [])
    # 快照读取结束后再留痕方案；留痕不触碰余量/领料状态，也不阻塞其他单确认。
    try:
        services.save_trial_plan(conn, result)
    except sqlite3.Error:
        # 留痕失败不应影响试配结论本身。
        pass
    return result


@app.get("/api/plans/{plan_id}", tags=["试配"], summary="查看此前的试配方案")
def get_plan(plan_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM trial_plans WHERE plan_id = ?", (plan_id,)).fetchone()
    if row is None:
        raise HTTPException(404, detail={"code": "plan_not_found", "plan_id": plan_id})
    result = json.loads(row["result_json"])
    return {
        "plan_id": row["plan_id"],
        "created_at": row["created_at"],
        "confirmed_order_id": row["confirmed_order_id"],
        "result": result,
    }


@app.post(
    "/api/plans/{plan_id}/confirm",
    response_model=ConfirmResponse,
    tags=["领料确认"],
    summary="确认试配方案；重复调用返回原领料单",
)
def confirm_plan(
    plan_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> JSONResponse:
    def work(c: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
        plan = c.execute(
            "SELECT * FROM trial_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if plan is None:
            raise HTTPException(404, detail={"code": "plan_not_found", "plan_id": plan_id})

        if plan["confirmed_order_id"]:
            order = c.execute(
                "SELECT * FROM requisition_orders WHERE id = ?",
                (plan["confirmed_order_id"],),
            ).fetchone()
            if order is not None:
                return services._replay_envelope(order, "plan_already_confirmed")  # noqa: SLF001

        req_data = json.loads(plan["request_json"])
        if not req_data.get("pick_lists"):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "empty_plan",
                    "message": "该试配方案没有任何可补齐样书（0 本入选），不能确认领料",
                    "plan_id": plan_id,
                },
            )
        status_code, envelope = services.create_order_in_tx(
            c,
            book_id=int(req_data["book_id"]),
            request_id=None,
            fingerprint=plan["fingerprint"],
            picks=req_data["pick_lists"],
        )
        c.execute(
            "UPDATE trial_plans SET confirmed_order_id = ? WHERE plan_id = ?",
            (envelope["order"]["id"], plan_id),
        )
        return status_code, envelope

    status_code, envelope = run_immediate(conn, work)
    return JSONResponse(status_code=status_code, content=envelope)


# --------------------------------------------------------------------------- #
# 直接确认领料单（并发自测的主要入口）
# --------------------------------------------------------------------------- #
@app.post(
    "/api/requisitions",
    response_model=ConfirmResponse,
    tags=["领料确认"],
    summary="提交领料/确认方案：整单原子扣料，冲突整单退回；同方案重发返回原单",
)
def create_requisition(
    payload: ConfirmRequest, conn: sqlite3.Connection = Depends(get_db)
) -> JSONResponse:
    data = payload.model_dump()
    fingerprint = services.fingerprint_plan(data["book_id"], data["pick_lists"])

    def work(c: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
        return services.create_order_in_tx(
            c,
            book_id=data["book_id"],
            request_id=data.get("request_id"),
            fingerprint=fingerprint,
            picks=data["pick_lists"],
        )

    status_code, envelope = run_immediate(conn, work)
    return JSONResponse(status_code=status_code, content=envelope)


@app.get("/api/requisitions", tags=["领料确认"], summary="领料单列表")
def list_requisitions(
    book_id: int | None = None, conn: sqlite3.Connection = Depends(get_db)
) -> list[dict[str, Any]]:
    if book_id is None:
        rows = conn.execute(
            "SELECT id, order_no, book_id, request_id, status, sample_count, "
            "total_pieces, created_at FROM requisition_orders ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, order_no, book_id, request_id, status, sample_count, "
            "total_pieces, created_at FROM requisition_orders WHERE book_id = ? ORDER BY id",
            (book_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/requisitions/{order_ref}", tags=["领料确认"], summary="领料单详情（按 ID 或 LL-0001 单号）")
def get_requisition(
    order_ref: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    row = None
    if order_ref.isdigit():
        row = conn.execute(
            "SELECT * FROM requisition_orders WHERE id = ?", (int(order_ref),)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM requisition_orders WHERE order_no = ?", (order_ref,)
        ).fetchone()
    if row is None:
        raise HTTPException(
            404, detail={"code": "requisition_not_found", "order_ref": order_ref}
        )
    return json.loads(row["response_json"])


# --------------------------------------------------------------------------- #
# 演示与管理
# --------------------------------------------------------------------------- #
@app.post("/api/demo/seed", status_code=201, tags=["演示"], summary="造一份争用演示数据（每次新建一本书）")
def demo_seed(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    with transaction(conn, immediate=True):
        seeded = services.seed_demo(conn)
    return seeded


@app.post("/api/admin/reset", tags=["演示"], summary="清空全部数据（需 ENABLE_RESET=1）")
def admin_reset(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    if not ENABLE_RESET:
        raise HTTPException(
            status_code=404,
            detail={"code": "reset_disabled", "message": "设置 ENABLE_RESET=1 后可用"},
        )
    with transaction(conn, immediate=True):
        conn.execute("PRAGMA defer_foreign_keys = ON")
        for table in (
            "stock_movements",
            "requisition_items",
            "requisition_orders",
            "trial_plans",
            "piles",
            "samples",
            "book_pages",
            "books",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")
    return {"reset": "ok", "at": services.utcnow_iso()}
