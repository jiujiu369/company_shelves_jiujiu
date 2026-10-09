import copy

import pytest
from PIL import Image

from qianniu.browser import PlatformPause
from qianniu.data import Product
from qianniu.media import manifest
from test_platform import ui


def local_product(tmp_path, data):
    data = copy.deepcopy(data)
    data['media'].update(root=str(tmp_path), input_mode='local_folder',
                         local_folder_authorization={'root': str(tmp_path), 'confirmed': True, 'source': 'operator'})
    for name in ('main.png', 'detail.png', 'sku.png', 'extra.png'):
        Image.new('RGB', (40, 40), 'red').save(tmp_path / name)
    p = Product(data)
    return Product(data, media_digest=manifest(p)['files'])


def test_plan_keeps_roles_order_and_excludes_extra(tmp_path, product_data):
    from qianniu.local_images import upload_plan
    p = local_product(tmp_path, product_data)
    plan = upload_plan(p)
    assert plan['ready']
    assert [r['file'] for r in plan['groups']['main']] == ['main.png']
    assert plan['groups']['sku'][0]['sku'] == 'JG6-6'
    assert 'extra.png' in plan['extras']
    assert all(r['file'] != 'extra.png' for rows in plan['groups'].values() for r in rows)
    assert not plan['platform_saved']


def test_changed_root_revokes_upload_authorization(tmp_path, product_data):
    from qianniu.local_images import upload_plan
    p = local_product(tmp_path, product_data)
    p.data['media']['local_folder_authorization']['root'] = str(tmp_path / 'old')
    plan = upload_plan(p)
    assert not plan['ready'] and any('authorization' in i['reason'] for i in plan['issues'])


def test_missing_detail_does_not_block_independent_main(tmp_path, product_data):
    from qianniu.local_images import upload_plan
    p = local_product(tmp_path, product_data)
    (tmp_path / 'detail.png').unlink()
    p = Product(p.data, media_digest=manifest(p)['files'])
    plan = upload_plan(p)
    assert not plan['ready']
    assert plan['group_ready']['main'] and not plan['group_ready']['detail']


def test_main_alias_duplicates_are_blocked(tmp_path, product_data):
    from qianniu.local_images import upload_plan
    p = local_product(tmp_path, product_data)
    p.data['media']['main'].append('./main.png')
    p = Product(p.data, media_digest=manifest(p)['files'])
    assert not upload_plan(p)['group_ready']['main']


def test_changed_content_rejected_before_dispatch(tmp_path, product_data):
    from qianniu.local_images import upload_plan
    p = local_product(tmp_path, product_data)
    Image.new('RGB', (40, 40), 'blue').save(tmp_path / 'main.png')
    with pytest.raises(PlatformPause, match='image changed'):
        upload_plan(p)


def test_local_entry_uses_upload_not_direct_navigation(ui, tmp_path, product_data, monkeypatch):
    from qianniu.platform import Qianniu
    p = local_product(tmp_path, product_data)
    ui.goto('http://127.0.0.1/sell/ai/category.htm')
    ui.page.set_content('<span>test shop</span>以图发品')
    a = Qianniu(ui, {'entry': {'category_path': '/sell/ai/category.htm',
                             'publish_url': 'http://127.0.0.1/publish?catId={category}'}}, None)
    called = []
    monkeypatch.setattr(a, 'start_local_entry', lambda product: called.append(product.version) or {'navigated': False})
    a.ensure_form(p)
    assert called == [p.version]
    assert ui.page.url.endswith('/sell/ai/category.htm')


def test_file_chooser_upload_requires_guard_and_only_selected_files(ui, tmp_path):
    Image.new('RGB', (40, 40), 'red').save(tmp_path / 'main.png')
    ui.page.set_content('<button onclick="document.querySelector(\'input\').click()">从本地上传</button>'
                        '<input type="file" multiple hidden>')
    checks = []
    ui.choose_files({'role': 'button', 'name': '从本地上传'}, [str(tmp_path / 'main.png')],
                    before_action=lambda: checks.append(True))
    assert checks == [True]
    assert ui.page.locator('input').evaluate('e=>Array.from(e.files,f=>f.name)') == ['main.png']


def test_entry_unknown_upload_is_not_repeated(ui, tmp_path, product_data, monkeypatch):
    from qianniu.platform import Qianniu
    from qianniu.store import Store
    p = local_product(tmp_path, product_data)
    ui.goto('http://127.0.0.1/sell/ai/category.htm')
    ui.page.set_content('<span>test shop</span><button>从本地上传</button>')
    s = Store(tmp_path / 'state.sqlite')
    s.register(p)
    profile = {'entry': {'category_path': '/sell/ai/category.htm', 'local_upload': {
        'verified': True, 'empty': {'text': '从本地上传'}, 'previews': {'css': 'img'},
        'open': {'role': 'button', 'name': '从本地上传'}, 'timeout': .2}}}
    a = Qianniu(ui, profile, s)
    calls = []
    def dispatch(*args, **kwargs):
        kwargs['before_action']()
        calls.append(True)
    monkeypatch.setattr(ui, 'choose_files', dispatch)
    with pytest.raises(PlatformPause):
        a.start_local_entry(p)
    with pytest.raises(PlatformPause, match='unknown|already dispatched'):
        a.start_local_entry(p)
    assert calls == [True] and not s.intent(p.task_id)
    s.close()


def test_unverified_entry_binding_cannot_upload(ui, tmp_path, product_data, monkeypatch):
    from qianniu.platform import Qianniu
    from qianniu.store import Store
    p = local_product(tmp_path, product_data)
    s = Store(tmp_path / 'state.sqlite')
    s.register(p)
    ui.goto('http://127.0.0.1/sell/ai/category.htm')
    ui.page.set_content('<span>test shop</span>从本地上传')
    a = Qianniu(ui, {'entry': {'category_path': '/sell/ai/category.htm',
                             'local_upload': {'verified': False}}}, s)
    monkeypatch.setattr(ui, 'choose_files', lambda *args, **kwargs: pytest.fail('must not upload'))
    with pytest.raises(PlatformPause, match='not live verified'):
        a.start_local_entry(p)
    s.close()


def test_local_config_missing_confirmation_is_visible_pending_data(tmp_path, product_data):
    from qianniu.data import validate
    p = local_product(tmp_path, product_data)
    p.data['media']['local_folder_authorization']['confirmed'] = False
    assert any(i.path == 'media.local_folder_authorization' and i.blocking for i in validate(p))
