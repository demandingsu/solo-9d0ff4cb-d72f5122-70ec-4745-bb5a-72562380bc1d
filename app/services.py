"""核心业务：试配最大流、方案指纹、确认事务、单据序列化、演示种子。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from .flow import Dinic, reachable


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# 规范化指纹（同方案重发的幂等依据）
# --------------------------------------------------------------------------- #
def fingerprint_plan(book_id: int, pick_lists: list[dict[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for pl in pick_lists:
        items = sorted(
            (
                {"pile_id": int(it["pile_id"]), "quantity": int(it.get("quantity", 1))}
                for it in pl["items"]
            ),
            key=lambda x: x["pile_id"],
        )
        normalized.append({"sample_id": int(pl["sample_id"]), "items": items})
    normalized.sort(key=lambda x: x["sample_id"])
    raw = json.dumps(
        {"book_id": int(book_id), "pick_lists": normalized},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 试配
# --------------------------------------------------------------------------- #
def compute_trial(
    conn: sqlite3.Connection, book_id: int, requested_ids: list[int] | None
) -> dict[str, Any]:
    now = utcnow_iso()
    book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "book_not_found", "message": f"图书 {book_id} 不存在"},
        )
    n_pages = int(book["page_count"])

    target = {
        r["page_no"]: r["edition_no"]
        for r in conn.execute(
            "SELECT page_no, edition_no FROM book_pages WHERE book_id = ?", (book_id,)
        )
    }

    # 样书集合：显式指定则严格校验存在性；留空则取本书全部样书。
    if requested_ids:
        ordered_ids = list(dict.fromkeys(int(x) for x in requested_ids))
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = conn.execute(
            f"SELECT * FROM samples WHERE book_id = ? AND id IN ({placeholders})",
            (book_id, *ordered_ids),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        missing = [sid for sid in ordered_ids if sid not in by_id]
        if missing:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "sample_not_found",
                    "message": "部分样书不存在或不属于该图书",
                    "missing_sample_ids": missing,
                },
            )
        samples = [by_id[sid] for sid in ordered_ids]
    else:
        samples = list(
            conn.execute(
                "SELECT * FROM samples WHERE book_id = ? ORDER BY id", (book_id,)
            )
        )

    # 散帖垛：判定是否为“版号逐页相符”的可用边。
    pile_rows = list(
        conn.execute("SELECT * FROM piles WHERE book_id = ? ORDER BY id", (book_id,))
    )
    piles_by_id = {p["id"]: p for p in pile_rows}

    usable_piles: list[sqlite3.Row] = []
    diagnostics: list[dict[str, Any]] = []
    struct_adj: dict[int, list[int]] = {}
    avail_adj: dict[int, list[int]] = {}

    for p in pile_rows:
        start = int(p["start_page"])
        end = int(p["end_page"])
        editions = json.loads(p["page_editions"])
        mismatched: list[int] = []
        reason: str | None = None

        if end - start + 1 != len(editions):
            reason = "invalid_shape"
        elif start < 1 or end > n_pages:
            reason = "page_out_of_range"
        else:
            mismatched = [
                start + i
                for i, ed in enumerate(editions)
                if target.get(start + i) != ed
            ]
            if mismatched:
                reason = "edition_mismatch"

        diagnostics.append(
            {
                "pile_id": p["id"],
                "code": p["code"],
                "usable": reason is None,
                "reason": reason,
                "mismatched_pages": mismatched,
            }
        )
        if reason is not None:
            continue

        usable_piles.append(p)
        next_page = end + 1
        struct_adj.setdefault(start, []).append(next_page)
        if int(p["copies_remaining"]) > 0:
            avail_adj.setdefault(start, []).append(next_page)

    # 按领料状态/完成度分流。
    skipped: list[dict[str, Any]] = []
    candidates: list[sqlite3.Row] = []
    for s in samples:
        if s["status"] == "requisitioned":
            skipped.append(
                {
                    "sample_id": s["id"],
                    "code": s["code"],
                    "reason": "already_requisitioned",
                    "detail": "该本样书已经领过料，不再参与试配",
                }
            )
        elif int(s["bound_to_page"]) >= n_pages:
            skipped.append(
                {
                    "sample_id": s["id"],
                    "code": s["code"],
                    "reason": "already_complete",
                    "detail": "已配末页即全书末页，不需要散帖",
                }
            )
        else:
            candidates.append(s)

    pick_lists: list[dict[str, Any]] = []
    selected_ids: list[int] = []
    selected_set: set[int] = set()

    if candidates:
        m = len(candidates)
        source = 0
        sample_base = 1
        page_base = sample_base + m  # 页码 p -> page_base + (p - 1)
        sink = page_base + (n_pages + 1)
        dinic = Dinic(sink + 1)

        def page_node(page: int) -> int:
            return page_base + (page - 1)

        inf = 10**9
        src_edges = []
        for i, s in enumerate(candidates):
            snode = sample_base + i
            e = dinic.add_edge(source, snode, 1)
            src_edges.append(e)
            dinic.add_edge(snode, page_node(int(s["bound_to_page"]) + 1), 1)

        records_by_from: dict[int, list[Any]] = {}
        for p in usable_piles:
            cap = int(p["copies_remaining"])
            edge = dinic.add_edge(
                page_node(int(p["start_page"])),
                page_node(int(p["end_page"]) + 1),
                cap,
                tag=p["id"],
            )
            rec = type(
                "Rec",
                (),
                {
                    "pile_id": p["id"],
                    "from_page": int(p["start_page"]),
                    "to_page_exclusive": int(p["end_page"]) + 1,
                    "edge": edge,
                },
            )()
            records_by_from.setdefault(rec.from_page, []).append(rec)

        dinic.add_edge(page_node(n_pages + 1), sink, inf)
        dinic.max_flow(source, sink)

        # 路径分解：每个被饱和的 源->样书 边对应一本选中的样书。
        for i, s in enumerate(candidates):
            src_edge = src_edges[i]
            if not (src_edge.cap < src_edge.init_cap):
                continue
            sample_id = int(s["id"])
            selected_set.add(sample_id)
            selected_ids.append(sample_id)

            cursor = int(s["bound_to_page"]) + 1
            lines: list[dict[str, Any]] = []
            safety = 0
            while cursor != n_pages + 1:
                safety += 1
                if safety > n_pages + 1:  # 防御：页码严格递增，不可能发生
                    raise RuntimeError("试配路径分解异常：超出页码范围")
                chosen = None
                for rec in records_by_from.get(cursor, []):
                    if rec.edge.cap < rec.edge.init_cap:
                        chosen = rec
                        break
                if chosen is None:
                    raise RuntimeError(
                        f"试配路径分解异常：样书 {sample_id} 在页码 {cursor} 断链"
                    )
                # 回收一个已分解的流单位（cap 加回 1），供后续样书判断。
                chosen.edge.cap += 1
                p = piles_by_id[chosen.pile_id]
                lines.append(
                    {
                        "pile_id": p["id"],
                        "pile_code": p["code"],
                        "start_page": p["start_page"],
                        "end_page": p["end_page"],
                        "quantity": 1,
                        "page_editions": json.loads(p["page_editions"]),
                    }
                )
                cursor = chosen.to_page_exclusive

            pick_lists.append(
                {
                    "sample_id": sample_id,
                    "sample_code": s["code"],
                    "bound_to_page": s["bound_to_page"],
                    "missing_from_page": int(s["bound_to_page"]) + 1,
                    "items": lines,
                }
            )

    # 未入选样书的原因分层。
    unselected: list[dict[str, Any]] = []
    target_node = n_pages + 1
    for s in candidates:
        sid = int(s["id"])
        if sid in selected_set:
            continue
        start = int(s["bound_to_page"]) + 1
        if not reachable(struct_adj, start, target_node):
            reason = "no_valid_chain"
        elif not reachable(avail_adj, start, target_node):
            reason = "out_of_stock"
        else:
            reason = "contention"
        unselected.append(
            {"sample_id": sid, "code": s["code"], "bound_to_page": s["bound_to_page"], "reason": reason}
        )

    stock_snapshot = [
        {
            "pile_id": p["id"],
            "code": p["code"],
            "copies_total": p["copies_total"],
            "copies_remaining": p["copies_remaining"],
        }
        for p in pile_rows
    ]

    confirm_picks = [
        {
            "sample_id": pl["sample_id"],
            "items": [
                {"pile_id": item["pile_id"], "quantity": 1} for item in pl["items"]
            ],
        }
        for pl in pick_lists
    ]
    fp = fingerprint_plan(book_id, confirm_picks)
    plan_id = f"plan_{uuid.uuid4().hex[:12]}"

    return {
        "plan_id": plan_id,
        "book_id": book_id,
        "computed_at": now,
        "candidate_count": len(samples),
        "selected_count": len(pick_lists),
        "selected_sample_ids": selected_ids,
        "pick_lists": pick_lists,
        "skipped_samples": skipped,
        "unselected_samples": unselected,
        "stock_snapshot": stock_snapshot,
        "pile_diagnostics": diagnostics,
        "fingerprint": fp,
        "confirm_request": {
            "book_id": book_id,
            "pick_lists": confirm_picks,
        },
    }


def save_trial_plan(conn: sqlite3.Connection, result: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO trial_plans
            (plan_id, book_id, fingerprint, sample_count,
             request_json, result_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            result["plan_id"],
            result["book_id"],
            result["fingerprint"],
            result["selected_count"],
            json.dumps(result["confirm_request"], ensure_ascii=False),
            json.dumps(result, ensure_ascii=False),
            result["computed_at"],
        ),
    )


# --------------------------------------------------------------------------- #
# 领料单序列化
# --------------------------------------------------------------------------- #
def build_order_detail(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    order = conn.execute(
        "SELECT * FROM requisition_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        raise RuntimeError(f"领料单 {order_id} 不存在")

    item_rows = conn.execute(
        """
        SELECT ri.*, p.code AS pile_code, p.page_editions AS page_editions
        FROM requisition_items ri
        JOIN piles p ON p.id = ri.pile_id
        WHERE ri.order_id = ?
        ORDER BY ri.sample_id, ri.start_page, ri.pile_id
        """,
        (order_id,),
    ).fetchall()

    sample_cache: dict[int, sqlite3.Row] = {}

    def get_sample(sid: int) -> sqlite3.Row:
        if sid not in sample_cache:
            sample_cache[sid] = conn.execute(
                "SELECT * FROM samples WHERE id = ?", (sid,)
            ).fetchone()
        return sample_cache[sid]

    picks: dict[int, dict[str, Any]] = {}
    aggregated: dict[int, dict[str, Any]] = {}
    for ri in item_rows:
        sid = int(ri["sample_id"])
        sample = get_sample(sid)
        if sid not in picks:
            picks[sid] = {
                "sample_id": sid,
                "sample_code": sample["code"] if sample else str(sid),
                "bound_to_page": sample["bound_to_page"] if sample else 0,
                "missing_from_page": (sample["bound_to_page"] + 1) if sample else 1,
                "items": [],
            }
        picks[sid]["items"].append(
            {
                "pile_id": ri["pile_id"],
                "pile_code": ri["pile_code"],
                "start_page": ri["start_page"],
                "end_page": ri["end_page"],
                "quantity": ri["quantity"],
                "page_editions": json.loads(ri["page_editions"]),
            }
        )
        agg = aggregated.setdefault(
            int(ri["pile_id"]),
            {"pile_id": ri["pile_id"], "pile_code": ri["pile_code"], "quantity": 0},
        )
        agg["quantity"] += int(ri["quantity"])

    balances = {
        r["pile_id"]: r["balance_after"]
        for r in conn.execute(
            "SELECT pile_id, balance_after FROM stock_movements WHERE order_id = ?",
            (order_id,),
        )
    }
    aggregated_items = []
    for pid, agg in sorted(aggregated.items()):
        aggregated_items.append(
            {
                **agg,
                "copies_remaining_after": balances.get(pid),
            }
        )

    sample_ids = [sid for sid in sorted(picks.keys())]
    return {
        "id": order["id"],
        "order_no": order["order_no"],
        "book_id": order["book_id"],
        "request_id": order["request_id"],
        "fingerprint": order["fingerprint"],
        "status": order["status"],
        "created_at": order["created_at"],
        "sample_count": order["sample_count"],
        "total_pieces": order["total_pieces"],
        "sample_ids": sample_ids,
        "pick_lists": [picks[sid] for sid in sample_ids],
        "aggregated_items": aggregated_items,
    }


def _replay_envelope(
    order_row: sqlite3.Row, reason: str
) -> tuple[int, dict[str, Any]]:
    snapshot = order_row["response_json"]
    order = json.loads(snapshot) if snapshot else None
    if order is None:
        raise RuntimeError(f"领料单 {order_row['id']} 缺少快照")
    return 200, {"replayed": True, "replay_reason": reason, "order": order}


# --------------------------------------------------------------------------- #
# 确认（必须运行在 BEGIN IMMEDIATE 事务体内）
# --------------------------------------------------------------------------- #
def create_order_in_tx(
    conn: sqlite3.Connection,
    *,
    book_id: int,
    request_id: str | None,
    fingerprint: str,
    picks: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    # 1) 幂等：request_id 优先；同指纹方案重发直接返回原单。
    if request_id:
        row = conn.execute(
            "SELECT * FROM requisition_orders WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_key_conflict",
                        "message": "相同 request_id 对应了不同的领料方案",
                        "request_id": request_id,
                        "existing_order_no": row["order_no"],
                    },
                )
            return _replay_envelope(row, "request_id")

    fp_row = conn.execute(
        "SELECT * FROM requisition_orders WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if fp_row is not None:
        return _replay_envelope(fp_row, "same_plan")

    # 2) 图书与样书存在性。
    book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "book_not_found", "message": f"图书 {book_id} 不存在"},
        )
    n_pages = int(book["page_count"])
    target = {
        r["page_no"]: r["edition_no"]
        for r in conn.execute(
            "SELECT page_no, edition_no FROM book_pages WHERE book_id = ?", (book_id,)
        )
    }

    sample_ids = [int(p["sample_id"]) for p in picks]
    placeholders = ",".join("?" for _ in sample_ids)
    sample_rows = conn.execute(
        f"SELECT * FROM samples WHERE book_id = ? AND id IN ({placeholders})",
        (book_id, *sample_ids),
    ).fetchall()
    sample_by_id = {r["id"]: r for r in sample_rows}
    missing_samples = [sid for sid in sample_ids if sid not in sample_by_id]
    if missing_samples:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "sample_not_found",
                "message": "部分样书不存在或不属于该图书",
                "missing_sample_ids": missing_samples,
            },
        )

    # 3) 样书领料状态：只要有一本领过料，整单退回。
    claimed = [
        {
            "sample_id": r["id"],
            "code": r["code"],
            "requisition_order_id": r["requisition_order_id"],
        }
        for r in sample_rows
        if r["status"] == "requisitioned"
    ]
    if claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "samples_already_requisitioned",
                "message": "单上存在已领料样书，整单退回，余量与领料状态不变",
                "samples": claimed,
            },
        )

    # 4) 散帖存在性 + 逐本帖链合法性（连续、到末页、版号相符）。
    pile_ids = sorted(
        {int(it["pile_id"]) for p in picks for it in p["items"]}
    )
    pile_ph = ",".join("?" for _ in pile_ids)
    pile_rows = conn.execute(
        f"SELECT * FROM piles WHERE book_id = ? AND id IN ({pile_ph})",
        (book_id, *pile_ids),
    ).fetchall()
    pile_by_id = {r["id"]: r for r in pile_rows}
    missing_piles = [pid for pid in pile_ids if pid not in pile_by_id]
    if missing_piles:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "pile_not_found",
                "message": "部分散帖垛不存在或不属于该图书",
                "missing_pile_ids": missing_piles,
            },
        )

    problems: list[dict[str, Any]] = []
    for pick in picks:
        sid = int(pick["sample_id"])
        sample = sample_by_id[sid]
        expected_start = int(sample["bound_to_page"]) + 1
        ordered_items = sorted(
            pick["items"], key=lambda it: int(pile_by_id[int(it["pile_id"])]["start_page"])
        )
        cursor = expected_start
        for it in ordered_items:
            pid = int(it["pile_id"])
            qty = int(it.get("quantity", 1))
            pile = pile_by_id[pid]
            if qty != 1:
                problems.append(
                    {"sample_id": sid, "pile_id": pid, "problem": "quantity_must_be_1_per_book"}
                )
            if int(pile["start_page"]) != cursor:
                problems.append(
                    {
                        "sample_id": sid,
                        "pile_id": pid,
                        "problem": "chain_not_continuous",
                        "expected_page": cursor,
                        "pile_start_page": pile["start_page"],
                    }
                )
                break
            editions = json.loads(pile["page_editions"])
            bad_pages = [
                int(pile["start_page"]) + i
                for i, ed in enumerate(editions)
                if target.get(int(pile["start_page"]) + i) != ed
            ]
            if bad_pages:
                problems.append(
                    {
                        "sample_id": sid,
                        "pile_id": pid,
                        "problem": "edition_mismatch",
                        "pages": bad_pages,
                    }
                )
                break
            cursor = int(pile["end_page"]) + 1
        else:
            if cursor != n_pages + 1:
                problems.append(
                    {
                        "sample_id": sid,
                        "problem": "chain_does_not_reach_last_page",
                        "next_page": cursor,
                        "book_last_page": n_pages,
                    }
                )
    if problems:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_pick_list",
                "message": "取帖单不满足整帖连续相接且版号相符的要求，未做任何改动",
                "problems": problems,
            },
        )

    # 5) 汇总需求并检查余量：不够则整单退回。
    need: dict[int, int] = {}
    for pick in picks:
        for it in pick["items"]:
            pid = int(it["pile_id"])
            need[pid] = need.get(pid, 0) + int(it.get("quantity", 1))

    shortages = []
    for pid, required in sorted(need.items()):
        remaining = int(pile_by_id[pid]["copies_remaining"])
        if remaining < required:
            shortages.append(
                {
                    "pile_id": pid,
                    "pile_code": pile_by_id[pid]["code"],
                    "required": required,
                    "remaining": remaining,
                    "short": required - remaining,
                }
            )
    if shortages:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "insufficient_stock",
                "message": "散帖余量不足，整单退回，余量与领料状态不变",
                "shortages": shortages,
            },
        )

    # 6) 全部检查通过：写单、扣料、标记样书。
    now = utcnow_iso()
    cur = conn.execute(
        """
        INSERT INTO requisition_orders
            (order_no, book_id, request_id, fingerprint, status,
             sample_count, total_pieces, created_at)
        VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?)
        """,
        (
            f"TMP-{uuid.uuid4().hex}",
            book_id,
            request_id,
            fingerprint,
            len(sample_ids),
            sum(need.values()),
            now,
        ),
    )
    order_id = int(cur.lastrowid)
    order_no = f"LL-{order_id:04d}"
    conn.execute(
        "UPDATE requisition_orders SET order_no = ? WHERE id = ?",
        (order_no, order_id),
    )

    for pick in picks:
        sid = int(pick["sample_id"])
        for it in pick["items"]:
            pid = int(it["pile_id"])
            pile = pile_by_id[pid]
            conn.execute(
                """
                INSERT INTO requisition_items
                    (order_id, sample_id, pile_id, quantity, start_page, end_page)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    sid,
                    pid,
                    int(it.get("quantity", 1)),
                    pile["start_page"],
                    pile["end_page"],
                ),
            )

        # 条件更新作为并发双保险（外层已有 BEGIN IMMEDIATE 串行化）。
        upd = conn.execute(
            """
            UPDATE samples
               SET status = 'requisitioned', requisition_order_id = ?
             WHERE id = ? AND status = 'available'
            """,
            (order_id, sid),
        )
        if upd.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "sample_state_changed",
                    "message": "样书状态在确认过程中变化，整单退回",
                    "sample_id": sid,
                },
            )

    for pid, required in sorted(need.items()):
        upd = conn.execute(
            """
            UPDATE piles
               SET copies_remaining = copies_remaining - ?
             WHERE id = ? AND copies_remaining >= ?
            """,
            (required, pid, required),
        )
        if upd.rowcount != 1:
            row = conn.execute(
                "SELECT copies_remaining FROM piles WHERE id = ?", (pid,)
            ).fetchone()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "insufficient_stock",
                    "message": "并发扣料时余量不足，整单退回",
                    "pile_id": pid,
                    "remaining": row["copies_remaining"] if row else None,
                    "required": required,
                },
            )
        balance = conn.execute(
            "SELECT copies_remaining FROM piles WHERE id = ?", (pid,)
        ).fetchone()["copies_remaining"]
        conn.execute(
            """
            INSERT INTO stock_movements
                (order_id, pile_id, delta, balance_after, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (order_id, pid, -required, balance, now),
        )

    detail = build_order_detail(conn, order_id)
    conn.execute(
        "UPDATE requisition_orders SET response_json = ? WHERE id = ?",
        (json.dumps(detail, ensure_ascii=False), order_id),
    )
    return 201, {"replayed": False, "replay_reason": None, "order": detail}


# --------------------------------------------------------------------------- #
# 演示种子：6 页书 + 5 本样书 + 5 垛帖（含一垛版号不符）
# 最大同时补齐数 = 4，且恰有 1 本因争用落选。
# --------------------------------------------------------------------------- #
def seed_demo(conn: sqlite3.Connection) -> dict[str, Any]:
    now = utcnow_iso()
    cur = conn.execute(
        "INSERT INTO books (title, page_count, created_at) VALUES (?, ?, ?)",
        ("演示样书：散帖争用与版号核对（6 页）", 6, now),
    )
    book_id = int(cur.lastrowid)
    target = ["E1", "E1", "E2", "E2", "E3", "E3"]
    for page_no, ed in enumerate(target, start=1):
        conn.execute(
            "INSERT INTO book_pages (book_id, page_no, edition_no) VALUES (?, ?, ?)",
            (book_id, page_no, ed),
        )

    sample_specs = [
        ("YB-01", 0),
        ("YB-02", 2),
        ("YB-03", 2),
        ("YB-04", 2),
        ("YB-05", 4),
    ]
    sample_ids: dict[str, int] = {}
    for code, bound in sample_specs:
        c = conn.execute(
            "INSERT INTO samples (book_id, code, bound_to_page, status, created_at)"
            " VALUES (?, ?, ?, 'available', ?)",
            (book_id, code, bound, now),
        )
        sample_ids[code] = int(c.lastrowid)

    pile_specs = [
        ("D-12", 1, ["E1", "E1"], 2),
        ("D-34", 3, ["E2", "E2"], 1),
        ("D-36", 3, ["E2", "E2", "E3", "E3"], 2),
        ("D-56", 5, ["E3", "E3"], 2),
        ("D-34-BAD", 3, ["E0", "E0"], 3),
    ]
    pile_ids: dict[str, int] = {}
    for code, start, eds, copies in pile_specs:
        c = conn.execute(
            """
            INSERT INTO piles
                (book_id, code, start_page, end_page, page_editions,
                 copies_total, copies_remaining, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                book_id,
                code,
                start,
                start + len(eds) - 1,
                json.dumps(eds),
                copies,
                copies,
                now,
            ),
        )
        pile_ids[code] = int(c.lastrowid)

    return {
        "book_id": book_id,
        "sample_ids": sample_ids,
        "pile_ids": pile_ids,
        "trial_request_hint": {"sample_ids": []},
        "next_steps": [
            f"POST /api/books/{book_id}/trial-matches  body={{\"sample_ids\": []}}",
            "从响应复制 plan_id，POST /api/plans/{plan_id}/confirm",
            "再次 POST 同一地址，验证返回原领料单（replayed=true）",
            "GET /api/requisitions 查看全部领料单",
        ],
    }
