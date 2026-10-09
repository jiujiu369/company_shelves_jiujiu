from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .browser import PlatformPause
from .data import validate
from .media import manifest, ensure_version


Paused = PlatformPause


@dataclass
class Observation:
    values: dict
    complete: bool = False
    source: str = "unverified"
    evidence: dict = field(default_factory=dict)
    blockers: list = field(default_factory=list)
    observed: float = field(default_factory=time.time)
    item_id: str | None = None
    warehouse: bool = False


def compare(product, observation):
    checks = [{"path": k, "expected": expected, "actual": observation.values.get(k),
               "ok": k in observation.values and observation.values[k] == expected}
              for k, expected in product.expected().items()]
    checks.append({"path": "completeness", "expected": True, "actual": observation.complete,
                   "ok": observation.complete and not observation.blockers})
    ok = all(c["ok"] for c in checks) and not any(i.blocking for i in validate(product))
    checks.append({"path": "preflight", "expected": True, "actual": ok, "ok": ok})
    return {"ok": ok, "checks": checks, "blockers": observation.blockers}


class Workflow:
    def __init__(self, store, platform):
        self.store, self.platform = store, platform

    def _observe(self, product, observation):
        if self.platform.live and observation.source != "live_playwright":
            raise Paused("live workflow cannot trust test or historical observations")
        if time.time() - observation.observed > 60:
            raise Paused("observation stale")
        result = compare(product, observation)
        self.store.observe(product, result["checks"], observation.source, observation.evidence, observation.observed)
        return result

    def run(self, product, submit=False):
        self.store.register(product)
        task_id = product.task_id
        intent = self.store.intent(task_id)
        if intent and intent["state"] == "terminated":
            return self.store.state(task_id, "terminated", {"reason": "manually terminated; no automatic restart"})
        if intent and intent["state"] != "manual_not_submitted":
            return self.recover(product)
        try:
            ensure = getattr(self.platform, "ensure_form", None)
            if ensure and getattr(self.platform, "profile", {}).get("entry"):
                ensure(product)
            before = self.platform.read_form(product)
            if self.platform.live and before.source != "live_playwright":
                raise Paused("live observation required")
            if before.values.get("shop") != product.value("shop"):
                raise Paused("shop identity differs or cannot be read")
            if before.values.get("category") != product.value("category"):
                raise Paused("enter confirmed category form before filling; " + str(before.blockers))
            issues = [i.to_dict() for i in validate(product)]
            # File existence is checked for file-backed products, not in-memory unit test fixtures.
            if product.media_digest:
                current_media = manifest(product)
                issues += current_media["issues"]
                if current_media["files"] != product.media_digest:
                    raise Paused("image changed after product version was loaded")
            self.store.event(task_id, "validation", {"issues": issues, "version": product.version})
            self.store.state(task_id, "filling")
            self.platform.fill_known(product, product.known_paths())
            ensure_version(product)
            after = self.platform.read_form(product)
            result = self._observe(product, after)
            if any(i.get("blocking", True) for i in issues):
                return self.store.state(task_id, "pending_data", {"issues": issues})
            if not result["ok"]:
                return self.store.state(task_id, "paused", {"reason": "full readback differs or is incomplete",
                    "mismatches": [c for c in result["checks"] if not c["ok"]], "blockers": result["blockers"]})
            if not submit:
                return self.store.state(task_id, "verified", {"source": after.source})
            prepare = getattr(self.platform, "prepare_submit", None)
            if prepare:
                prepare(product)
            baseline = self.platform.warehouse_baseline(product)
            # A baseline search may navigate away; re-read the actual form immediately before reserving.
            fresh = self.platform.read_form(product)
            if not self._observe(product, fresh)["ok"]:
                raise Paused("form changed while collecting baseline")
            if self.platform.live and baseline.get("source") != "live_playwright":
                raise Paused("live complete warehouse baseline required")
            token = self.store.reserve_submit(product, baseline)
            self.store.attempt_state(task_id, "dispatch_started")
            try:
                receipt = self.platform.submit_once(token)
                self.store.attempt_state(task_id, "dispatch_returned", receipt)
            except BaseException as e:
                self.store.attempt_state(task_id, "submission_unknown")
                self.store.event(task_id, "submit_exception", {"error": str(e), "evidence": self.platform.capture("submit-unknown")})
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
            return self.recover(product)
        except (KeyboardInterrupt, SystemExit):
            state = "submission_unknown" if self.store.intent(task_id) else "paused"
            self.store.state(task_id, state, {"reason": "operator interrupted"})
            raise
        except Exception as e:
            state = "submission_unknown" if self.store.intent(task_id) else "paused"
            self.store.state(task_id, state, {"reason": str(e), "capture": "pending"})
            try:
                evidence = self.platform.capture("paused")
            except Exception as capture_error:
                evidence = {"capture_error": str(capture_error)}
            return self.store.state(task_id, state, {"reason": str(e), "evidence": evidence})

    def recover(self, product):
        intent = self.store.intent(product.task_id)
        if not intent:
            return self.run(product)
        if intent["version"] != product.version:
            return self.store.state(product.task_id, "submission_unknown", {"reason": "revision changed after intent; reconcile original revision"})
        try:
            candidates = self.platform.find_warehouse(product, intent)
            baseline = set(intent["baseline"]["ids"])
            candidates = [c for c in candidates if str(c.get("item_id", "")) not in baseline
                          and c.get("warehouse") is True and c.get("shop") == product.value("shop")
                          and re.fullmatch(r"\d{10,20}", str(c.get("item_id", "")))]
            receipt_id = (intent.get("receipt") or {}).get("item_id")
            manual = [r for r in self.store.report(product.task_id)["resolutions"]
                      if r["attempt"] == intent["attempt"] and r["conclusion"] == "candidate"]
            attributed_ids = {str(receipt_id)} if receipt_id else set()
            attributed_ids.update(str(r["item_id"]) for r in manual if r["authorization"])
            if len(attributed_ids) > 1:
                raise Paused("conflicting receipt/manual attribution requires user review")
            if attributed_ids:
                candidates = [c for c in candidates if str(c["item_id"]) in attributed_ids]
            if len(candidates) != 1:
                raise Paused("no unique attributed new warehouse candidate; absence is not proof of non-submission")
            candidate = candidates[0]
            attributed = str(receipt_id) == str(candidate["item_id"]) or any(
                r["item_id"] == str(candidate["item_id"]) and r["authorization"] for r in manual)
            if not attributed:
                raise Paused("new content match has no task attribution; user confirmation required")
            if self.platform.live:
                created = candidate.get("created_at")
                if created is None or not intent["created"] - 120 <= created <= time.time() + 120:
                    raise Paused("candidate creation time unavailable or outside submission window")
                if candidate.get("source") != "live_playwright" or not candidate.get("query_complete"):
                    raise Paused("warehouse candidate query evidence incomplete")
            edit = self.platform.read_edit(product, str(candidate["item_id"]))
            if edit.item_id != str(candidate["item_id"]) or not edit.warehouse or not self._observe(product, edit)["ok"]:
                raise Paused("warehouse/edit-page identity, status or content not fully verified")
            state = "confirmed_warehouse" if self.platform.live else "tested_warehouse"
            self.store.attempt_state(product.task_id, state)
            return self.store.state(product.task_id, state, {"warehouse": candidate, "source": edit.source}, str(candidate["item_id"]))
        except Exception as e:
            self.store.attempt_state(product.task_id, "submission_unknown")
            self.store.state(product.task_id, "submission_unknown", {"reason": str(e), "capture": "pending"})
            try:
                evidence = self.platform.capture("recovery-stop")
            except Exception as capture_error:
                evidence = {"capture_error": str(capture_error)}
            return self.store.state(product.task_id, "submission_unknown", {"reason": str(e), "evidence": evidence})
