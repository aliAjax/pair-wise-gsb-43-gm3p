"""Sealed public-procurement tendering and evaluation service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "public_procurement.db"


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.details = details


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise DomainError("时间格式无效") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def clean_actor(actor: str) -> str:
    actor = (actor or "").strip()
    if not actor:
        raise DomainError("缺少操作人")
    return actor


def require_role(role: str, allowed: set[str], action: str) -> None:
    if role not in allowed:
        raise DomainError("角色无权执行：%s" % action, 403)


def canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class ProcurementService:
    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    deadline TEXT NOT NULL,
                    criteria TEXT NOT NULL DEFAULT '[]',
                    evaluation_round INTEGER NOT NULL DEFAULT 1,
                    evaluations_locked INTEGER NOT NULL DEFAULT 0,
                    awarded_bid_id INTEGER,
                    award_snapshot TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS vendors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor_no TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    representative TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bids (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'sealed',
                    version INTEGER NOT NULL DEFAULT 1,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    opened_at TEXT,
                    UNIQUE(tender_id,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bid_id INTEGER NOT NULL REFERENCES bids(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    seat_id INTEGER REFERENCES evaluation_seats(id),
                    criterion TEXT NOT NULL,
                    raw_value REAL NOT NULL,
                    score REAL NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'valid',
                    invalidated_at TEXT,
                    invalidation_reason TEXT NOT NULL DEFAULT '',
                    recusal_id INTEGER REFERENCES recusals(id),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(bid_id,evaluation_round,evaluator,criterion)
                );
                CREATE TABLE IF NOT EXISTS recusals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    declared_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_by TEXT,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS evaluation_seats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seat_no TEXT NOT NULL,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    evaluation_round INTEGER NOT NULL,
                    evaluator TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    recusal_id INTEGER REFERENCES recusals(id),
                    handover_id INTEGER REFERENCES seat_handovers(id),
                    original_evaluator TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tender_id,vendor_id,evaluation_round,seat_no)
                );
                CREATE TABLE IF NOT EXISTS seat_handovers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seat_id INTEGER NOT NULL REFERENCES evaluation_seats(id),
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER NOT NULL REFERENCES vendors(id),
                    evaluation_round INTEGER NOT NULL,
                    previous_evaluator TEXT NOT NULL,
                    replacement_evaluator TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    confirmed_by TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    evaluator TEXT NOT NULL,
                    vendor_id INTEGER REFERENCES vendors(id),
                    reason TEXT NOT NULL,
                    declared_by TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    source_recusal_id INTEGER REFERENCES recusals(id),
                    created_at TEXT NOT NULL,
                    UNIQUE(tender_id,evaluator,vendor_id)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    vendor_id INTEGER REFERENCES vendors(id),
                    question TEXT NOT NULL,
                    answer TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    answered_by TEXT,
                    created_at TEXT NOT NULL,
                    answered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS complaints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER NOT NULL REFERENCES tenders(id),
                    complainant TEXT NOT NULL,
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolution TEXT,
                    reviewed_by TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS timeline (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id INTEGER REFERENCES tenders(id),
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_bids_tender ON bids(tender_id,status);
                CREATE INDEX IF NOT EXISTS idx_eval_bid_round ON evaluations(bid_id,evaluation_round);
                """
            )
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Backfill columns/indexes added after the first release on already-created databases."""
        add_columns = {
            "evaluations": [
                ("seat_id", "INTEGER"),
                ("status", "TEXT NOT NULL DEFAULT 'valid'"),
                ("invalidated_at", "TEXT"),
                ("invalidation_reason", "TEXT NOT NULL DEFAULT ''"),
                ("recusal_id", "INTEGER"),
            ],
            "conflicts": [
                ("source", "TEXT NOT NULL DEFAULT 'manual'"),
                ("source_recusal_id", "INTEGER"),
            ],
        }
        with self.connect() as conn:
            for table, columns in add_columns.items():
                existing = {row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()}
                for name, declaration in columns:
                    if name not in existing:
                        conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, declaration))
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_eval_status ON evaluations(bid_id,evaluation_round,status);
                CREATE INDEX IF NOT EXISTS idx_seats_lookup ON evaluation_seats(tender_id,vendor_id,evaluation_round);
                CREATE INDEX IF NOT EXISTS idx_handovers_seat ON seat_handovers(seat_id);
                CREATE INDEX IF NOT EXISTS idx_recusals_lookup ON recusals(tender_id,evaluator,status);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_seat_holder
                    ON evaluation_seats(tender_id,vendor_id,evaluation_round,evaluator)
                    WHERE status IN ('active','handover_pending');
                CREATE UNIQUE INDEX IF NOT EXISTS uq_confirmed_handover
                    ON seat_handovers(seat_id) WHERE status='confirmed';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_vendor_recusal
                    ON recusals(tender_id,evaluator,COALESCE(vendor_id,0)) WHERE status='active';
                """
            )

    def _audit(self, conn: sqlite3.Connection, tender_id: int | None, actor: str,
               action: str, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO timeline(tender_id,actor,action,details,created_at) VALUES(?,?,?,?,?)",
            (tender_id, actor, action, json.dumps(details, ensure_ascii=False, sort_keys=True), utcnow()),
        )

    def _tender(self, conn: sqlite3.Connection, tender_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM tenders WHERE id=?", (tender_id,)).fetchone()
        if not row:
            raise DomainError("采购项目不存在", 404)
        return row

    def create_vendor(self, actor: str, role: str, vendor_no: str, name: str,
                      representative: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "创建供应商")
        if not vendor_no.strip() or not name.strip() or not representative.strip():
            raise DomainError("供应商编号、名称和代表不能为空")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO vendors(vendor_no,name,representative,created_at) VALUES(?,?,?,?)",
                    (vendor_no.strip(), name.strip(), representative.strip(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("供应商编号已存在", 409) from exc
            self._audit(conn, None, actor, "vendor.created", {"vendor_no": vendor_no.strip()})
            return dict(conn.execute("SELECT * FROM vendors WHERE id=?", (cur.lastrowid,)).fetchone())

    def create_tender(self, actor: str, role: str, tender_no: str, title: str,
                      deadline: str, criteria: list[dict[str, Any]], description: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "创建采购项目")
        parse_time(deadline)
        if not tender_no.strip() or not title.strip():
            raise DomainError("项目编号和标题不能为空")
        normalized_criteria = []
        total_weight = Decimal("0")
        for item in criteria:
            if not isinstance(item, dict) or not str(item.get("name", "")).strip():
                raise DomainError("评分项格式无效")
            kind = item.get("kind", "direct")
            if kind not in {"direct", "cost"}:
                raise DomainError("评分项类型只支持 direct 或 cost")
            try:
                weight = Decimal(str(item["weight"]))
                max_value = Decimal(str(item.get("max_value", 100)))
            except (KeyError, InvalidOperation) as exc:
                raise DomainError("评分权重或上限无效") from exc
            if weight <= 0 or max_value <= 0:
                raise DomainError("评分权重和上限必须大于0")
            total_weight += weight
            normalized_criteria.append({"name": str(item["name"]).strip(), "kind": kind,
                                        "weight": float(weight), "max_value": float(max_value)})
        if not normalized_criteria or total_weight != 100:
            raise DomainError("评分项权重合计必须等于100")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO tenders(tender_no,title,description,deadline,criteria,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (tender_no.strip(), title.strip(), description.strip(), parse_time(deadline).isoformat(timespec="seconds"),
                     json.dumps(normalized_criteria, ensure_ascii=False), actor, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目编号已存在", 409) from exc
            self._audit(conn, cur.lastrowid, actor, "tender.created", {"tender_no": tender_no.strip()})
            return dict(self._tender(conn, cur.lastrowid))

    def publish_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement"}, "发布采购项目")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "draft":
                raise DomainError("只有草稿项目可以发布", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            conn.execute("UPDATE tenders SET status='published',version=version+1,updated_at=? WHERE id=?", (utcnow(), tender_id))
            self._audit(conn, tender_id, actor, "tender.published", {"deadline": tender["deadline"]})
            return dict(self._tender(conn, tender_id))

    def submit_bid(self, actor: str, role: str, tender_id: int, vendor_id: int,
                   payload: dict[str, Any], price: float, expected_version: int | None = None) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "提交投标")
        if not isinstance(payload, dict):
            raise DomainError("投标内容必须是对象")
        try:
            price = float(price)
        except (TypeError, ValueError) as exc:
            raise DomainError("报价必须是数值") from exc
        if price <= 0:
            raise DomainError("报价必须大于0")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("当前项目不接受投标", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]):
                raise DomainError("投标截止时间已过", 409)
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (vendor_id,)).fetchone()
            if not vendor:
                raise DomainError("供应商不存在", 404)
            if not conn.execute("SELECT 1 FROM conflicts WHERE tender_id=? AND vendor_id=? AND evaluator=?", (tender_id, vendor_id, actor)).fetchone():
                pass
            existing = conn.execute("SELECT * FROM bids WHERE tender_id=? AND vendor_id=?", (tender_id, vendor_id)).fetchone()
            payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            digest = canonical_hash(payload)
            if existing:
                if existing["status"] != "sealed":
                    raise DomainError("投标已撤回或已开标，不能修改", 409)
                if expected_version is None or existing["version"] != int(expected_version):
                    raise DomainError("投标已变化，请刷新后重试", 409)
                conn.execute(
                    "UPDATE bids SET payload=?,payload_hash=?,price=?,version=version+1,submitted_at=? WHERE id=? AND version=?",
                    (payload_text, digest, price, utcnow(), existing["id"], expected_version),
                )
                bid_id = existing["id"]
                action = "bid.updated"
            else:
                cur = conn.execute(
                    """INSERT INTO bids(tender_id,vendor_id,payload,payload_hash,price,submitted_by,submitted_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (tender_id, vendor_id, payload_text, digest, price, actor, utcnow()),
                )
                bid_id = cur.lastrowid
                action = "bid.submitted"
            self._audit(conn, tender_id, actor, action, {"bid_id": bid_id, "vendor_id": vendor_id, "hash": digest})
            bid = dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())
            bid["payload_hash"] = digest
            return bid

    def withdraw_bid(self, actor: str, role: str, bid_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor"}, "撤回投标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if bid["submitted_by"] != actor:
                raise DomainError("只能撤回自己的投标", 403)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) >= parse_time(tender["deadline"]) or bid["status"] != "sealed":
                raise DomainError("截止后不能撤回投标", 409)
            conn.execute("UPDATE bids SET status='withdrawn',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.withdrawn", {"bid_id": bid_id})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def open_bids(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "开标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] != "published":
                raise DomainError("项目当前不能开标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            if datetime.now(timezone.utc) < parse_time(tender["deadline"]):
                raise DomainError("尚未到开标时间", 409)
            rows = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status='sealed' ORDER BY id", (tender_id,)).fetchall()
            opened = []
            now = utcnow()
            for row in rows:
                digest = canonical_hash(json.loads(row["payload"]))
                if digest != row["payload_hash"]:
                    raise DomainError("投标完整性校验失败: %s" % row["id"], 409)
                conn.execute("UPDATE bids SET status='opened',opened_at=?,version=version+1 WHERE id=?", (now, row["id"]))
                opened.append(dict(conn.execute("SELECT * FROM bids WHERE id=?", (row["id"],)).fetchone()))
            conn.execute("UPDATE tenders SET status='opened',version=version+1,updated_at=? WHERE id=?", (now, tender_id))
            self._audit(conn, tender_id, actor, "tender.opened", {"bid_count": len(opened)})
            return {"tender": dict(self._tender(conn, tender_id)), "bids": opened}

    def declare_conflict(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "申报利益冲突")
        if not evaluator.strip() or not reason.strip():
            raise DomainError("评审人和冲突原因不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            try:
                cur = conn.execute(
                    "INSERT INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,created_at) VALUES(?,?,?,?,?,?)",
                    (tender_id, evaluator.strip(), vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("利益冲突已申报", 409) from exc
            self._audit(conn, tender_id, actor, "conflict.declared", {"evaluator": evaluator.strip(), "vendor_id": vendor_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM conflicts WHERE id=?", (cur.lastrowid,)).fetchone())

    def _get_seat(self, conn: sqlite3.Connection, seat_id: int) -> sqlite3.Row:
        seat = conn.execute("SELECT * FROM evaluation_seats WHERE id=?", (seat_id,)).fetchone()
        if not seat:
            raise DomainError("评审席位不存在", 404)
        return seat

    def _seat_for_scoring(self, conn: sqlite3.Connection, tender: sqlite3.Row,
                          vendor_id: int, evaluator: str) -> sqlite3.Row:
        """Return the live seat an evaluator is allowed to score through, opening one on first use."""
        seat = conn.execute(
            """SELECT * FROM evaluation_seats
               WHERE tender_id=? AND vendor_id=? AND evaluation_round=? AND evaluator=?
                 AND status IN ('active','handover_pending')""",
            (tender["id"], vendor_id, tender["evaluation_round"], evaluator),
        ).fetchone()
        if seat:
            if seat["status"] == "handover_pending":
                raise DomainError("席位交接进行中，接替人请通过补评接口提交评分", 409, {"seat_id": seat["id"]})
            return seat
        # A recused seat for this evaluator means they must not produce live scores.
        recused = conn.execute(
            """SELECT * FROM evaluation_seats
               WHERE tender_id=? AND vendor_id=? AND evaluation_round=? AND evaluator=? AND status='recused'""",
            (tender["id"], vendor_id, tender["evaluation_round"], evaluator),
        ).fetchone()
        if recused:
            raise DomainError("该专家已回避本供应商，请通过席位交接后由接替人补评", 403)
        # No seat yet: another live (recused-but-unhanded) seat exists only for a different evaluator,
        # which is allowed. A pending handover to us means we score via rescore, not a new seat.
        pending_for_us = conn.execute(
            """SELECT h.* FROM seat_handovers h
               WHERE h.tender_id=? AND h.vendor_id=? AND h.evaluation_round=?
                 AND h.replacement_evaluator=? AND h.status='pending'""",
            (tender["id"], vendor_id, tender["evaluation_round"], evaluator),
        ).fetchone()
        if pending_for_us:
            raise DomainError("交接尚待监督员确认，请使用补评接口提交评分", 409,
                              {"seat_id": pending_for_us["seat_id"], "handover_id": pending_for_us["id"]})
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM evaluation_seats WHERE tender_id=? AND vendor_id=? AND evaluation_round=?",
            (tender["id"], vendor_id, tender["evaluation_round"]),
        ).fetchone()["c"]
        seat_no = "S%02d" % (count + 1)
        now = utcnow()
        try:
            cur = conn.execute(
                """INSERT INTO evaluation_seats(seat_no,tender_id,vendor_id,evaluation_round,evaluator,status,original_evaluator,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',?,?,?)""",
                (seat_no, tender["id"], vendor_id, tender["evaluation_round"], evaluator, evaluator, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise DomainError("同一专家不能同时占用该供应商的两个评审席位", 409) from exc
        return conn.execute("SELECT * FROM evaluation_seats WHERE id=?", (cur.lastrowid,)).fetchone()

    def _validate_replacement(self, conn: sqlite3.Connection, tender_id: int, vendor_id: int,
                              round_no: int, seat: sqlite3.Row, replacement: str) -> None:
        if not replacement.strip():
            raise DomainError("接替人不能为空")
        replacement = replacement.strip()
        if replacement == seat["original_evaluator"]:
            raise DomainError("接替人不能是被回避的原评审专家")
        conflict = conn.execute(
            "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
            (tender_id, replacement, vendor_id),
        ).fetchone()
        if conflict:
            raise DomainError("接替人与该供应商存在利益冲突")
        occupied = conn.execute(
            """SELECT id FROM evaluation_seats
               WHERE tender_id=? AND vendor_id=? AND evaluation_round=? AND evaluator=?
                 AND id<>? AND status IN ('active','handover_pending')""",
            (tender_id, vendor_id, round_no, replacement, seat["id"]),
        ).fetchone()
        if occupied:
            raise DomainError("接替人已占用该供应商的其他评审席位")

    def recuse_evaluator(self, actor: str, role: str, tender_id: int, evaluator: str,
                         vendor_id: int | None, reason: str) -> dict[str, Any]:
        """回避生效：席位转回避、相关评分立即失效（历史保留），并登记冲突阻断复评。"""
        actor = clean_actor(actor)
        require_role(role, {"evaluator", "procurement", "supervisor"}, "回避")
        evaluator = (evaluator or "").strip()
        if not evaluator or not reason.strip():
            raise DomainError("评审人和回避原因不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["evaluations_locked"] or tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目阶段不能办理回避", 409)
            if role == "evaluator" and actor != evaluator:
                raise DomainError("评审专家只能办理本人回避", 403)
            if vendor_id is not None and not conn.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
                raise DomainError("供应商不存在", 404)
            round_no = tender["evaluation_round"]
            try:
                cur = conn.execute(
                    """INSERT INTO recusals(tender_id,evaluation_round,evaluator,vendor_id,reason,status,declared_by,created_at)
                       VALUES(?,?,?,?,?, 'active',?,?)""",
                    (tender_id, round_no, evaluator, vendor_id, reason.strip(), actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该专家对该供应商已有生效中的回避记录", 409) from exc
            recusal_id = cur.lastrowid
            now = utcnow()
            if vendor_id is None:
                seats = conn.execute(
                    """SELECT * FROM evaluation_seats
                       WHERE tender_id=? AND evaluation_round=? AND evaluator=? AND status IN ('active','handover_pending')""",
                    (tender_id, round_no, evaluator),
                ).fetchall()
            else:
                seats = conn.execute(
                    """SELECT * FROM evaluation_seats
                       WHERE tender_id=? AND vendor_id=? AND evaluation_round=? AND evaluator=?
                         AND status IN ('active','handover_pending')""",
                    (tender_id, vendor_id, round_no, evaluator),
                ).fetchall()
            affected_seat_ids = [s["id"] for s in seats]
            for seat in seats:
                conn.execute(
                    "UPDATE evaluation_seats SET status='recused',recusal_id=?,handover_id=NULL,updated_at=? WHERE id=?",
                    (recusal_id, now, seat["id"]),
                )
                for handover in conn.execute(
                    "SELECT * FROM seat_handovers WHERE seat_id=? AND status='pending'", (seat["id"],)
                ).fetchall():
                    conn.execute(
                        """UPDATE evaluations SET status='invalidated',invalidated_at=?,invalidation_reason='回避状态变化，待确认交接取消',updated_at=?
                           WHERE seat_id=? AND evaluator=? AND status='pending'""",
                        (now, now, seat["id"], handover["replacement_evaluator"]),
                    )
                    conn.execute("UPDATE seat_handovers SET status='failed',failure_reason=? WHERE id=?",
                                 ("回避生效，待确认交接取消", handover["id"]))
            # 失效相关评分（只失效当前轮次、未失效的），历史行原样保留。
            if vendor_id is None:
                eval_rows = conn.execute(
                    """SELECT e.id FROM evaluations e JOIN bids b ON b.id=e.bid_id
                       WHERE b.tender_id=? AND e.evaluation_round=? AND e.evaluator=? AND e.status='valid'""",
                    (tender_id, round_no, evaluator),
                ).fetchall()
            else:
                eval_rows = conn.execute(
                    """SELECT e.id FROM evaluations e JOIN bids b ON b.id=e.bid_id
                       WHERE b.tender_id=? AND b.vendor_id=? AND e.evaluation_round=? AND e.evaluator=? AND e.status='valid'""",
                    (tender_id, vendor_id, round_no, evaluator),
                ).fetchall()
            invalidated = [r["id"] for r in eval_rows]
            if invalidated:
                conn.execute(
                    """UPDATE evaluations SET status='invalidated',invalidated_at=?,invalidation_reason=?,recusal_id=?,
                       updated_at=? WHERE id IN (%s)""" % ",".join("?" * len(invalidated)),
                    [now, "评审专家临时回避: " + reason.strip(), recusal_id, now, *invalidated],
                )
            # 登记/复用利益冲突，避免专家改从普通评分接口回到该供应商。
            conn.execute(
                """INSERT OR IGNORE INTO conflicts(tender_id,evaluator,vendor_id,reason,declared_by,source,source_recusal_id,created_at)
                   VALUES(?,?,?,?,?, 'recusal',?,?)""",
                (tender_id, evaluator, vendor_id, "回避: " + reason.strip(), actor, recusal_id, now),
            )
            self._audit(conn, tender_id, actor, "evaluator.recused", {
                "recusal_id": recusal_id, "evaluator": evaluator, "vendor_id": vendor_id,
                "seats": affected_seat_ids, "invalidated_evaluations": invalidated,
            })
            seat_rows = conn.execute(
                "SELECT * FROM evaluation_seats WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(affected_seat_ids)),
                affected_seat_ids,
            ).fetchall() if affected_seat_ids else []
            return {
                "recusal": dict(conn.execute("SELECT * FROM recusals WHERE id=?", (recusal_id,)).fetchone()),
                "seats": [dict(s) for s in seat_rows],
                "invalidated_evaluations": invalidated,
            }

    def revoke_recusal(self, actor: str, role: str, recusal_id: int) -> dict[str, Any]:
        """撤销回避：未交接的席位恢复原专家，失效评分复原；已完成交接的席位不可回退。"""
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "撤销回避")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            recusal = conn.execute("SELECT * FROM recusals WHERE id=?", (recusal_id,)).fetchone()
            if not recusal:
                raise DomainError("回避记录不存在", 404)
            if recusal["status"] != "active":
                raise DomainError("回避记录不是生效状态", 409)
            tender = self._tender(conn, recusal["tender_id"])
            if tender["evaluations_locked"]:
                raise DomainError("评分已锁定，不能撤销回避", 409)
            all_seats = conn.execute(
                "SELECT * FROM evaluation_seats WHERE recusal_id=?", (recusal_id,)
            ).fetchall()
            handed_over = [s["id"] for s in all_seats if s["status"] == "active"
                           and s["evaluator"] != s["original_evaluator"]]
            if handed_over:
                raise DomainError("已有席位完成交接，不能撤销该回避（交接不可回退）", 409,
                                  {"handed_over_seats": handed_over})
            recused_seats = [s for s in all_seats if s["status"] == "recused"]
            now = utcnow()
            restored_seats = []
            for seat in recused_seats:
                # 待确认的交接提名一并作废，其补评分数失效保留（无 recusal_id 关联，单独处理）。
                for handover in conn.execute(
                    "SELECT * FROM seat_handovers WHERE seat_id=? AND status='pending'", (seat["id"],)
                ).fetchall():
                    conn.execute(
                        """UPDATE evaluations SET status='invalidated',invalidated_at=?,invalidation_reason='回避撤销，交接提名作废',updated_at=?
                           WHERE seat_id=? AND evaluator=? AND status='pending'""",
                        (now, now, seat["id"], handover["replacement_evaluator"]),
                    )
                    conn.execute("UPDATE seat_handovers SET status='failed',failure_reason=? WHERE id=?",
                                 ("回避撤销，待确认交接作废", handover["id"]))
                conn.execute(
                    "UPDATE evaluation_seats SET status='active',updated_at=? WHERE id=?", (now, seat["id"])
                )
                restored_seats.append(seat["id"])
            if restored_seats:
                placeholders = ",".join("?" * len(restored_seats))
                conn.execute(
                    """UPDATE evaluations SET status='valid',invalidated_at=NULL,invalidation_reason='',updated_at=?
                       WHERE recusal_id=? AND status='invalidated' AND seat_id IN (%s)""" % placeholders,
                    [now, recusal_id, *restored_seats],
                )
            # 只清理回避自动登记的冲突；手工申报的冲突保持不变。
            conn.execute(
                "DELETE FROM conflicts WHERE source='recusal' AND source_recusal_id=?", (recusal_id,)
            )
            conn.execute(
                "UPDATE recusals SET status='revoked',revoked_by=?,revoked_at=? WHERE id=?",
                (actor, now, recusal_id),
            )
            self._audit(conn, recusal["tender_id"], actor, "recusal.revoked",
                        {"recusal_id": recusal_id, "restored_seats": restored_seats})
            return {"recusal": dict(conn.execute("SELECT * FROM recusals WHERE id=?", (recusal_id,)).fetchone()),
                    "restored_seats": restored_seats}

    def confirm_handover(self, actor: str, role: str, seat_id: int, replacement_evaluator: str) -> dict[str, Any]:
        """监督员确认席位交接。对同一接替人重试幂等；失败留痕，可对原席位再次重试。"""
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "确认交接")
        replacement_evaluator = (replacement_evaluator or "").strip()

        # 预检查（只读短事务）：资格不通过时独立提交失败留痕，主流程回滚也不会丢。
        with self.connect() as pre:
            seat_pre = pre.execute("SELECT * FROM evaluation_seats WHERE id=?", (seat_id,)).fetchone()
            if not seat_pre:
                raise DomainError("评审席位不存在", 404)
            tender_pre = self._tender(pre, seat_pre["tender_id"])
            if tender_pre["evaluations_locked"] or tender_pre["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目阶段不能交接席位", 409)
            need_record = False
            if seat_pre["status"] == "recused":
                need_record = True
            elif seat_pre["status"] == "handover_pending":
                pending_pre = pre.execute(
                    "SELECT * FROM seat_handovers WHERE seat_id=? AND status='pending' ORDER BY id", (seat_id,)
                ).fetchone()
                need_record = bool(pending_pre and pending_pre["replacement_evaluator"] != replacement_evaluator)
            if need_record:
                try:
                    self._validate_replacement(pre, seat_pre["tender_id"], seat_pre["vendor_id"],
                                               seat_pre["evaluation_round"], seat_pre, replacement_evaluator)
                except DomainError as exc:
                    now = utcnow()
                    cur = pre.execute(
                        """INSERT INTO seat_handovers(seat_id,tender_id,vendor_id,evaluation_round,previous_evaluator,replacement_evaluator,status,failure_reason,created_at)
                           VALUES(?,?,?,?,?,?, 'failed',?,?)""",
                        (seat_id, seat_pre["tender_id"], seat_pre["vendor_id"], seat_pre["evaluation_round"],
                         seat_pre["original_evaluator"], replacement_evaluator, str(exc), now),
                    )
                    failure_id = cur.lastrowid
                    self._audit(pre, seat_pre["tender_id"], actor, "handover.failed",
                                {"seat_id": seat_id, "replacement": replacement_evaluator,
                                 "reason": str(exc), "handover_id": failure_id})
                    pre.commit()
                    raise DomainError(str(exc), exc.status, {"handover_id": failure_id, "seat_id": seat_id}) from exc

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seat = self._get_seat(conn, seat_id)
            tender = self._tender(conn, seat["tender_id"])
            if tender["evaluations_locked"] or tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目阶段不能交接席位", 409)
            now = utcnow()
            pending = conn.execute(
                "SELECT * FROM seat_handovers WHERE seat_id=? AND status='pending' ORDER BY id", (seat_id,)
            ).fetchall()
            if seat["status"] == "active":
                if seat["evaluator"] == replacement_evaluator:
                    return {"seat": dict(seat), "handover": None, "noop": True}
                raise DomainError("席位无需交接", 409)
            if seat["status"] == "handover_pending":
                if len(pending) != 1:
                    raise DomainError("席位交接状态异常", 409)
                handover = pending[0]
                if handover["replacement_evaluator"] == replacement_evaluator:
                    # 同一提名人重试确认：幂等完成。
                    pass
                else:
                    # 监督员改派他人（资格已在预检查中校验）：原提名失败留痕。
                    conn.execute(
                        """UPDATE evaluations SET status='invalidated',invalidated_at=?,invalidation_reason='监督员改派接替人',updated_at=?
                           WHERE seat_id=? AND evaluator=? AND status='pending'""",
                        (now, now, seat_id, handover["replacement_evaluator"]),
                    )
                    conn.execute("UPDATE seat_handovers SET status='failed',failure_reason=? WHERE id=?",
                                 ("监督员改派其他接替人", handover["id"]))
                    handover = self._open_handover(conn, seat, replacement_evaluator, now)
            else:
                if seat["status"] != "recused":
                    raise DomainError("席位当前不需要交接", 409)
                self._validate_replacement(conn, seat["tender_id"], seat["vendor_id"],
                                           seat["evaluation_round"], seat, replacement_evaluator)
                handover = self._open_handover(conn, seat, replacement_evaluator, now)
            # 完成交接：席位归属接替人，提名期间补评的分数转有效。
            try:
                conn.execute(
                    "UPDATE evaluation_seats SET evaluator=?,status='handover_pending',handover_id=?,updated_at=? WHERE id=?",
                    (replacement_evaluator, handover["id"], now, seat_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一专家不能同时占用该供应商的两个评审席位", 409) from exc
            conn.execute(
                "UPDATE evaluations SET status='valid',updated_at=? WHERE seat_id=? AND evaluator=? AND status='pending'",
                (now, seat_id, replacement_evaluator),
            )
            conn.execute(
                "UPDATE seat_handovers SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?",
                (actor, now, handover["id"]),
            )
            conn.execute(
                "UPDATE evaluation_seats SET status='active',updated_at=? WHERE id=?", (now, seat_id)
            )
            self._audit(conn, seat["tender_id"], actor, "handover.confirmed", {
                "seat_id": seat_id, "handover_id": handover["id"],
                "previous": handover["previous_evaluator"], "replacement": replacement_evaluator,
            })
            return {"seat": dict(conn.execute("SELECT * FROM evaluation_seats WHERE id=?", (seat_id,)).fetchone()),
                    "handover": dict(conn.execute("SELECT * FROM seat_handovers WHERE id=?", (handover["id"],)).fetchone())}

    def _open_handover(self, conn: sqlite3.Connection, seat: sqlite3.Row,
                       replacement_evaluator: str, now: str) -> sqlite3.Row:
        cur = conn.execute(
            """INSERT INTO seat_handovers(seat_id,tender_id,vendor_id,evaluation_round,previous_evaluator,replacement_evaluator,status,created_at)
               VALUES(?,?,?,?,?,?,'pending',?)""",
            (seat["id"], seat["tender_id"], seat["vendor_id"], seat["evaluation_round"],
             seat["original_evaluator"], replacement_evaluator, now),
        )
        conn.execute(
            "UPDATE evaluation_seats SET status='handover_pending',handover_id=?,updated_at=? WHERE id=?",
            (cur.lastrowid, now, seat["id"]),
        )
        return conn.execute("SELECT * FROM seat_handovers WHERE id=?", (cur.lastrowid,)).fetchone()

    def rescore_bid(self, actor: str, role: str, seat_id: int, bid_id: int,
                    values: dict[str, float], comment: str = "") -> dict[str, Any]:
        """接替人对回避席位补评；与监督员确认交接并发提交时只收敛到同一个接替人。"""
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "补评")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seat = self._get_seat(conn, seat_id)
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["vendor_id"] != seat["vendor_id"]:
                raise DomainError("投标与席位供应商不一致", 409)
            tender = self._tender(conn, bid["tender_id"])
            if tender["id"] != seat["tender_id"] or tender["evaluation_round"] != seat["evaluation_round"]:
                raise DomainError("席位不属于当前评审轮次", 409)
            if tender["evaluations_locked"] or tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能补评", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能补评", 409)
            now = utcnow()
            handover = None
            if seat["status"] == "active":
                if seat["evaluator"] != actor:
                    raise DomainError("该席位已归属其他评审专家，同一席位只保留一个接替人", 409)
            elif seat["status"] == "handover_pending":
                handover = conn.execute(
                    "SELECT * FROM seat_handovers WHERE seat_id=? AND status='pending' ORDER BY id", (seat_id,)
                ).fetchone()
                if handover is None:
                    confirmed = conn.execute(
                        "SELECT * FROM seat_handovers WHERE seat_id=? AND status='confirmed' ORDER BY id DESC", (seat_id,)
                    ).fetchone()
                    if confirmed and confirmed["replacement_evaluator"] == actor and seat["evaluator"] == actor:
                        handover = confirmed
                    else:
                        raise DomainError("席位交接状态异常", 409)
                if handover["replacement_evaluator"] != actor:
                    raise DomainError("已有另一位接替人待确认，同一席位只保留一个接替人", 409)
            elif seat["status"] == "recused":
                # 专家补评先到：登记交接提名，等监督员确认；分数挂起不参与汇总。
                if actor == seat["original_evaluator"]:
                    raise DomainError("被回避专家不能补评本人席位", 403)
                self._validate_replacement(conn, tender["id"], seat["vendor_id"], tender["evaluation_round"], seat, actor)
                handover = self._open_handover(conn, seat, actor, now)
            else:
                raise DomainError("席位当前不能补评", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与该供应商存在利益冲突", 403)
            criteria = json.loads(tender["criteria"])
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            created = []
            pending_status = "pending" if handover and handover["status"] == "pending" else "valid"
            for criterion in criteria:
                try:
                    raw = float(values[criterion["name"]])
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                if raw < 0 or raw > criterion["max_value"]:
                    raise DomainError("评分值超出范围: " + criterion["name"])
                if criterion["kind"] == "direct":
                    score = raw / criterion["max_value"] * 100
                else:
                    benchmark = criterion["max_value"]
                    score = min(100.0, benchmark / raw * 100) if raw > 0 else 0.0
                existing = conn.execute(
                    "SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND evaluator=? AND criterion=? AND status<>'invalidated'",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"]),
                ).fetchone()
                if existing:
                    raise DomainError("该评分项已提交，不能覆盖", 409)
                cur = conn.execute(
                    """INSERT INTO evaluations(bid_id,evaluation_round,evaluator,seat_id,criterion,raw_value,score,comment,status,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (bid_id, tender["evaluation_round"], actor, seat_id, criterion["name"], raw, score,
                     comment.strip(), pending_status, now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))
            self._audit(conn, tender["id"], actor, "bid.rescored", {
                "bid_id": bid_id, "seat_id": seat_id,
                "handover_id": handover["id"] if handover else None,
                "awaiting_confirmation": pending_status == "pending",
                "criteria": [item["criterion"] for item in created],
            })
            return {"bid_id": bid_id, "seat_id": seat_id, "evaluator": actor,
                    "round": tender["evaluation_round"], "status": pending_status,
                    "handover_id": handover["id"] if handover and handover["status"] == "pending" else None,
                    "evaluations": created}

    def _vendor_review(self, conn: sqlite3.Connection, tender: sqlite3.Row) -> list[dict[str, Any]]:
        """按供应商还原回避、席位、交接、失效和补评结果（当前轮次）。"""
        round_no = tender["evaluation_round"]
        criteria = [c["name"] for c in json.loads(tender["criteria"])]
        bids = conn.execute(
            "SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender["id"],)
        ).fetchall()
        result = []
        for bid in bids:
            vendor = conn.execute("SELECT * FROM vendors WHERE id=?", (bid["vendor_id"],)).fetchone()
            seats = conn.execute(
                "SELECT * FROM evaluation_seats WHERE tender_id=? AND vendor_id=? AND evaluation_round=? ORDER BY id",
                (tender["id"], bid["vendor_id"], round_no),
            ).fetchall()
            seat_payloads = []
            blockers = []
            for seat in seats:
                evaluations = conn.execute(
                    "SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND seat_id=? ORDER BY criterion",
                    (bid["id"], round_no, seat["id"]),
                ).fetchall()
                scored = {e["criterion"] for e in evaluations if e["status"] in ("valid", "pending")}
                seat_missing = [name for name in criteria if name not in scored]
                handovers = [dict(h) for h in conn.execute(
                    "SELECT * FROM seat_handovers WHERE seat_id=? ORDER BY id", (seat["id"],)
                ).fetchall()]
                if seat["status"] == "recused":
                    blockers.append("席位%s专家%s已回避，等待交接补评" % (seat["seat_no"], seat["original_evaluator"]))
                elif seat["status"] == "handover_pending":
                    blockers.append("席位%s交接待监督员确认" % seat["seat_no"])
                elif seat_missing and bid["status"] in {"opened", "qualified"}:
                    blockers.append("席位%s缺少补评项: %s" % (seat["seat_no"], ",".join(seat_missing)))
                seat_payloads.append({
                    "seat": dict(seat), "handovers": handovers,
                    "missing_criteria": seat_missing,
                    "evaluations": [dict(e) for e in evaluations],
                })
            recusals = [dict(r) for r in conn.execute(
                """SELECT * FROM recusals WHERE tender_id=? AND evaluation_round=?
                   AND (vendor_id=? OR vendor_id IS NULL) ORDER BY id""",
                (tender["id"], round_no, bid["vendor_id"]),
            ).fetchall()]
            invalidated = [dict(e) for e in conn.execute(
                """SELECT e.* FROM evaluations e WHERE e.bid_id=? AND e.evaluation_round=? AND e.status='invalidated'
                   ORDER BY e.id""",
                (bid["id"], round_no),
            ).fetchall()]
            unlinked = [dict(e) for e in conn.execute(
                "SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND seat_id IS NULL AND status='valid' ORDER BY id",
                (bid["id"], round_no),
            ).fetchall()]
            scored_criteria = {row["criterion"] for row in conn.execute(
                "SELECT DISTINCT criterion FROM evaluations WHERE bid_id=? AND evaluation_round=? AND status='valid'",
                (bid["id"], round_no),
            ).fetchall()}
            if bid["status"] in {"opened", "qualified"}:
                overall_missing = [name for name in criteria if name not in scored_criteria]
                if overall_missing and not blockers:
                    blockers.append("缺少有效评分项: %s" % ",".join(overall_missing))
            result.append({
                "vendor": dict(vendor) if vendor else {"id": bid["vendor_id"]},
                "bid": dict(bid),
                "recusals": recusals,
                "seats": seat_payloads,
                "invalidated_evaluations": invalidated,
                "unlinked_evaluations": unlinked,
                "ready": not blockers and bid["status"] in {"opened", "qualified"},
                "blockers": blockers,
            })
        return result

    def review_trace(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        if role not in {"procurement", "supervisor", "auditor"}:
            raise DomainError("角色无权查看评审席位还原", 403)
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            payload = self._vendor_review(conn, tender)
            return {
                "tender_id": tender_id,
                "evaluation_round": tender["evaluation_round"],
                "criteria": json.loads(tender["criteria"]),
                "vendors": payload,
            }

    def evaluate_bid(self, actor: str, role: str, bid_id: int, values: dict[str, float],
                     comment: str = "") -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"evaluator"}, "评分")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            tender = self._tender(conn, bid["tender_id"])
            if tender["status"] not in {"opened", "reevaluation"} or tender["evaluations_locked"]:
                raise DomainError("当前项目不能评分", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("该投标不能评分", 409)
            conflict = conn.execute(
                "SELECT 1 FROM conflicts WHERE tender_id=? AND evaluator=? AND (vendor_id=? OR vendor_id IS NULL)",
                (tender["id"], actor, bid["vendor_id"]),
            ).fetchone()
            if conflict:
                raise DomainError("评审人与该供应商存在利益冲突", 403)
            seat = self._seat_for_scoring(conn, tender, bid["vendor_id"], actor)
            criteria = json.loads(tender["criteria"])
            missing = [c["name"] for c in criteria if c["name"] not in values]
            if missing:
                raise DomainError("缺少评分项: " + ",".join(missing))
            created = []
            now = utcnow()
            for criterion in criteria:
                try:
                    raw = float(values[criterion["name"]])
                except (TypeError, ValueError) as exc:
                    raise DomainError("评分值必须是数值") from exc
                if raw < 0 or raw > criterion["max_value"]:
                    raise DomainError("评分值超出范围: " + criterion["name"])
                if criterion["kind"] == "direct":
                    score = raw / criterion["max_value"] * 100
                else:
                    benchmark = criterion["max_value"]
                    score = min(100.0, benchmark / raw * 100) if raw > 0 else 0.0
                existing = conn.execute(
                    """SELECT * FROM evaluations WHERE bid_id=? AND evaluation_round=? AND evaluator=? AND criterion=?""",
                    (bid_id, tender["evaluation_round"], actor, criterion["name"]),
                ).fetchone()
                if existing:
                    raise DomainError("该评分项已提交，不能覆盖", 409)
                cur = conn.execute(
                    """INSERT INTO evaluations(bid_id,evaluation_round,evaluator,seat_id,criterion,raw_value,score,comment,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (bid_id, tender["evaluation_round"], actor, seat["id"], criterion["name"], raw, score, comment.strip(), now, now),
                )
                created.append(dict(conn.execute("SELECT * FROM evaluations WHERE id=?", (cur.lastrowid,)).fetchone()))
            self._audit(conn, tender["id"], actor, "bid.evaluated", {"bid_id": bid_id, "criteria": [item["criterion"] for item in created]})
            return {"bid_id": bid_id, "evaluator": actor, "round": tender["evaluation_round"], "evaluations": created}

    def disqualify_bid(self, actor: str, role: str, bid_id: int, reason: str,
                       expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "废标")
        if not reason.strip():
            raise DomainError("废标理由不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            bid = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
            if not bid:
                raise DomainError("投标不存在", 404)
            if bid["version"] != int(expected_version):
                raise DomainError("投标已变化，请刷新后重试", 409)
            if bid["status"] not in {"opened", "qualified"}:
                raise DomainError("当前投标不能废标", 409)
            conn.execute("UPDATE bids SET status='disqualified',version=version+1 WHERE id=?", (bid_id,))
            self._audit(conn, bid["tender_id"], actor, "bid.disqualified", {"bid_id": bid_id, "reason": reason.strip()})
            return dict(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    def ask_clarification(self, actor: str, role: str, tender_id: int, vendor_id: int, question: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "procurement", "supervisor"}, "提交澄清")
        if not question.strip():
            raise DomainError("澄清问题不能为空")
        with self.connect() as conn:
            self._tender(conn, tender_id)
            cur = conn.execute(
                "INSERT INTO clarifications(tender_id,vendor_id,question,created_at) VALUES(?,?,?,?)",
                (tender_id, vendor_id, question.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "clarification.asked", {"clarification_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (cur.lastrowid,)).fetchone())

    def answer_clarification(self, actor: str, role: str, clarification_id: int,
                             answer: str, publish: bool = True) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"procurement", "supervisor"}, "答复澄清")
        if not answer.strip():
            raise DomainError("澄清答复不能为空")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone()
            if not row:
                raise DomainError("澄清不存在", 404)
            if row["status"] != "pending":
                raise DomainError("澄清已经处理", 409)
            status = "published" if publish else "answered"
            conn.execute(
                "UPDATE clarifications SET answer=?,status=?,answered_by=?,answered_at=? WHERE id=?",
                (answer.strip(), status, actor, utcnow(), clarification_id),
            )
            self._audit(conn, row["tender_id"], actor, "clarification.answered", {"clarification_id": clarification_id, "published": publish})
            return dict(conn.execute("SELECT * FROM clarifications WHERE id=?", (clarification_id,)).fetchone())

    def submit_complaint(self, actor: str, role: str, tender_id: int, body: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"vendor", "evaluator", "procurement", "supervisor"}, "提交投诉")
        if not body.strip():
            raise DomainError("投诉内容不能为空")
        with self.connect() as conn:
            tender = self._tender(conn, tender_id)
            if tender["status"] in {"awarded", "cancelled"}:
                raise DomainError("项目已经结束，不能提交投诉", 409)
            cur = conn.execute(
                "INSERT INTO complaints(tender_id,complainant,body,created_at) VALUES(?,?,?,?)",
                (tender_id, actor, body.strip(), utcnow()),
            )
            self._audit(conn, tender_id, actor, "complaint.submitted", {"complaint_id": cur.lastrowid})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (cur.lastrowid,)).fetchone())

    def resolve_complaint(self, actor: str, role: str, complaint_id: int, decision: str,
                          resolution: str) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "处理投诉")
        if decision not in {"accepted", "rejected"} or not resolution.strip():
            raise DomainError("投诉决定或处理说明无效")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            complaint = conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
            if not complaint:
                raise DomainError("投诉不存在", 404)
            if complaint["status"] != "open":
                raise DomainError("投诉已经处理", 409)
            conn.execute(
                "UPDATE complaints SET status=?,resolution=?,reviewed_by=?,resolved_at=? WHERE id=?",
                (decision, resolution.strip(), actor, utcnow(), complaint_id),
            )
            if decision == "accepted":
                tender = self._tender(conn, complaint["tender_id"])
                if tender["status"] in {"awarded", "cancelled"}:
                    raise DomainError("已结束项目不能重新评审", 409)
                conn.execute(
                    "UPDATE tenders SET status='reevaluation',evaluation_round=evaluation_round+1,evaluations_locked=0,version=version+1,updated_at=? WHERE id=?",
                    (utcnow(), tender["id"]),
                )
            self._audit(conn, complaint["tender_id"], actor, "complaint.resolved", {"complaint_id": complaint_id, "decision": decision})
            return dict(conn.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone())

    def award_tender(self, actor: str, role: str, tender_id: int, expected_version: int) -> dict[str, Any]:
        actor = clean_actor(actor)
        require_role(role, {"supervisor"}, "授标")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tender = self._tender(conn, tender_id)
            if tender["status"] not in {"opened", "reevaluation"}:
                raise DomainError("当前项目不能授标", 409)
            if tender["version"] != int(expected_version):
                raise DomainError("项目已变化，请刷新后重试", 409)
            open_complaint = conn.execute("SELECT COUNT(*) AS c FROM complaints WHERE tender_id=? AND status='open'", (tender_id,)).fetchone()["c"]
            if open_complaint:
                raise DomainError("存在未处理投诉，不能授标", 409)
            vendor_review = self._vendor_review(conn, tender)
            blocked = []
            for item in vendor_review:
                if item["bid"]["status"] in {"opened", "qualified"} and item["blockers"]:
                    blocked.append({"vendor_id": item["vendor"].get("id"),
                                    "vendor_name": item["vendor"].get("name"),
                                    "blockers": item["blockers"]})
            if blocked:
                raise DomainError("存在回避交接未完成或缺项补评，不能授标（其他供应商不受影响）", 409,
                                  {"blocked_vendors": blocked})
            bids = conn.execute("SELECT * FROM bids WHERE tender_id=? AND status IN ('opened','qualified')", (tender_id,)).fetchall()
            criteria = json.loads(tender["criteria"])
            expected_criteria = {c["name"] for c in criteria}
            ranking = []
            for bid in bids:
                rows = conn.execute(
                    """SELECT criterion,AVG(score) AS score FROM evaluations
                       WHERE bid_id=? AND evaluation_round=? AND status='valid' GROUP BY criterion""",
                    (bid["id"], tender["evaluation_round"]),
                ).fetchall()
                scores = {row["criterion"]: row["score"] for row in rows}
                if set(scores) != expected_criteria:
                    raise DomainError("投标尚未完成全部有效评分: %s" % bid["id"], 409)
                weighted = 0.0
                for criterion in criteria:
                    weighted += scores[criterion["name"]] * criterion["weight"] / 100
                ranking.append({"bid_id": bid["id"], "vendor_id": bid["vendor_id"], "price": bid["price"], "score": round(weighted, 2)})
            if not ranking:
                raise DomainError("没有可授标的有效投标", 409)
            ranking.sort(key=lambda item: (-item["score"], item["price"], item["bid_id"]))
            winner = ranking[0]
            active_recusals = conn.execute(
                "SELECT COUNT(*) AS c FROM recusals WHERE tender_id=? AND evaluation_round=? AND status='active'",
                (tender_id, tender["evaluation_round"]),
            ).fetchone()["c"]
            replaced_seats = conn.execute(
                "SELECT COUNT(*) AS c FROM seat_handovers WHERE tender_id=? AND evaluation_round=? AND status='confirmed'",
                (tender_id, tender["evaluation_round"]),
            ).fetchone()["c"]
            invalidated = conn.execute(
                """SELECT COUNT(*) AS c FROM evaluations e JOIN bids b ON b.id=e.bid_id
                   WHERE b.tender_id=? AND e.evaluation_round=? AND e.status='invalidated'""",
                (tender_id, tender["evaluation_round"]),
            ).fetchone()["c"]
            review_summary = {"active_recusals": active_recusals, "confirmed_handovers": replaced_seats,
                              "invalidated_evaluations": invalidated}
            snapshot = {"tender_id": tender_id, "round": tender["evaluation_round"], "ranking": ranking,
                        "winner": winner, "review": review_summary, "awarded_by": actor, "awarded_at": utcnow()}
            conn.execute(
                "UPDATE tenders SET status='awarded',awarded_bid_id=?,award_snapshot=?,evaluations_locked=1,version=version+1,updated_at=? WHERE id=? AND version=?",
                (winner["bid_id"], json.dumps(snapshot, ensure_ascii=False), utcnow(), tender_id, expected_version),
            )
            conn.execute("UPDATE bids SET status='awarded',version=version+1 WHERE id=?", (winner["bid_id"],))
            self._audit(conn, tender_id, actor, "tender.awarded", {"winner": winner, "ranking": ranking})
            return {"tender": dict(self._tender(conn, tender_id)), "award": snapshot}

    def get_tender(self, actor: str, role: str, tender_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            tender = dict(self._tender(conn, tender_id))
            bids = []
            if role in {"procurement", "supervisor", "auditor"} and tender["status"] in {"opened", "reevaluation", "awarded"}:
                bids = [dict(r) for r in conn.execute("SELECT * FROM bids WHERE tender_id=? ORDER BY id", (tender_id,)).fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    "SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id WHERE b.tender_id=? AND b.submitted_by=?",
                    (tender_id, actor),
                ).fetchall():
                    item = dict(row)
                    item.pop("tender_status", None)
                    if tender["status"] not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
            else:
                bids = [dict(r) for r in conn.execute(
                    "SELECT id,tender_id,vendor_id,price,status,payload_hash,submitted_at,opened_at FROM bids WHERE tender_id=? ORDER BY id",
                    (tender_id,),
                ).fetchall()]
            clarifications = [dict(r) for r in conn.execute(
                "SELECT id,tender_id,vendor_id,question,answer,status,answered_at FROM clarifications WHERE tender_id=? AND status='published' ORDER BY id",
                (tender_id,),
            ).fetchall()]
            payload = {"tender": tender, "bids": bids, "clarifications": clarifications}
            if role in {"procurement", "supervisor", "auditor"}:
                payload["review"] = self._vendor_review(conn, tender)
            return payload

    def state(self, actor: str = "", role: str = "public") -> dict[str, Any]:
        with self.connect() as conn:
            tenders = [dict(r) for r in conn.execute(
                "SELECT id,tender_no,title,description,status,deadline,evaluation_round,version,awarded_bid_id,created_at,updated_at FROM tenders ORDER BY id DESC"
            ).fetchall()]
            timeline = [dict(r) for r in conn.execute("SELECT * FROM timeline ORDER BY id DESC LIMIT 200").fetchall()]
            if role in {"procurement", "supervisor", "auditor"}:
                bids = [dict(r) for r in conn.execute(
                    """SELECT b.id,b.tender_id,b.vendor_id,b.price,b.status,b.payload_hash,b.submitted_at,b.opened_at,
                              CASE WHEN t.status IN ('opened','reevaluation','awarded') THEN b.payload ELSE NULL END AS payload
                       FROM bids b JOIN tenders t ON t.id=b.tender_id ORDER BY b.id DESC LIMIT 200"""
                ).fetchall()]
                complaints = [dict(r) for r in conn.execute("SELECT * FROM complaints ORDER BY id DESC LIMIT 100").fetchall()]
            elif role == "vendor":
                bids = []
                for row in conn.execute(
                    """SELECT b.*,t.status AS tender_status FROM bids b JOIN tenders t ON t.id=b.tender_id
                       WHERE b.submitted_by=? ORDER BY b.id DESC LIMIT 100""",
                    (actor,),
                ).fetchall():
                    item = dict(row)
                    status = item.pop("tender_status")
                    if status not in {"opened", "reevaluation", "awarded"}:
                        item.pop("payload", None)
                    bids.append(item)
                complaints = [dict(r) for r in conn.execute(
                    "SELECT * FROM complaints WHERE complainant=? ORDER BY id DESC LIMIT 100", (actor,)
                ).fetchall()]
            else:
                bids, complaints = [], []
            review = []
            if role in {"procurement", "supervisor", "auditor"}:
                review = [dict(r) for r in conn.execute(
                    """SELECT s.id,s.seat_no,s.tender_id,s.vendor_id,s.evaluation_round,s.evaluator,s.status,
                              s.original_evaluator,s.recusal_id,s.handover_id,s.updated_at
                       FROM evaluation_seats s ORDER BY s.id DESC LIMIT 200"""
                ).fetchall()]
                recusals = [dict(r) for r in conn.execute(
                    "SELECT * FROM recusals ORDER BY id DESC LIMIT 200"
                ).fetchall()]
            else:
                recusals = []
        return {"tenders": tenders, "bids": bids, "complaints": complaints, "timeline": timeline,
                "review_seats": review, "recusals": recusals, "role": role}

    def seed_demo(self) -> dict[str, Any]:
        with self.connect() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM tenders").fetchone()["c"]:
                return {"seeded": False, "reason": "已有数据"}
        vendor = self.create_vendor("proc-demo", "procurement", "V-001", "启明科技", "vendor-demo")
        deadline = (datetime.now(timezone.utc) + __import__("datetime").timedelta(hours=1)).isoformat(timespec="seconds")
        tender = self.create_tender(
            "proc-demo", "procurement", "TENDER-DEMO", "服务器采购", deadline,
            [{"name": "价格", "weight": 60, "kind": "cost", "max_value": 1000000},
             {"name": "质量", "weight": 40, "kind": "direct", "max_value": 100}],
        )
        published = self.publish_tender("proc-demo", "procurement", tender["id"], tender["version"])
        self.submit_bid("vendor-demo", "vendor", tender["id"], vendor["id"], {"价格": 900000, "质量": 90}, 900000)
        return {"seeded": True, "tender_id": tender["id"], "vendor_id": vendor["id"], "published_version": published["version"]}


class ApiHandler(BaseHTTPRequestHandler):
    service: ProcurementService

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _headers(self) -> tuple[str, str]:
        return self.headers.get("X-User", ""), self.headers.get("X-Role", "public")

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise DomainError("请求体过大", 413)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise DomainError("JSON 请求体必须是对象")
        return value

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path in {"/", "/index.html"}:
                body = (ROOT / "static" / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            actor, role = self._headers()
            if path == "/health":
                self._send(200, {"status": "ok", "service": "public-procurement"})
            elif path == "/api/state":
                self._send(200, self.service.state(actor, role))
            elif path.startswith("/api/tenders/"):
                parts = path.split("/")
                tender_pk = int(parts[3])
                if len(parts) == 5 and parts[4] == "review":
                    self._send(200, self.service.review_trace(actor, role, tender_pk))
                else:
                    self._send(200, self.service.get_tender(actor, role, tender_pk))
            else:
                self._send(404, {"error": "接口不存在"})
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc), **({"details": exc.details} if exc.details else {})})
        except (ValueError, IndexError) as exc:
            self._send(400, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            path, data, (actor, role) = urlparse(self.path).path, self._json(), self._headers()
            if path == "/api/vendors":
                result = self.service.create_vendor(actor, role, **data)
            elif path == "/api/tenders":
                result = self.service.create_tender(actor, role, **data)
            elif path == "/api/tenders/publish":
                result = self.service.publish_tender(actor, role, **data)
            elif path == "/api/bids":
                result = self.service.submit_bid(actor, role, **data)
            elif path == "/api/bids/withdraw":
                result = self.service.withdraw_bid(actor, role, **data)
            elif path == "/api/tenders/open":
                result = self.service.open_bids(actor, role, **data)
            elif path == "/api/conflicts":
                result = self.service.declare_conflict(actor, role, **data)
            elif path == "/api/recusals":
                result = self.service.recuse_evaluator(actor, role, **data)
            elif path == "/api/recusals/revoke":
                result = self.service.revoke_recusal(actor, role, **data)
            elif path == "/api/seats/handover":
                result = self.service.confirm_handover(actor, role, **data)
            elif path == "/api/evaluations/rescore":
                result = self.service.rescore_bid(actor, role, **data)
            elif path == "/api/evaluations":
                result = self.service.evaluate_bid(actor, role, **data)
            elif path == "/api/bids/disqualify":
                result = self.service.disqualify_bid(actor, role, **data)
            elif path == "/api/clarifications":
                result = self.service.ask_clarification(actor, role, **data)
            elif path == "/api/clarifications/answer":
                result = self.service.answer_clarification(actor, role, **data)
            elif path == "/api/complaints":
                result = self.service.submit_complaint(actor, role, **data)
            elif path == "/api/complaints/resolve":
                result = self.service.resolve_complaint(actor, role, **data)
            elif path == "/api/tenders/award":
                result = self.service.award_tender(actor, role, **data)
            else:
                raise DomainError("接口不存在", 404)
            self._send(201, result)
        except DomainError as exc:
            self._send(exc.status, {"error": str(exc), **({"details": exc.details} if exc.details else {})})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": "请求参数错误: %s" % exc})
        except Exception as exc:
            self._send(500, {"error": "服务器内部错误", "detail": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def serve(service: ProcurementService, host: str, port: int) -> None:
    ApiHandler.service = service
    server = ThreadingHTTPServer((host, port), ApiHandler)
    print("Public procurement service listening on http://%s:%s" % (host, port))
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="公共采购密封投标服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8209)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = ProcurementService(args.db)
    if args.init:
        print(json.dumps(service.seed_demo() if args.seed else {"initialized": True, "db": args.db}, ensure_ascii=False))
        return
    serve(service, args.host, args.port)


if __name__ == "__main__":
    main()
