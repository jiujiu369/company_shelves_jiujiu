from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from .data import Product, canonical


class SubmissionAlreadyReserved(RuntimeError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY,version TEXT NOT NULL,
            state TEXT NOT NULL,updated REAL NOT NULL,item_id TEXT);
          CREATE TABLE IF NOT EXISTS versions(task_id TEXT,version TEXT,data TEXT,media TEXT,base TEXT,
            created REAL,PRIMARY KEY(task_id,version));
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,
            payload TEXT,created REAL);
          CREATE TABLE IF NOT EXISTS checks(task_id TEXT,path TEXT,version TEXT,expected TEXT,
            actual TEXT,ok INTEGER,source TEXT,observed REAL,evidence TEXT,PRIMARY KEY(task_id,path));
          CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY,task_id TEXT,version TEXT,
            checks TEXT,source TEXT,evidence TEXT,observed REAL);
          CREATE TABLE IF NOT EXISTS intents(task_id TEXT,attempt INTEGER,token TEXT UNIQUE,
            version TEXT,state TEXT,created REAL,baseline TEXT,receipt TEXT,proof TEXT,
            PRIMARY KEY(task_id,attempt));
          CREATE TABLE IF NOT EXISTS resolutions(id INTEGER PRIMARY KEY,task_id TEXT,attempt INTEGER,
            operator TEXT,conclusion TEXT,item_id TEXT,authorization TEXT,evidence TEXT,created REAL);
        """)

    def close(self):
        self.db.close()

    def event(self, task_id, kind, payload):
        with self.db:
            self.db.execute("INSERT INTO events(task_id,kind,payload,created) VALUES(?,?,?,?)",
                            (task_id, kind, canonical(payload), time.time()))

    def register(self, product):
        if not product.task_id:
            raise ValueError("task_id required")
        task_id, version = product.task_id, product.version
        previous = self.db.execute("SELECT version FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?)",
                            (task_id, version, canonical(product.data), canonical(product.media_digest),
                             str(product.base), time.time()))
            if previous and previous["version"] != version:
                old = self.product(task_id, previous["version"])
                changed = {p for p in old.facts().keys() | product.facts().keys()
                           if old.facts().get(p) != product.facts().get(p)}
                invalidate = set(changed) | {"preflight"}
                if any(p.endswith("inventory") for p in changed):
                    invalidate.add("total_inventory")
                if old.data.get("skus") != product.data.get("skus"):
                    invalidate.add("sku_names")
                if old.media_digest != product.media_digest or old.data.get("media") != product.data.get("media"):
                    invalidate |= {"main_images", "detail_images"}
                    invalidate |= {p for p in old.facts() if p.endswith(".image")}
                if old.data.get("title_facts") != product.data.get("title_facts"):
                    invalidate |= {"fields.title", "fields.guide_title", "title_count", "guide_title_count"}
                for p in invalidate:
                    self.db.execute("DELETE FROM checks WHERE task_id=? AND path=?", (task_id, p))
                self.db.execute("INSERT INTO events(task_id,kind,payload,created) VALUES(?,?,?,?)",
                                (task_id, "data_changed", canonical({"from": previous["version"],
                                 "to": version, "invalidated": sorted(invalidate)}), time.time()))
            if previous:
                self.db.execute("UPDATE tasks SET version=?,updated=? WHERE task_id=?", (version, time.time(), task_id))
            else:
                self.db.execute("INSERT INTO tasks VALUES(?,?,?,?,NULL)", (task_id, version, "selected", time.time()))

    def product(self, task_id, version):
        row = self.db.execute("SELECT * FROM versions WHERE task_id=? AND version=?", (task_id, version)).fetchone()
        if not row:
            raise ValueError("unknown product revision")
        return Product(json.loads(row["data"]), row["base"], json.loads(row["media"]))

    def state(self, task_id, state, payload=None, item_id=None):
        with self.db:
            self.db.execute("UPDATE tasks SET state=?,updated=?,item_id=COALESCE(?,item_id) WHERE task_id=?",
                            (state, time.time(), item_id, task_id))
        self.event(task_id, state, payload or {})
        return {"task_id": task_id, "state": state, "item_id": item_id, **(payload or {})}

    def observe(self, product, checks, source, evidence, observed=None):
        with self.db:
            self.db.execute("INSERT INTO observations(task_id,version,checks,source,evidence,observed) VALUES(?,?,?,?,?,?)",
                            (product.task_id, product.version, canonical(checks), source, canonical(evidence),
                             observed or time.time()))
            for c in checks:
                self.db.execute("INSERT OR REPLACE INTO checks VALUES(?,?,?,?,?,?,?,?,?)",
                                (product.task_id, c["path"], product.version, canonical(c.get("expected")),
                                 canonical(c.get("actual")), int(c["ok"]), source, observed or time.time(), canonical(evidence)))

    def intent(self, task_id):
        row = self.db.execute("SELECT * FROM intents WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        for k in ("baseline", "receipt", "proof"):
            result[k] = json.loads(result[k]) if result[k] else None
        return result

    def reserve_submit(self, product, baseline):
        from .media import ensure_version
        ensure_version(product)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            last = self.intent(product.task_id)
            if last and last["state"] != "manual_not_submitted":
                raise SubmissionAlreadyReserved("existing attempt must be reconciled; no automatic retry")
            check = self.db.execute("SELECT * FROM checks WHERE task_id=? AND path='preflight'",
                                    (product.task_id,)).fetchone()
            if not check or not check["ok"] or check["version"] != product.version or time.time() - check["observed"] > 60:
                raise ValueError("fresh full preflight required")
            if not baseline.get("complete"):
                raise ValueError("warehouse baseline incomplete")
            observation = self.db.execute("SELECT * FROM observations WHERE task_id=? AND version=? ORDER BY id DESC LIMIT 1",
                                          (product.task_id, product.version)).fetchone()
            if not observation:
                raise ValueError("immutable full observation required")
            proof = dict(observation)
            for key in ("checks", "evidence"):
                proof[key] = json.loads(proof[key])
            proof["authorization"] = [dict(r) for r in self.db.execute(
                "SELECT * FROM events WHERE task_id=? AND kind='submit_authorization' ORDER BY id DESC LIMIT 1", (product.task_id,))]
            token = uuid.uuid4().hex
            attempt = last["attempt"] + 1 if last else 1
            self.db.execute("INSERT INTO intents(task_id,attempt,token,version,state,created,baseline,receipt,proof)"
                            " VALUES(?,?,?,?,?,?,?,?,?)",
                            (product.task_id, attempt, token, product.version, "intent_recorded", time.time(),
                             canonical(baseline), None, canonical(proof)))
            self.db.execute("UPDATE tasks SET state='submission_unknown',updated=? WHERE task_id=?",
                            (time.time(), product.task_id))
            self.db.execute("INSERT INTO events(task_id,kind,payload,created) VALUES(?,?,?,?)",
                            (product.task_id, "intent_recorded", canonical({"attempt": attempt, "token": token,
                             "version": product.version}), time.time()))
            self.db.commit()
            return token
        except BaseException:
            self.db.rollback()
            raise

    def attempt_state(self, task_id, state, receipt=None):
        intent = self.intent(task_id)
        with self.db:
            self.db.execute("UPDATE intents SET state=?,receipt=COALESCE(?,receipt) WHERE task_id=? AND attempt=?",
                            (state, canonical(receipt) if receipt is not None else None, task_id, intent["attempt"]))
        self.event(task_id, state, {"attempt": intent["attempt"], "receipt": receipt})

    def claim_click(self, token):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM intents WHERE token=?", (token,)).fetchone()
            if not row or row["state"] != "dispatch_started":
                raise SubmissionAlreadyReserved("click already claimed or attempt not dispatchable")
            self.db.execute("UPDATE intents SET state='click_claimed' WHERE token=?", (token,))
            self.db.execute("INSERT INTO events(task_id,kind,payload,created) VALUES(?,?,?,?)",
                            (row["task_id"], "click_claimed", canonical({"attempt": row["attempt"]}), time.time()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def resolve_submission(self, task_id, operator, conclusion, evidence_files, authorization="", item_id=None):
        intent = self.intent(task_id)
        if not intent or intent["state"] in ("confirmed_warehouse", "tested_warehouse", "terminated"):
            raise ValueError("no unresolved attempt")
        if not operator.strip() or not evidence_files:
            raise ValueError("operator and evidence files required")
        evidence = []
        for path in evidence_files:
            p = Path(path).resolve()
            evidence.append({"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
        if conclusion not in ("continue_query", "candidate", "terminate", "not_submitted"):
            raise ValueError("unsupported resolution")
        if conclusion in ("candidate", "not_submitted") and not authorization.strip():
            raise ValueError("direct user instruction must be recorded")
        if conclusion == "candidate" and (not item_id or not str(item_id).isdigit()):
            raise ValueError("candidate platform item id required")
        state = {"terminate": "terminated", "not_submitted": "manual_not_submitted"}.get(conclusion, "submission_unknown")
        with self.db:
            self.db.execute("INSERT INTO resolutions(task_id,attempt,operator,conclusion,item_id,authorization,evidence,created)"
                            " VALUES(?,?,?,?,?,?,?,?)", (task_id, intent["attempt"], operator, conclusion, item_id,
                            authorization, canonical(evidence), time.time()))
            self.db.execute("UPDATE intents SET state=? WHERE task_id=? AND attempt=?", (state, task_id, intent["attempt"]))
            self.db.execute("UPDATE tasks SET state=?,updated=? WHERE task_id=?", (state, time.time(), task_id))
            self.db.execute("DELETE FROM checks WHERE task_id=? AND path='preflight'", (task_id,))
        self.event(task_id, "manual_resolution", {"conclusion": conclusion, "attempt": intent["attempt"], "evidence": evidence})

    def report(self, task_id):
        task = self.db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError("unknown task")
        result = dict(task)
        for key, table in (("checks", "checks"), ("events", "events"), ("attempts", "intents"),
                           ("resolutions", "resolutions"), ("observations", "observations")):
            result[key] = [dict(r) for r in self.db.execute(f"SELECT * FROM {table} WHERE task_id=?", (task_id,))]
        return result
