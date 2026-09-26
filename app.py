#!/usr/bin/env python3
"""Pharmaceutical batch deviation, rework and release decision service."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path(__file__).with_name("data.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def after_now(value: str | None = None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) > datetime.now(timezone.utc)
    except ValueError:
        return False


def j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message); self.status, self.message = status, message


class Store:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False); self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON"); self.conn.execute("PRAGMA journal_mode=WAL"); self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS factories (
          id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, country TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER NOT NULL REFERENCES factories(id),
          batch_no TEXT NOT NULL, product TEXT NOT NULL, mfg_date TEXT NOT NULL, expiry_date TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('manufactured','investigation','awaiting_resample','conditional','released','rejected','recall_pending')),
          revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          quantity REAL NOT NULL DEFAULT 0,
          UNIQUE(factory_id,batch_no)
        );
        CREATE TABLE IF NOT EXISTS deviations (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          severity TEXT NOT NULL CHECK(severity IN ('critical','minor')), title TEXT NOT NULL, due_at TEXT,
          status TEXT NOT NULL CHECK(status IN ('open','closed')), corrective_action TEXT,
          exception_reason TEXT, exception_until TEXT, exception_approved_by TEXT,
          closed_by TEXT, closed_at TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tests (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          test_type TEXT NOT NULL, result REAL NOT NULL, spec_min REAL NOT NULL, spec_max REAL NOT NULL,
          passed INTEGER NOT NULL, round INTEGER NOT NULL DEFAULT 1, recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rework (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          description TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('planned','completed')),
          created_by TEXT NOT NULL, created_at TEXT NOT NULL, completed_by TEXT, completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS supplier_changes (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          supplier TEXT NOT NULL, change_type TEXT NOT NULL, description TEXT NOT NULL,
          recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stability (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id),
          condition TEXT NOT NULL, timepoint TEXT NOT NULL, result REAL NOT NULL, spec_limit REAL NOT NULL,
          passed INTEGER NOT NULL, recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS decisions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES batches(id), revision INTEGER NOT NULL,
          decision TEXT NOT NULL CHECK(decision IN ('release','reject','conditional','resample')), rationale TEXT NOT NULL,
          exception_code TEXT, decided_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(batch_id,revision)
        );
        CREATE TABLE IF NOT EXISTS audit_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
          entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, details_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batch_lineage (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          upstream_id INTEGER NOT NULL REFERENCES batches(id),
          downstream_id INTEGER NOT NULL REFERENCES batches(id),
          quantity REAL NOT NULL CHECK(quantity > 0),
          registered_by TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(upstream_id,downstream_id)
        );
        CREATE TABLE IF NOT EXISTS recall_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL REFERENCES batches(id),
          source_id INTEGER NOT NULL REFERENCES batches(id),
          trigger_type TEXT NOT NULL CHECK(trigger_type IN ('reject','critical_open','lineage_link')),
          ref_id INTEGER,
          prior_state TEXT NOT NULL,
          withdrawn_from_release INTEGER NOT NULL DEFAULT 0,
          reason TEXT NOT NULL,
          created_by TEXT NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(batch_id,source_id,trigger_type,ref_id)
        );
        CREATE TABLE IF NOT EXISTS recall_reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          batch_id INTEGER NOT NULL REFERENCES batches(id),
          action TEXT NOT NULL CHECK(action IN ('note','reject','release','conditional')),
          note TEXT NOT NULL, reviewed_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        self._migrate_schema()
        self.conn.commit()

    def _migrate_schema(self) -> None:
        """Add quantity column and recall_pending state to pre-genealogy databases."""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(batches)")}
        if "quantity" not in cols:
            self.conn.execute("ALTER TABLE batches ADD COLUMN quantity REAL NOT NULL DEFAULT 0")
        check = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='batches'").fetchone()[0]
        if "recall_pending" not in check:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript("""
            CREATE TABLE batches_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT, factory_id INTEGER NOT NULL REFERENCES factories(id),
              batch_no TEXT NOT NULL, product TEXT NOT NULL, mfg_date TEXT NOT NULL, expiry_date TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('manufactured','investigation','awaiting_resample','conditional','released','rejected','recall_pending')),
              revision INTEGER NOT NULL DEFAULT 1, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              quantity REAL NOT NULL DEFAULT 0,
              UNIQUE(factory_id,batch_no)
            );
            INSERT INTO batches_new(id,factory_id,batch_no,product,mfg_date,expiry_date,state,revision,created_by,created_at,updated_at,quantity)
            SELECT id,factory_id,batch_no,product,mfg_date,expiry_date,state,revision,created_by,created_at,updated_at,quantity FROM batches;
            DROP TABLE batches;
            ALTER TABLE batches_new RENAME TO batches;
            """)
            self.conn.execute("PRAGMA foreign_keys=ON")

    def audit(self, actor: str, action: str, entity_type: str, entity_id: object, details: dict) -> None:
        self.conn.execute("INSERT INTO audit_log(at,actor,action,entity_type,entity_id,details_json) VALUES(?,?,?,?,?,?)",
                          (now(), actor, action, entity_type, str(entity_id), j(details)))

    def close(self) -> None:
        self.conn.close()


class BatchService:
    def __init__(self, store: Store): self.store, self.conn = store, store.conn

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _row(self, table: str, identity: int) -> sqlite3.Row:
        row = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
        if not row: raise ApiError(404, "对象不存在")
        return row

    def _factory_check(self, actor: str, factory_id: int, batch: sqlite3.Row | None = None) -> None:
        factory = self.conn.execute("SELECT * FROM factories WHERE id=?", (factory_id,)).fetchone()
        if not factory: raise ApiError(404, "工厂不存在")
        if batch is not None and int(batch["factory_id"]) != int(factory_id):
            raise ApiError(403, "不能修改其他工厂的批次")

    def register_factory(self, actor: str | None, role: str | None, code: str, name: str, country: str) -> dict:
        actor = self._actor(actor, role, {"qa"})
        if not code or not name: raise ApiError(400, "工厂代号和名称不能为空")
        try:
            with self.conn:
                cur = self.conn.execute("INSERT INTO factories(code,name,country) VALUES(?,?,?)", (code, name, country))
                self.store.audit(actor, "factory.register", "factory", cur.lastrowid, {"code": code})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "工厂代号已存在") from exc
        return {"id": cur.lastrowid, "code": code, "name": name, "country": country}

    def create_batch(self, actor: str | None, role: str | None, factory_id: int, batch_no: str, product: str, mfg_date: str, expiry_date: str, quantity: float = 0) -> dict:
        actor = self._actor(actor, role, {"operator"})
        self._factory_check(actor, factory_id)
        if not batch_no.strip() or not product.strip() or expiry_date <= mfg_date: raise ApiError(400, "批号、产品或有效期不合法")
        try: quantity = float(quantity)
        except (TypeError, ValueError): raise ApiError(400, "批次数量不合法")
        if quantity < 0: raise ApiError(400, "批次数量不能为负")
        stamp = now()
        try:
            with self.conn:
                cur = self.conn.execute("""INSERT INTO batches(factory_id,batch_no,product,mfg_date,expiry_date,state,quantity,created_by,created_at,updated_at)
                                         VALUES(?,?,?,?,?, 'manufactured',?,?,?,?)""",
                                        (factory_id, batch_no, product, mfg_date, expiry_date, quantity, actor, stamp, stamp))
                self.store.audit(actor, "batch.create", "batch", cur.lastrowid, {"factory_id": factory_id, "batch_no": batch_no, "quantity": quantity})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "该工厂批号已存在") from exc
        return self._batch_dict(self._row("batches", cur.lastrowid))

    def add_deviation(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, severity: str, title: str, due_at: str | None, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if severity not in {"critical", "minor"} or not title.strip(): raise ApiError(400, "偏差等级或描述不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "已终态批次不能新增偏差")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO deviations(batch_id,severity,title,due_at,status,created_by,created_at)
                                     VALUES(?,?,?,?,'open',?,?)""", (batch_id, severity, title, due_at, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            if severity == "critical":
                self._propagate_recall(batch_id, "critical_open", cur.lastrowid, f"未关闭关键偏差 #{cur.lastrowid}：{title}", actor)
            self.store.audit(actor, "deviation.open", "deviation", cur.lastrowid, {"batch_id": batch_id, "severity": severity})
        return self._deviation_dict(self._row("deviations", cur.lastrowid))

    def close_deviation(self, actor: str | None, role: str | None, deviation_id: int, corrective_action: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id); batch = self._row("batches", deviation["batch_id"])
        if deviation["status"] != "open": raise ApiError(409, "偏差已经关闭")
        if not corrective_action.strip(): raise ApiError(400, "必须填写纠正措施")
        with self.conn:
            self.conn.execute("UPDATE deviations SET status='closed',corrective_action=?,closed_by=?,closed_at=? WHERE id=? AND status='open'",
                              (corrective_action, actor, now(), deviation_id))
            self._advance_batch(batch["id"], expected_revision, "investigation")
            self.store.audit(actor, "deviation.close", "deviation", deviation_id, {"batch_id": batch["id"], "corrective_action": corrective_action})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def approve_exception(self, actor: str | None, role: str | None, deviation_id: int, reason: str, until: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"qa"})
        deviation = self._row("deviations", deviation_id); batch = self._row("batches", deviation["batch_id"])
        if deviation["severity"] == "critical": raise ApiError(409, "关键偏差不允许例外批准")
        if deviation["status"] != "open" or not reason.strip() or not after_now(until): raise ApiError(400, "例外原因或有效期不合法")
        with self.conn:
            self.conn.execute("UPDATE deviations SET exception_reason=?,exception_until=?,exception_approved_by=? WHERE id=?", (reason, until, actor, deviation_id))
            self._advance_batch(batch["id"], expected_revision, batch["state"])
            self.store.audit(actor, "deviation.exception", "deviation", deviation_id, {"batch_id": batch["id"], "reason": reason, "until": until})
        return self._deviation_dict(self._row("deviations", deviation_id))

    def record_test(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, test_type: str, result: float, spec_min: float, spec_max: float, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if not test_type.strip() or spec_min > spec_max: raise ApiError(400, "检验项目或标准不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不能补录检验")
        round_no = self.conn.execute("SELECT COALESCE(MAX(round),0)+1 FROM tests WHERE batch_id=? AND test_type=?", (batch_id, test_type)).fetchone()[0]
        passed = int(spec_min <= result <= spec_max)
        with self.conn:
            cur = self.conn.execute("""INSERT INTO tests(batch_id,test_type,result,spec_min,spec_max,passed,round,recorded_by,created_at)
                                     VALUES(?,?,?,?,?,?,?,?,?)""", (batch_id, test_type, result, spec_min, spec_max, passed, round_no, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation" if (batch["state"] == "awaiting_resample" or not passed) else batch["state"])
            self.store.audit(actor, "test.record", "batch", batch_id, {"test_type": test_type, "result": result, "passed": bool(passed), "round": round_no})
        return self._test_dict(self._row("tests", cur.lastrowid))

    def plan_rework(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, description: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不能返工")
        with self.conn:
            cur = self.conn.execute("INSERT INTO rework(batch_id,description,status,created_by,created_at) VALUES(?,?,'planned',?,?)", (batch_id, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "rework.plan", "rework", cur.lastrowid, {"batch_id": batch_id, "description": description})
        return dict(self._row("rework", cur.lastrowid))

    def complete_rework(self, actor: str | None, role: str | None, factory_id: int, rework_id: int, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator"})
        row = self._row("rework", rework_id); batch = self._row("batches", row["batch_id"]); self._factory_check(actor, factory_id, batch)
        if row["status"] != "planned": raise ApiError(409, "返工记录已经完成")
        with self.conn:
            self.conn.execute("UPDATE rework SET status='completed',completed_by=?,completed_at=? WHERE id=?", (actor, now(), rework_id))
            self._advance_batch(batch["id"], expected_revision, "investigation")
            self.store.audit(actor, "rework.complete", "rework", rework_id, {"batch_id": batch["id"]})
        return dict(self._row("rework", rework_id))

    def record_supplier_change(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, supplier: str, change_type: str, description: str, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "qa"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        with self.conn:
            cur = self.conn.execute("INSERT INTO supplier_changes(batch_id,supplier,change_type,description,recorded_by,created_at) VALUES(?,?,?,?,?,?)",
                                    (batch_id, supplier, change_type, description, actor, now()))
            self._advance_batch(batch_id, expected_revision, "investigation")
            self.store.audit(actor, "supplier_change.record", "batch", batch_id, {"supplier": supplier, "change_type": change_type})
        return dict(self._row("supplier_changes", cur.lastrowid))

    def record_stability(self, actor: str | None, role: str | None, factory_id: int, batch_id: int, condition: str, timepoint: str, result: float, spec_limit: float, expected_revision: int) -> dict:
        actor = self._actor(actor, role, {"lab"})
        batch = self._row("batches", batch_id); self._factory_check(actor, factory_id, batch)
        passed = int(result <= spec_limit)
        with self.conn:
            cur = self.conn.execute("INSERT INTO stability(batch_id,condition,timepoint,result,spec_limit,passed,recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                    (batch_id, condition, timepoint, result, spec_limit, passed, actor, now()))
            self._advance_batch(batch_id, expected_revision, batch["state"])
            self.store.audit(actor, "stability.record", "batch", batch_id, {"condition": condition, "timepoint": timepoint, "passed": bool(passed)})
        return dict(self._row("stability", cur.lastrowid))

    # ---------- 批次血缘 ----------
    def _downstream_map(self) -> dict[int, list[int]]:
        graph: dict[int, list[int]] = {}
        for row in self.conn.execute("SELECT upstream_id,downstream_id FROM batch_lineage"):
            graph.setdefault(row["upstream_id"], []).append(row["downstream_id"])
        return graph

    def _descendants(self, batch_id: int, include_self: bool, graph: dict[int, list[int]] | None = None) -> set[int]:
        graph = graph or self._downstream_map()
        seen, frontier = set(), list(graph.get(batch_id, ()))
        if include_self: seen.add(batch_id)
        while frontier:
            cur = frontier.pop()
            if cur in seen: continue
            seen.add(cur); frontier.extend(graph.get(cur, ()))
        return seen

    def _available_quantity(self, batch_id: int) -> float:
        batch = self._row("batches", batch_id)
        used = self.conn.execute("SELECT COALESCE(SUM(quantity),0) FROM batch_lineage WHERE upstream_id=?", (batch_id,)).fetchone()[0]
        return float(batch["quantity"]) - float(used)

    def _active_blockers(self, batch_id: int) -> list[dict]:
        """沿血缘向上查找仍在生效的阻塞来源：上游拒收批、上游未关闭关键偏差（含自身）。"""
        parents: dict[int, list[int]] = {}
        for row in self.conn.execute("SELECT upstream_id,downstream_id FROM batch_lineage"):
            parents.setdefault(row["downstream_id"], []).append(row["upstream_id"])
        seen, frontier, blockers = set(), [batch_id], []
        while frontier:
            cur = frontier.pop()
            if cur in seen: continue
            seen.add(cur)
            batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (cur,)).fetchone()
            if not batch: continue
            if batch["state"] == "rejected":
                blockers.append({"source_id": cur, "source_batch_no": batch["batch_no"], "kind": "rejected",
                                 "message": f"上游批次 {batch['batch_no']} 已拒收"})
            for d in self.conn.execute("SELECT * FROM deviations WHERE batch_id=? AND severity='critical' AND status='open' ORDER BY id", (cur,)):
                blockers.append({"source_id": cur, "source_batch_no": batch["batch_no"], "kind": "critical_deviation",
                                 "deviation_id": d["id"], "message": f"上游批次 {batch['batch_no']} 存在未关闭关键偏差 #{d['id']} {d['title']}"})
            frontier.extend(parents.get(cur, ()))
        return sorted(blockers, key=lambda b: (b["source_id"], b["kind"], b.get("deviation_id") or 0))

    def register_lineage(self, actor: str | None, role: str | None, factory_id: int, upstream_id: int,
                         downstream_id: int, quantity: float, upstream_revision: int) -> dict:
        actor = self._actor(actor, role, {"operator", "qa"})
        self._factory_check(actor, factory_id)
        try: upstream_id, downstream_id = int(upstream_id), int(downstream_id)
        except (TypeError, ValueError): raise ApiError(400, "批次编号不合法")
        try: quantity = float(quantity)
        except (TypeError, ValueError): raise ApiError(400, "投入数量不合法")
        if quantity <= 0: raise ApiError(400, "投入数量必须为正数")
        if upstream_id == downstream_id: raise ApiError(409, "批次不能作为自身上游，血缘不允许成环")
        upstream, downstream = self._row("batches", upstream_id), self._row("batches", downstream_id)
        if int(upstream["factory_id"]) != int(factory_id) or int(downstream["factory_id"]) != int(factory_id):
            raise ApiError(403, "上下游批次必须属于同一工厂，跨厂血缘关系拒绝登记")
        if int(upstream["revision"]) != int(upstream_revision): raise ApiError(409, "上游批次版本冲突，请刷新版本")
        if downstream["state"] == "rejected": raise ApiError(409, "下游批次已拒收，不能再登记投入")
        if upstream_id in self._descendants(downstream_id, include_self=True):
            raise ApiError(409, "登记后血缘会成环，关系不落库")
        available = self._available_quantity(upstream_id)
        if quantity > available + 1e-9:
            raise ApiError(409, f"投入量 {quantity:g} 超过来源批可用量 {available:g}")
        stamp = now()
        with self.conn:
            try:
                cur = self.conn.execute("""INSERT INTO batch_lineage(upstream_id,downstream_id,quantity,registered_by,created_at)
                                         VALUES(?,?,?,?,?)""", (upstream_id, downstream_id, quantity, actor, stamp))
            except sqlite3.IntegrityError as exc: raise ApiError(409, "该上下游关系已登记") from exc
            # 投入消耗来源批可用量，推进上游修订号让并发登记刷新
            self.conn.execute("UPDATE batches SET revision=revision+1,updated_at=? WHERE id=?", (stamp, upstream_id))
            # 新边可能把既有阻塞传播给下游，链路登记时仅当上游当前被阻塞才补传播
            upstream_now = self._row("batches", upstream_id)
            if upstream_now["state"] == "rejected" or self.conn.execute(
                    "SELECT 1 FROM deviations WHERE batch_id=? AND severity='critical' AND status='open' LIMIT 1",
                    (upstream_id,)).fetchone():
                blocker_msgs = "；".join(b["message"] for b in self._active_blockers(upstream_id))
                self._propagate_recall(upstream_id, "lineage_link", cur.lastrowid,
                                       f"血缘登记带入阻塞：{upstream['batch_no']} -> {downstream['batch_no']}（{blocker_msgs}）", actor)
            self.store.audit(actor, "lineage.register", "batch_lineage", cur.lastrowid,
                             {"upstream_id": upstream_id, "downstream_id": downstream_id, "quantity": quantity})
        return self._lineage_dict(self._row("batch_lineage", cur.lastrowid))

    def _propagate_recall(self, source_id: int, trigger_type: str, ref_id: int | None, reason: str, actor: str) -> None:
        """拒收或关键偏差后，将所有未拒收下游批次置为召回待审；已放行批撤回但保留原决定。"""
        graph = self._downstream_map()
        affected = self._descendants(source_id, include_self=False, graph=graph)
        stamp = now()
        for batch_id in sorted(affected):
            batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not batch or batch["state"] == "rejected": continue
            prior, withdrawn = batch["state"], int(batch["state"] in {"released", "conditional"})
            if batch["state"] != "recall_pending":
                self.conn.execute("UPDATE batches SET state='recall_pending',revision=revision+1,updated_at=? WHERE id=?", (stamp, batch_id))
            try:
                self.conn.execute("""INSERT INTO recall_events(batch_id,source_id,trigger_type,ref_id,prior_state,withdrawn_from_release,reason,created_by,created_at)
                                   VALUES(?,?,?,?,?,?,?,?,?)""",
                                  (batch_id, source_id, trigger_type, ref_id, prior, withdrawn, reason, actor, stamp))
            except sqlite3.IntegrityError:
                pass  # 同一触发原因已记录，幂等跳过
            else:
                self.store.audit(actor, "recall.pending", "batch", batch_id,
                                 {"source_id": source_id, "trigger": trigger_type, "prior_state": prior,
                                  "withdrawn_from_release": bool(withdrawn), "reason": reason})

    def review_recall(self, actor: str | None, role: str | None, batch_id: int, action: str, note: str) -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if batch["state"] != "recall_pending": raise ApiError(409, "批次不处于召回待审状态")
        if action not in {"note", "reject", "release", "conditional"}: raise ApiError(400, "召回审查动作不合法")
        if not note.strip(): raise ApiError(400, "必须填写召回审查意见")
        blockers = self._active_blockers(batch_id)
        stamp = now()
        with self.conn:
            cur = self.conn.execute("INSERT INTO recall_reviews(batch_id,action,note,reviewed_by,created_at) VALUES(?,?,?,?,?)",
                                    (batch_id, action, note, actor, stamp))
            self.store.audit(actor, "recall.review", "recall_review", cur.lastrowid, {"batch_id": batch_id, "action": action})
            if action == "reject":
                self.conn.execute("UPDATE batches SET state='rejected',revision=revision+1,updated_at=? WHERE id=?", (stamp, batch_id))
                self._propagate_recall(batch_id, "reject", None, f"召回审查拒收：{note}", actor)
            elif action in {"release", "conditional"} and not blockers:
                new_state = "released" if action == "release" else "conditional"
                self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=?", (new_state, stamp, batch_id))
        result = {"review": dict(self._row("recall_reviews", cur.lastrowid)), "batch": self.batch_detail(batch_id)["batch"]}
        if blockers and action in {"release", "conditional"}:
            result["warning"] = "上游阻塞来源仍然存在，维持召回待审；处理后请通过放行决定流程重新放行"
        return result

    def decide(self, actor: str | None, role: str | None, batch_id: int, decision: str, rationale: str, expected_revision: int, exception_code: str = "") -> dict:
        actor = self._actor(actor, role, {"qa"})
        batch = self._row("batches", batch_id)
        if decision not in {"release", "reject", "conditional", "resample"}: raise ApiError(400, "放行决定不合法")
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "批次已经是终态")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次已被其他工厂或质量人员修改，请刷新版本")
        if not rationale.strip(): raise ApiError(400, "必须填写决定依据")
        upstream_blockers = self._active_blockers(batch_id) if decision in {"release", "conditional"} else []
        if upstream_blockers:
            raise ApiError(409, "血缘上游存在阻塞来源，不能放行：" + "；".join(b["message"] for b in upstream_blockers))
        deviations = self.conn.execute("SELECT * FROM deviations WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        open_deviations = [d for d in deviations if d["status"] == "open"]
        latest_tests: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute("SELECT * FROM tests WHERE batch_id=? ORDER BY id", (batch_id,)):
            latest_tests[row["test_type"]] = row
        if decision in {"release", "conditional"} and not latest_tests:
            raise ApiError(409, "放行前至少需要一项检验结果")
        if decision in {"release", "conditional"} and any(not row["passed"] for row in latest_tests.values()):
            raise ApiError(409, "最新检验结果仍有不合格项")
        if decision == "resample":
            if batch["state"] == "conditional": raise ApiError(409, "有条件放行后不能直接改为再取样")
            new_state = "awaiting_resample"
        elif decision == "reject":
            new_state = "rejected"
        elif any(d["severity"] == "critical" for d in open_deviations):
            raise ApiError(409, "未关闭的关键偏差阻止放行")
        elif decision == "release" and open_deviations:
            raise ApiError(409, "仍有未关闭偏差，不能正式放行")
        elif decision == "conditional":
            for deviation in open_deviations:
                if not deviation["exception_reason"] or not after_now(deviation["exception_until"]):
                    raise ApiError(409, f"偏差 {deviation['id']} 没有有效例外批准")
            if not exception_code.strip(): raise ApiError(400, "有条件放行必须提供例外编号")
            new_state = "conditional"
        else:
            new_state = "released"
        with self.conn:
            cur = self.conn.execute("""INSERT INTO decisions(batch_id,revision,decision,rationale,exception_code,decided_by,created_at)
                                     VALUES(?,?,?,?,?,?,?)""", (batch_id, batch["revision"], decision, rationale, exception_code or None, actor, now()))
            updated = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                        (new_state, now(), batch_id, expected_revision))
            if updated.rowcount != 1: raise ApiError(409, "并发放行冲突")
            if decision == "reject":
                self._propagate_recall(batch_id, "reject", cur.lastrowid, f"批次拒收：{rationale}", actor)
            self.store.audit(actor, "batch.decision", "batch", batch_id, {"decision": decision, "revision": batch["revision"], "state": new_state, "exception_code": exception_code})
        return {"decision": dict(self._row("decisions", cur.lastrowid)), "batch": self.batch_detail(batch_id)["batch"]}

    def _advance_batch(self, batch_id: int, expected_revision: int, next_state: str) -> None:
        batch = self._row("batches", batch_id)
        if batch["state"] in {"released", "rejected"}: raise ApiError(409, "终态批次不可修改")
        if int(expected_revision) != int(batch["revision"]): raise ApiError(409, "批次版本冲突")
        cur = self.conn.execute("UPDATE batches SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?",
                                (next_state, now(), batch_id, expected_revision))
        if cur.rowcount != 1: raise ApiError(409, "并发更新冲突")

    def batch_detail(self, batch_id: int) -> dict:
        batch = self._batch_dict(self._row("batches", batch_id))
        def rows(name: str) -> list[dict]: return [dict(row) for row in self.conn.execute(f"SELECT * FROM {name} WHERE batch_id=? ORDER BY id", (batch_id,))]
        upstream = [self._lineage_dict(row) for row in self.conn.execute(
            "SELECT * FROM batch_lineage WHERE downstream_id=? ORDER BY id", (batch_id,))]
        downstream = [self._lineage_dict(row) for row in self.conn.execute(
            "SELECT * FROM batch_lineage WHERE upstream_id=? ORDER BY id", (batch_id,))]
        return {"batch": batch, "deviations": rows("deviations"), "tests": rows("tests"), "rework": rows("rework"),
                "supplier_changes": rows("supplier_changes"), "stability": rows("stability"),
                "decisions": rows("decisions"),
                "upstream_lineage": upstream, "downstream_lineage": downstream,
                "available_quantity": self._available_quantity(batch_id),
                "active_blockers": self._active_blockers(batch_id),
                "recall_events": rows("recall_events"), "recall_reviews": rows("recall_reviews")}

    def _lineage_dict(self, row: sqlite3.Row) -> dict:
        up = self.conn.execute("SELECT batch_no,product,factory_id,state FROM batches WHERE id=?", (row["upstream_id"],)).fetchone()
        down = self.conn.execute("SELECT batch_no,product,factory_id,state FROM batches WHERE id=?", (row["downstream_id"],)).fetchone()
        return {"id": row["id"], "upstream_id": row["upstream_id"], "downstream_id": row["downstream_id"],
                "quantity": row["quantity"], "registered_by": row["registered_by"], "created_at": row["created_at"],
                "upstream_batch_no": up["batch_no"] if up else None, "upstream_state": up["state"] if up else None,
                "upstream_available_remaining": self._available_quantity(row["upstream_id"]),
                "downstream_batch_no": down["batch_no"] if down else None, "downstream_state": down["state"] if down else None}

    def _batch_dict(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "factory_id": row["factory_id"], "batch_no": row["batch_no"], "product": row["product"],
                "mfg_date": row["mfg_date"], "expiry_date": row["expiry_date"], "state": row["state"], "revision": row["revision"],
                "quantity": row["quantity"], "available_quantity": self._available_quantity(row["id"])}

    @staticmethod
    def _deviation_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "severity": row["severity"], "title": row["title"], "due_at": row["due_at"],
                "status": row["status"], "corrective_action": row["corrective_action"], "exception_reason": row["exception_reason"],
                "exception_until": row["exception_until"], "exception_approved_by": row["exception_approved_by"]}

    @staticmethod
    def _test_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "batch_id": row["batch_id"], "test_type": row["test_type"], "result": row["result"],
                "spec_min": row["spec_min"], "spec_max": row["spec_max"], "passed": bool(row["passed"]), "round": row["round"]}

    def state(self) -> dict:
        batches = [self._batch_dict(row) for row in self.conn.execute("SELECT * FROM batches ORDER BY id DESC")]
        blocker_map: dict[int, list[dict]] = {}
        for b in batches: blocker_map[b["id"]] = self._active_blockers(b["id"])
        return {"factories": [dict(row) for row in self.conn.execute("SELECT * FROM factories ORDER BY id")],
                "batches": batches,
                "lineage": [self._lineage_dict(row) for row in self.conn.execute("SELECT * FROM batch_lineage ORDER BY id")],
                "active_blockers": {str(k): v for k, v in blocker_map.items() if v},
                "recall_events": [dict(row) for row in self.conn.execute("SELECT * FROM recall_events ORDER BY id DESC")],
                "audits": [dict(row) for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 30")]}

    def seed(self) -> None:
        if not self.conn.execute("SELECT id FROM factories LIMIT 1").fetchone():
            self.register_factory("qa-demo", "qa", "F-DEMO", "演示工厂", "CN")
            fid = self.conn.execute("SELECT id FROM factories WHERE code='F-DEMO'").fetchone()[0]
            bulk = self.create_batch("operator-demo", "operator", fid, "BULK-001", "阿莫西林片", "2026-09-01", "2028-09-01", 1000)
            pkg1 = self.create_batch("operator-demo", "operator", fid, "PKG-101", "阿莫西林片", "2026-09-05", "2028-09-01", 0)
            pkg2 = self.create_batch("operator-demo", "operator", fid, "PKG-102", "阿莫西林片", "2026-09-05", "2028-09-01", 0)
            self.register_lineage("operator-demo", "operator", fid, bulk["id"], pkg1["id"], 400, bulk["revision"])
            self.register_lineage("operator-demo", "operator", fid, bulk["id"], pkg2["id"], 300,
                                  self.batch_detail(bulk["id"])["batch"]["revision"])


class Handler(BaseHTTPRequestHandler):
    service: BatchService

    def log_message(self, fmt: str, *args: object) -> None: sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))
    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        try: return json.loads(self.rfile.read(size)) if size else {}
        except json.JSONDecodeError as exc: raise ApiError(400, "JSON 请求体无效") from exc
    def _parts(self) -> list[str]: return [p for p in urlparse(self.path).path.strip("/").split("/") if p]

    def do_GET(self) -> None:
        try:
            p = self._parts()
            if p in (["health"], ["api", "health"]): out = {"status": "ok"}
            elif p == ["api", "state"]: out = self.service.state()
            elif len(p) == 3 and p[:2] == ["api", "batches"]: out = self.service.batch_detail(int(p[2]))
            elif not p:
                page = (Path(__file__).parent / "static" / "index.html").read_bytes(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except Exception as exc: self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            p, b = self._parts(), self._body(); actor, role = self.headers.get("X-Actor"), self.headers.get("X-Role")
            if p == ["api", "factories"]: out = self.service.register_factory(actor, role, b.get("code", ""), b.get("name", ""), b.get("country", ""))
            elif p == ["api", "batches"]: out = self.service.create_batch(actor, role, int(b.get("factory_id", 0)), b.get("batch_no", ""), b.get("product", ""), b.get("mfg_date", ""), b.get("expiry_date", ""), float(b.get("quantity", 0)))
            elif p == ["api", "lineage"]: out = self.service.register_lineage(actor, role, int(b.get("factory_id", 0)), int(b.get("upstream_id", 0)), int(b.get("downstream_id", 0)), float(b.get("quantity", 0)), int(b.get("upstream_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "recall-review": out = self.service.review_recall(actor, role, int(p[2]), b.get("action", ""), b.get("note", ""))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "deviations": out = self.service.add_deviation(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("severity", ""), b.get("title", ""), b.get("due_at"), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "close": out = self.service.close_deviation(actor, role, int(p[2]), b.get("corrective_action", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "deviations"] and p[3] == "exception": out = self.service.approve_exception(actor, role, int(p[2]), b.get("reason", ""), b.get("until", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "tests": out = self.service.record_test(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("test_type", ""), float(b.get("result", 0)), float(b.get("spec_min", 0)), float(b.get("spec_max", 0)), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "rework": out = self.service.plan_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "rework"] and p[3] == "complete": out = self.service.complete_rework(actor, role, int(b.get("factory_id", 0)), int(p[2]), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "supplier-changes": out = self.service.record_supplier_change(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("supplier", ""), b.get("change_type", ""), b.get("description", ""), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "stability": out = self.service.record_stability(actor, role, int(b.get("factory_id", 0)), int(p[2]), b.get("condition", ""), b.get("timepoint", ""), float(b.get("result", 0)), float(b.get("spec_limit", 0)), int(b.get("expected_revision", -1)))
            elif len(p) == 4 and p[:2] == ["api", "batches"] and p[3] == "decide": out = self.service.decide(actor, role, int(p[2]), b.get("decision", ""), b.get("rationale", ""), int(b.get("expected_revision", -1)), b.get("exception_code", ""))
            else: raise ApiError(404, "接口不存在")
            self._send(200, out)
        except ApiError as exc: self._send(exc.status, {"error": exc.message})
        except (ValueError, TypeError, sqlite3.IntegrityError) as exc: self._send(400, {"error": str(exc)})
        except Exception as exc: self._send(500, {"error": str(exc)})


def run(port: int, db_path: str, seed: bool) -> None:
    store = Store(db_path); service = BatchService(store)
    if seed: service.seed()
    Handler.service = service
    print(f"batch release listening on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=8214); parser.add_argument("--db", default=str(DB_PATH)); parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    if args.init: Store(args.db).close()
    if args.seed or not args.init: run(args.port, args.db, args.seed)


if __name__ == "__main__": main()
