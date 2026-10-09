import copy
import json
from pathlib import Path

import pytest

from qianniu.data import Product, validate, title_units


def fact(value, source="user", confirmed=True):
    return {"value": value, "source": source, "confirmed": confirmed}


@pytest.fixture
def product_data():
    return {
        "task_id": "test-1", "mode": "test", "shop": fact("test shop"),
        "category": fact("50021001"),
        "fields": {"title": fact("a" * 60), "guide_title": fact("b" * 30),
                   "brand": fact("realbrand"), "installation": fact("压接"),
                   "conductor": fact("铜镀锡"), "model": fact("JG铜接线端子")},
        "title_facts": [fact("a" * 60), fact("b" * 30)],
        "skus": [{"name": fact("JG6-6"), "price": fact("100.00"),
                  "inventory": fact(999), "sales_unit": fact("包"),
                  "inventory_unit": fact("包"), "area": fact("6"),
                  "cable": fact("单芯多股芯"), "pack_quantity": fact(None),
                  "image": fact("sku.png")}],
        "logistics": {"province": fact("浙江省"), "city": fact("温州市"),
                      "dispatch": fact("48小时以内"), "template": fact("包邮"),
                      "listing": fact("放入仓库")},
        "media": {"root": ".", "main": ["main.png"], "detail": ["detail.png"],
                  "folders": {"main": "JG/主图", "detail": "JG/详情图", "sku": "JG/sku"},
                  "split_authorized": True},
    }


def errors(data):
    return [i.path for i in validate(Product(data)) if i.blocking]


def test_known_complete_config_has_no_data_blockers(product_data):
    assert not errors(product_data)


def test_missing_price_has_source_and_reason_and_does_not_block_title(product_data):
    product_data["skus"][0]["price"] = {"value": None, "source": "price table",
                                           "confirmed": False, "reason": "not supplied"}
    p = Product(product_data)
    issues = validate(p)
    issue = next(i for i in issues if i.path == "skus.0.price")
    assert issue.blocking and issue.source == "price table" and issue.reason == "not supplied"
    assert "fields.title" in p.known_paths()
    assert "skus.0.price" not in p.known_paths()


def test_no_default_price_or_inventory(product_data):
    del product_data["skus"][0]["price"]
    del product_data["skus"][0]["inventory"]
    assert {"skus.0.price", "skus.0.inventory"} <= set(errors(product_data))


def test_duplicate_sku_is_conflict_not_overwritten(product_data):
    product_data["skus"].append(copy.deepcopy(product_data["skus"][0]))
    assert "skus" in errors(product_data)


@pytest.mark.parametrize("value", [-1, 1.5, True, "999.5"])
def test_inventory_requires_nonnegative_integer(product_data, value):
    product_data["skus"][0]["inventory"] = fact(value)
    assert "skus.0.inventory" in errors(product_data)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1.001"])
def test_price_is_finite_exact_cents(product_data, value):
    product_data["skus"][0]["price"] = fact(value)
    assert "skus.0.price" in errors(product_data)


def test_pack_quantity_is_not_inventory(product_data):
    product_data["skus"][0]["pack_quantity"] = fact(20)
    assert Product(product_data).total_inventory == 999


def test_unit_conflict_blocks_related_sku(product_data):
    product_data["skus"][0]["inventory_unit"] = fact("只")
    assert "skus.0.inventory_unit" in errors(product_data)


def test_unconfirmed_required_field_blocks(product_data):
    product_data["fields"]["conductor"] = fact("铜", "model extraction", False)
    assert "fields.conductor" in errors(product_data)


def test_brand_not_allowed_in_title(product_data):
    product_data["fields"]["title"] = fact("realbrand" + "a" * 51)
    assert "fields.title" in errors(product_data)


def test_immediate_listing_never_valid(product_data):
    product_data["logistics"]["listing"] = fact("立即上架")
    assert "logistics.listing" in errors(product_data)


def test_title_count_preserves_ascii_and_chinese_rules():
    assert title_units("JG紫铜6-6") == 9


def test_config_version_changes_with_any_value(product_data):
    a = Product(product_data).version
    other = copy.deepcopy(product_data)
    other["skus"][0]["inventory"] = fact(998)
    assert Product(other).version != a


def test_unproven_title_text_blocked(product_data):
    product_data["title_facts"] = []
    assert "title_facts" in errors(product_data)


def test_config_cannot_lower_global_title_policy(product_data):
    product_data["title_policy"] = {"title_units": 40, "guide_units": 20, "forbid_brand": False}
    assert "title_policy" in errors(product_data)
