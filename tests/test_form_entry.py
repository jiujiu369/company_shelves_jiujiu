import pytest

from test_platform import ui
from qianniu.browser import PlatformPause
from qianniu.data import Product
from qianniu.platform import Qianniu


def entry_profile():
    return {'entry': {'category_path': '/sell/ai/category.htm',
                      'publish_url': 'http://127.0.0.1/publish?catId={category}'}}


def test_enter_form_preserves_existing_unsaved_form(ui, product_data):
    ui.page.set_content('<span>test shop</span><label>宝贝标题<input value="unsaved"></label>')
    a = Qianniu(ui, entry_profile(), None)
    result = a.ensure_form(Product(product_data))
    assert result['navigated'] is False
    assert ui.page.get_by_label('宝贝标题').input_value() == 'unsaved'


def test_enter_confirmed_category_from_entry_page(ui, product_data):
    ui.goto('http://127.0.0.1/sell/ai/category.htm')
    ui.page.set_content('<span>test shop</span>搜索发品')
    ui.page.route('http://127.0.0.1/publish?catId=50021001',
                  lambda route: route.fulfill(body='<span>test shop</span><label>宝贝标题<input></label>', content_type='text/html'))
    result = Qianniu(ui, entry_profile(), None).ensure_form(Product(product_data))
    assert result['navigated'] and result['category'] == '50021001'


def test_wrong_existing_form_is_not_replaced(ui, product_data):
    ui.goto('http://127.0.0.1/publish?catId=123')
    ui.page.set_content('<span>test shop</span><label>宝贝标题<input value="other product"></label>')
    with pytest.raises(PlatformPause):
        Qianniu(ui, entry_profile(), None).ensure_form(Product(product_data))
    assert ui.page.get_by_label('宝贝标题').input_value() == 'other product'
