import copy
import time

import pytest

from test_workflow import setup_run
from qianniu.data import Product
from qianniu.store import Store


def test_new_content_match_without_receipt_not_attributed(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.submit_once = lambda token: {"source": "internal_test"}
    a.found = [{"item_id": "888888888888", "warehouse": True, "shop": "test shop"}]
    assert w.run(p, submit=True)["state"] == "submission_unknown"


def test_manual_confirmation_does_not_skip_edit_readback(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.submit_once = lambda token: {}
    a.found = [{"item_id": "888888888888", "warehouse": True, "shop": "test shop"}]
    w.run(p, submit=True)
    evidence = tmp_path / "operator.txt"
    evidence.write_text("human checked platform record", encoding="utf-8")
    s.resolve_submission(p.task_id, "operator", "candidate", [evidence],
                         "I confirm this candidate belongs to this task", "888888888888")
    a.values["skus.0.inventory"] = 998
    assert w.recover(p)["state"] == "submission_unknown"


def test_manual_not_submitted_requires_new_authorization_and_evidence(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p, submit=True)
    evidence = tmp_path / "operator.txt"
    evidence.write_text("human investigation", encoding="utf-8")
    with pytest.raises(ValueError):
        s.resolve_submission(p.task_id, "operator", "not_submitted", [evidence])
    with pytest.raises(ValueError):
        s.resolve_submission(p.task_id, "operator", "not_submitted", [], "new attempt authorized")
    assert s.intent(p.task_id)["attempt"] == 1


def test_explicit_new_attempt_preserves_old_intent(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p, submit=True)
    evidence = tmp_path / "operator.txt"
    evidence.write_text("confirmed not submitted beyond an empty list", encoding="utf-8")
    s.resolve_submission(p.task_id, "operator", "not_submitted", [evidence], "user explicitly authorizes new attempt")
    result = w.run(p, submit=True)
    report = s.report(p.task_id)
    assert len(report["attempts"]) == 2 and a.clicks == 2
    assert report["attempts"][0]["state"] == "manual_not_submitted"
    assert report["resolutions"][0]["authorization"]


def test_terminate_is_durable_and_never_restarts(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p, submit=True)
    evidence = tmp_path / "operator.txt"
    evidence.write_text("terminate unresolved task", encoding="utf-8")
    s.resolve_submission(p.task_id, "operator", "terminate", [evidence])
    assert w.run(p, submit=True)["state"] == "terminated" and a.clicks == 1


def test_media_change_invalidates_image_checks(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p)
    changed = Product(product_data, media_digest={"sku.png": {"sha256": "changed"}})
    s.register(changed)
    assert not any(c["path"] in ("main_images", "detail_images", "skus.0.image", "preflight")
                   for c in s.report(p.task_id)["checks"])


def test_fresh_preflight_needed_even_after_human_unlock(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p, submit=True)
    evidence = tmp_path / "operator.txt"
    evidence.write_text("confirmed not submitted", encoding="utf-8")
    s.resolve_submission(p.task_id, "operator", "not_submitted", [evidence], "new attempt authorized")
    with pytest.raises(ValueError, match="preflight"):
        s.reserve_submit(p, {"complete": True, "ids": []})
