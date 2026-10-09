import copy

import pytest

from qianniu.platform import Qianniu, sku_completeness
from qianniu.browser import PlatformPause
from qianniu.browser import Browser
from qianniu.data import Product
from qianniu.store import Store
from PIL import Image
from io import BytesIO
import base64


def test_material_filename_is_not_content_proof(ui, tmp_path):
    local = tmp_path / "sku.png"
    Image.new("RGB", (12, 12), "red").save(local)
    buf = BytesIO()
    Image.new("RGB", (12, 12), "blue").save(buf, format="PNG")
    src = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    ui.page.set_content('<section aria-label="素材"><span class="name">sku.png</span>'
                        f'<img src="{src}"></section>')
    a = Qianniu(ui, {}, None)
    record = a.verify_material(ui.page.get_by_role("region", name="素材"), local, "JG/sku",
                               {"filename": ".name", "image": "img", "id_attribute": "data-id"})
    assert not record["ok"] and not record["content_ok"]


def test_material_needs_unique_platform_id_and_actual_pixels(ui, tmp_path):
    local = tmp_path / "sku.png"
    Image.new("RGB", (12, 12), "red").save(local)
    src = "data:image/png;base64," + base64.b64encode(local.read_bytes()).decode()
    ui.page.set_content('<section aria-label="素材" data-id="actual-material-123"><span class="name">sku.png</span>'
                        f'<img src="{src}"></section>')
    a = Qianniu(ui, {}, None)
    record = a.verify_material(ui.page.get_by_role("region", name="素材"), local, "JG/sku",
                               {"filename": ".name", "image": "img", "id_attribute": "data-id"})
    assert record["ok"] and record["platform_id"] == "actual-material-123"
    assert record["width"] == 12 and record["height"] == 12


def test_missing_warehouse_binding_is_explicit_pause_not_empty_success(ui, product_data):
    a = Qianniu(ui, {}, None)
    with pytest.raises(PlatformPause):
        a.warehouse_baseline(Product(product_data))


def test_platform_fractional_price_is_not_silently_rounded():
    with pytest.raises(PlatformPause):
        Qianniu._normalize("price", "100.001")


def test_observations_from_local_browser_cannot_be_claimed_live(ui, product_data):
    a = Qianniu(ui, {}, None)
    assert not a.live
    assert a.read_form(Product(product_data)).source == "internal_browser_test"


def test_selected_images_are_rechecked_from_current_page(ui, tmp_path, product_data):
    local = tmp_path / "main.png"
    Image.new("RGB", (12, 12), "red").save(local)
    product_data["media"]["root"] = str(tmp_path)
    p = Product(product_data)
    src = "data:image/png;base64," + base64.b64encode(local.read_bytes()).decode()
    ui.page.set_content(f'<section aria-label="主图"><div class="item" data-id="123">'
                        f'<span class="name">main.png</span><img src="{src}"></div></section>')
    a = Qianniu(ui, {"selected_media": {"main": {"container": {"role": "region", "name": "主图"},
                      "items": ".item", "filename": ".name", "image": "img", "id_attribute": "data-id"}}}, None)
    assert a.read_media(p, "main")["names"] == ["main.png"]
    ui.page.get_by_role("region", name="主图").locator(".name").evaluate("e => e.textContent = 'different.png'")
    with pytest.raises(PlatformPause):
        a.read_media(p, "main")


def test_warehouse_pagination_reads_all_and_count_mismatch_pauses(ui, product_data):
    ui.page.set_content('''<span>test shop</span><span role="tab" aria-selected="true">仓库中(2)</span>
      <span id="total">共2件商品</span><span id="page">1</span><button id="first" onclick="show(1)">首页</button>
      <button id="next" onclick="show(2)">下一页</button><table aria-label="仓库"><tbody id="rows"></tbody></table>
      <script>function show(n){document.querySelector('#page').textContent=n;
      document.querySelector('#next').disabled=n===2;
      document.querySelector('#rows').innerHTML=`<tr><td class="title">same title</td><td>ID:${n===1?'111111111111':'222222222222'}</td>
      <td class="created">2026-10-08 11:00</td><td><a href="/edit?itemId=${n===1?'111111111111':'222222222222'}">编辑商品</a></td></tr>`;}show(1)</script>''')
    a = Qianniu(ui, {"warehouse": {"verified": True, "selected_tab": {"css": "[role=tab][aria-selected=true]"},
                                  "first_page": {"css": "#first"}, "page_number": {"css": "#page"},
                                  "total": {"css": "#total"}, "total_pattern": r"共(\d+)件商品",
                                  "table": {"role": "table", "name": "仓库"}, "rows": "tbody tr",
                                  "title": ".title", "created": ".created", "next_page": {"css": "#next"}}}, None)
    result = a._scan_warehouse(Product(product_data))
    assert result["complete"] and result["ids"] == ["111111111111", "222222222222"]
    ui.page.locator("#total").evaluate("e => e.textContent='共3件商品'")
    with pytest.raises(PlatformPause):
        a._scan_warehouse(Product(product_data))


def test_submit_receipt_never_uses_category_or_source_item_id(ui):
    a = Qianniu(ui, {"submit": {"receipt_url_pattern": "/success", "item_id_parameter": "itemId"}}, None)
    assert a._receipt_id() is None
    ui.goto("http://127.0.0.1/success?itemId=632745637833")
    assert a._receipt_id() is None
    ui.goto("http://127.0.0.1/success?catId=50021001")
    assert a._receipt_id() is None


def test_batch_fills_only_configured_single_sku_and_reads_all_results(ui, product_data, tmp_path):
    ui.page.set_content('''<span>test shop</span><button onclick="document.querySelector('dialog').showModal()">更多批量</button>
       <dialog aria-label="批量填写"><label>价格<input></label><label>库存<input></label>
       <label>截面<input></label><label for="cable">线缆</label><select id="cable"><option>单芯多股芯</option></select>
       <button onclick="let d=this.parentElement; document.querySelectorAll('tbody input')[0].value=d.querySelectorAll('input')[0].value;
       document.querySelectorAll('tbody input')[1].value=d.querySelectorAll('input')[1].value;
       document.querySelectorAll('tbody input')[2].value=d.querySelectorAll('input')[2].value; d.close()">应用</button></dialog>
       <table aria-label="SKU"><thead><tr><th>规格</th><th>价格</th><th>库存</th><th>截面</th><th>线缆</th></tr></thead>
       <tbody><tr><td>JG6-6</td><td><input></td><td><input></td><td><input></td><td>单芯多股芯</td></tr></tbody></table>''')
    p, s = Product(product_data), Store(tmp_path / "state.sqlite")
    s.register(p)
    scoped = lambda label: {"scope": [{"role": "dialog", "name": "批量填写"}], "label": label}
    a = Qianniu(ui, {"sku": {"table": {"role": "table", "name": "SKU"}, "fully_rendered": True,
                             "columns": {"name": "规格", "price": "价格", "inventory": "库存", "area": "截面", "cable": "线缆"}},
                    "batch": {"open": {"role": "button", "name": "更多批量"}, "single_sku_only": True,
                              "fields": {"price": {"selector": scoped("价格")}, "inventory": {"selector": scoped("库存")},
                                         "area": {"selector": scoped("截面")}, "cable": {"selector": scoped("线缆")}},
                              "apply": {"scope": [{"role": "dialog", "name": "批量填写"}], "role": "button", "name": "应用"}}}, s)
    a.fill_batch(p, p.known_paths())
    row = a.read_all_skus()["rows"][0]
    assert row["price"] == "100.00" and row["inventory"] == 999 and row["area"] == "6"
    assert any(e["kind"] == "batch_readback" for e in s.report(p.task_id)["events"])
    s.close()


@pytest.fixture
def ui(tmp_path):
    with Browser({"profile_dir": str(tmp_path / "profile"), "channel": "chrome", "headless": True,
                  "allowed_hosts": ["127.0.0.1"]}, tmp_path) as b:
        b.page.route("http://127.0.0.1/**", lambda route: route.fulfill(body="<html><body></body></html>",
                                                                       content_type="text/html"))
        b.goto("http://127.0.0.1/publish?catId=50021001")
        yield b


def test_readback_is_actual_not_config_expected(ui, product_data):
    ui.page.set_content('<span>test shop</span><label>宝贝标题<input value="wrong"></label>'
                        '<span id="count">5/60</span>')
    a = Qianniu(ui, {"fields": {"fields.title": {"selector": {"label": "宝贝标题"}}},
                     "counts": {"title_count": {"css": "#count"}}}, None)
    result = a.read_form(Product(product_data))
    assert result.values["fields.title"] == "wrong"
    assert result.values["title_count"] == 5
    assert not result.complete and result.blockers


def test_known_independent_field_filled_and_existing_different_title_preserved(ui, product_data, tmp_path):
    ui.page.set_content('<span>test shop</span><label>宝贝标题<input></label><label>型号<input></label>')
    p = Product(product_data)
    s = Store(tmp_path / "state.sqlite")
    s.register(p)
    a = Qianniu(ui, {"fields": {"fields.title": {"selector": {"label": "宝贝标题"}},
                               "fields.model": {"selector": {"label": "型号"}}}}, s)
    a.fill_known(p, ["fields.model"])
    assert ui.page.get_by_label("型号").input_value() == "JG铜接线端子"
    ui.page.get_by_label("宝贝标题").fill("another product")
    with pytest.raises(PlatformPause):
        a.fill_known(p, ["fields.title"])
    assert ui.page.get_by_label("宝贝标题").input_value() == "another product"
    s.close()


def test_sku_reader_maps_headers_not_fixed_column_positions(ui):
    ui.page.set_content('''<table aria-label="SKU"><thead><tr><th>库存</th><th>规格</th><th>价格</th>
       <th>适用电线截面</th><th>适用线缆</th></tr></thead><tbody><tr><td><input value="999"></td>
       <td>JG6-6</td><td><input value="100"></td><td>6</td><td>单芯多股芯</td></tr></tbody></table>''')
    a = Qianniu(ui, {"sku": {"table": {"role": "table", "name": "SKU"}, "fully_rendered": True,
                             "columns": {"name": "规格", "price": "价格", "inventory": "库存",
                                         "area": "适用电线截面", "cable": "适用线缆"}}}, None)
    result = a.read_all_skus()
    assert result["declared_count"] == 1
    assert result["rows"] == [{"name": "JG6-6", "price": "100.00", "inventory": 999,
                               "area": "6", "cable": "单芯多股芯"}]


def test_unread_virtual_rows_do_not_pass_as_full_sku(ui):
    ui.page.set_content('<table aria-label="SKU"><thead><tr><th>规格</th></tr></thead>'
                        '<tbody><tr><td>JG6-6</td></tr></tbody></table><span id="count">共45个SKU</span>')
    a = Qianniu(ui, {"sku": {"table": {"role": "table", "name": "SKU"}, "columns": {"name": "规格"},
                             "count": {"css": "#count"}, "count_pattern": r"共(\d+)个SKU"}}, None)
    result = a.read_all_skus()
    assert result["declared_count"] == 45
    assert not sku_completeness([{"name": "JG6-6"}], result["rows"], result["declared_count"])["ok"]


def test_unselected_warehouse_label_does_not_prove_warehouse_status(ui):
    ui.page.set_content('<span role="tab" aria-selected="false">仓库中(2)</span>')
    a = Qianniu(ui, {"warehouse": {"selected_tab": {"role": "tab", "name": "仓库中(2)"}}}, None)
    assert not a._warehouse_selected()


def test_unverified_profile_cannot_click_even_with_token(ui, tmp_path, product_data):
    ui.page.set_content('<button onclick="this.textContent=\'clicked\'">提交宝贝信息</button>')
    p, s = Product(product_data), Store(tmp_path / "state.sqlite")
    s.register(p)
    s.observe(p, [{"path": "preflight", "ok": True, "actual": True}], "live_playwright", {})
    token = s.reserve_submit(p, {"ids": [], "complete": True})
    a = Qianniu(ui, {"submit": {"selector": {"role": "button", "name": "提交宝贝信息"}}}, s)
    with pytest.raises(PlatformPause):
        a.submit_once(token)
    assert ui.page.get_by_role("button").inner_text() == "提交宝贝信息"
    s.close()


def test_material_picker_uses_exact_folder_filename_and_checked_state(ui, tmp_path, product_data):
    local = tmp_path / "sku.png"
    Image.new("RGB", (12, 12), "red").save(local)
    src = "data:image/png;base64," + base64.b64encode(local.read_bytes()).decode()
    product_data["media"]["root"] = str(tmp_path)
    ui.page.set_content(f'''<button onclick="document.querySelector('dialog').showModal()">选择图片</button>
      <dialog aria-label="素材"><button role="treeitem">JG</button>
      <button role="treeitem" onclick="document.querySelector('#breadcrumb').textContent='JG/sku'">sku</button>
      <span id="breadcrumb">JG</span><section aria-label="文件">
      <div class="item" data-id="actual-123"><span class="filename">sku.png</span><img src="{src}"><input type="checkbox" checked></div>
      <div class="item" data-id="extra-456"><span class="filename">extra.png</span><input type="checkbox"></div></section>
      <button onclick="this.parentElement.close()">确认</button></dialog>''')
    p, s = Product(product_data), Store(tmp_path / "state.sqlite")
    s.register(p)
    a = Qianniu(ui, {"materials": {"sku": {
        "fully_rendered": True, "open": {"role": "button", "name": "选择图片"},
        "folder_item": {"scope": [{"role": "dialog", "name": "素材"}], "role": "treeitem"},
        "breadcrumb": {"css": "#breadcrumb"}, "container": {"role": "region", "name": "文件"},
        "items": ".item", "filename": ".filename", "image": "img", "id_attribute": "data-id",
        "confirm": {"scope": [{"role": "dialog", "name": "素材"}], "role": "button", "name": "确认"}}}}, s)
    proof = a.choose_materials(p, "sku", ["sku.png"])
    assert proof[0]["platform_id"] == "actual-123"
    assert ui.page.locator('[data-id="actual-123"] input').is_checked()
    assert not ui.page.locator('[data-id="extra-456"] input').is_checked()
    assert len(p.data["skus"]) == 1
    s.close()


def test_sku_all_combinations_columns_and_no_extra_rows():
    expected = [{"name": "JG6-6", "price": "100.00", "inventory": 999, "area": "6", "cable": "单芯多股芯"}]
    assert sku_completeness(expected, expected, 1)["ok"]
    assert not sku_completeness(expected, [], 1)["ok"]
    assert not sku_completeness(expected, expected * 2, 2)["ok"]
    missing = copy.deepcopy(expected)
    del missing[0]["area"]
    assert not sku_completeness(expected, missing, 1)["ok"]
    extra = copy.deepcopy(expected) + [{"name": "JG6-8"}]
    assert not sku_completeness(expected, extra, 2)["ok"]
    assert not sku_completeness(expected, expected, None)["ok"]


def test_same_spec_name_with_different_combination_is_not_same_sku():
    expected = [{"name": "JG6-6", "combination": ["JG6-6", "red"], "price": "100", "inventory": 999,
                 "area": "6", "cable": "单芯多股芯"}]
    actual = copy.deepcopy(expected)
    actual[0]["combination"] = ["JG6-6", "blue"]
    assert not sku_completeness(expected, actual, 1)["ok"]


def test_adapter_never_submits_without_persisted_token():
    class Browser:
        pass
    with pytest.raises(PlatformPause):
        Qianniu(Browser(), {}, None).submit_once(None)
