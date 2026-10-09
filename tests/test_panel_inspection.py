import pytest

from test_platform import ui
from qianniu.browser import PlatformPause
from qianniu.data import Product
from qianniu.platform import Qianniu
from qianniu.store import Store


def test_panel_inspection_opens_but_never_creates_or_submits(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><div class="sku-panel"><button '
                        'onclick="document.querySelector(\'dialog\').showModal()">+ 创建规格</button></div>'
                        '<dialog>规格设置<input><button>确认创建</button><button onclick="this.parentElement.close()">取消</button></dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'specification': {'selector': {'role': 'button', 'name': '+ 创建规格'},
                                                 'empty_region': {'css': '.sku-panel'}, 'empty_text': '+ 创建规格',
                                                 'region': {'css': 'dialog'},
                                                 'close': {'scope': [{'css': 'dialog'}], 'role': 'button', 'name': '取消'}}}}, s)
    result = a.open_panel(p, 'specification')
    assert ui.page.locator('dialog').is_visible() and result['state'] == 'panel_opened'
    assert ui.page.locator('dialog input').input_value() == '' and s.intent(p.task_id) is None
    with pytest.raises(PlatformPause):
        a.open_panel(p, 'submit')
    a.close_panel(p, 'specification')
    assert not ui.page.locator('dialog').is_visible()


def test_panel_inspection_never_treats_nonempty_sku_region_as_empty(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><div class="sku-panel">JG6-6<button>+ 创建规格</button></div>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'specification': {'selector': {'role': 'button', 'name': '+ 创建规格'},
                                                 'empty_region': {'css': '.sku-panel'}, 'empty_text': '+ 创建规格'}}}, s)
    with pytest.raises(PlatformPause):
        a.open_panel(p, 'specification')


def test_panel_close_preserves_any_manually_staged_text(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><div class="sku-panel"><button '
                        'onclick="document.querySelector(\'dialog\').showModal()">+ 创建规格</button></div>'
                        '<dialog><input><button onclick="this.parentElement.close()">取消</button></dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'specification': {'selector': {'role': 'button', 'name': '+ 创建规格'},
                                                 'region': {'css': 'dialog'},
                                                 'close': {'scope': [{'css': 'dialog'}], 'role': 'button', 'name': '取消'}}}}, s)
    a.open_panel(p, 'specification')
    ui.page.locator('dialog input').fill('user edits')
    with pytest.raises(PlatformPause):
        a.close_panel(p, 'specification')
    assert ui.page.locator('dialog').is_visible()
