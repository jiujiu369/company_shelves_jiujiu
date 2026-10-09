import copy
import sqlite3

import pytest

from qianniu.data import Product
from qianniu.store import Store, SubmissionAlreadyReserved
from qianniu.workflow import Workflow, Observation, Paused, compare


class TestPlatform:
    __test__ = False
    live = False

    def __init__(self, product):
        self.product = product
        self.values = product.expected()
        self.reads = 0
        self.clicks = 0
        self.lookups = 0
        self.fail_submit = False
        self.found = []
        self.baseline = []
        self.fill_paths = []

    def read_form(self, product):
        self.reads += 1
        return Observation(copy.deepcopy(self.values), complete=True, source="internal_test")

    def fill_known(self, product, paths):
        self.fill_paths += paths

    def warehouse_baseline(self, product):
        return {"ids": self.baseline, "complete": True, "source": "internal_test"}

    def submit_once(self, token):
        self.clicks += 1
        if self.fail_submit:
            raise TimeoutError("unknown click outcome")
        return {"item_id": "888888888888", "source": "internal_test"}

    def find_warehouse(self, product, intent):
        self.lookups += 1
        return self.found

    def read_edit(self, product, item_id):
        return Observation(copy.deepcopy(self.values), complete=True, source="internal_test",
                           item_id=item_id, warehouse=True)

    def capture(self, label):
        return {"label": label, "source": "internal_test"}


def setup_run(tmp_path, product_data):
    p = Product(product_data)
    store = Store(tmp_path / "state.sqlite")
    platform = TestPlatform(p)
    return p, store, platform, Workflow(store, platform)


def test_missing_data_blocks_submit_but_fills_known_fields(tmp_path, product_data):
    product_data["skus"][0]["price"]["value"] = None
    p, s, a, w = setup_run(tmp_path, product_data)
    result = w.run(p, submit=True)
    assert result["state"] == "pending_data"
    assert "fields.title" in a.fill_paths and "skus.0.price" not in a.fill_paths
    assert a.clicks == 0 and s.intent(p.task_id) is None


def test_resume_always_reads_platform_instead_of_trusting_old_checks(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    assert w.run(p)["state"] == "verified"
    a.values["skus.0.inventory"] = 1
    before = a.reads
    assert w.run(p)["state"] == "paused"
    assert a.reads > before and a.clicks == 0


def test_change_invalidates_affected_checks_preserves_unrelated(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p)
    changed = copy.deepcopy(product_data)
    changed["skus"][0]["inventory"]["value"] = 998
    s.register(Product(changed))
    checks = s.report(p.task_id)["checks"]
    assert not any(c["path"] in ("skus.0.inventory", "total_inventory", "preflight") for c in checks)
    assert any(c["path"] == "fields.title" for c in checks)


def test_click_intent_survives_crash_and_cannot_be_repeated(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.fail_submit = True
    assert w.run(p, submit=True)["state"] == "submission_unknown"
    assert a.clicks == 1
    s.close()
    s2 = Store(tmp_path / "state.sqlite")
    result = Workflow(s2, a).run(p, submit=True)
    assert result["state"] == "submission_unknown" and a.clicks == 1 and a.lookups >= 1


def test_intent_written_before_click(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    original = a.submit_once

    def check(token):
        assert s.intent(p.task_id)["token"] == token
        other = sqlite3.connect(s.path)
        assert other.execute("SELECT count(*) FROM intents").fetchone()[0] == 1
        other.close()
        return original(token)

    a.submit_once = check
    w.run(p, submit=True)
    assert a.clicks == 1


def test_warehouse_and_edit_required_for_success(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.found = [{"item_id": "888888888888", "warehouse": True, "shop": "test shop"}]
    result = w.run(p, submit=True)
    assert result["state"] == "tested_warehouse"
    assert result["item_id"] == "888888888888" and a.lookups == 1


def test_old_id_cannot_become_new_id(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.baseline = ["888888888888"]
    a.found = [{"item_id": "888888888888", "warehouse": True, "shop": "test shop"}]
    assert w.run(p, submit=True)["state"] == "submission_unknown"


def test_multiple_title_matches_need_user_review(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.found = [{"item_id": "111111111111", "warehouse": True, "shop": "test shop"},
               {"item_id": "222222222222", "warehouse": True, "shop": "test shop"}]
    assert w.run(p, submit=True)["state"] == "submission_unknown"


def test_config_change_after_intent_never_unlocks_submit(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p, submit=True)
    d = copy.deepcopy(product_data)
    d["skus"][0]["price"]["value"] = "101"
    assert w.run(Product(d), submit=True)["state"] == "submission_unknown"
    assert a.clicks == 1


def test_all_sku_rows_and_columns_must_be_read(product_data):
    p = Product(product_data)
    actual = p.expected()
    del actual["skus.0.area"]
    assert not compare(p, Observation(actual, complete=True))["ok"]
    assert not compare(p, Observation(p.expected(), complete=False))["ok"]


def test_unexpected_sku_blocks_submit(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.values["sku_names"].append("JG6-8")
    assert w.run(p, submit=True)["state"] == "paused" and a.clicks == 0


def test_concurrent_reservation_is_atomic(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p)
    other = Store(s.path)
    token = s.reserve_submit(p, {"ids": [], "complete": True})
    with pytest.raises(SubmissionAlreadyReserved):
        other.reserve_submit(p, {"ids": [], "complete": True})
    assert token and s.intent(p.task_id)["version"] == p.version


def test_edit_page_mismatch_keeps_result_unknown(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.found = [{"item_id": "888888888888", "warehouse": True, "shop": "test shop"}]
    original = a.read_edit

    def wrong(*args):
        observation = original(*args)
        observation.values["fields.title"] = "wrong"
        return observation

    a.read_edit = wrong
    assert w.run(p, submit=True)["state"] == "submission_unknown"


def test_crash_after_reservation_before_click_recovers_by_query_only(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    w.run(p)
    s.reserve_submit(p, {"ids": [], "complete": True})
    s.close()
    result = Workflow(Store(tmp_path / "state.sqlite"), a).run(p, submit=True)
    assert result["state"] == "submission_unknown" and a.clicks == 0 and a.lookups == 1


def test_live_adapter_cannot_trust_test_observation(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.live = True
    assert w.run(p, submit=True)["state"] == "paused" and a.clicks == 0


def test_unready_submission_adapter_does_not_reserve_intent(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    def unready(product):
        raise Paused("submission/warehouse bindings not accepted")
    a.prepare_submit = unready
    result = w.run(p, submit=True)
    assert result["state"] == "paused" and s.intent(p.task_id) is None and a.clicks == 0
