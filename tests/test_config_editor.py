import json

import pytest

from test_ui import dashboard
from qianniu.data import load_product
from qianniu.store import Store


def test_config_editor_returns_real_configuration(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'configuration'), 'configuration editor is missing'
    result = app.configuration('test.json')
    assert result['data']['skus'][0]['inventory']['value'] == 999
    assert result['version'] == app.snapshot('test.json')['version']


def test_save_changes_price_and_keeps_archive_and_invalidates_preflight(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'save_configuration'), 'configuration saving is missing'
    p = load_product(tmp_path / 'products/test.json')
    s = Store(app.state_path)
    s.register(p)
    s.observe(p, [{'path': 'preflight', 'ok': True}], 'live_playwright', {})
    s.close()
    product_data['skus'][0]['price']['value'] = '123.45'
    result = app.save_configuration('test.json', p.version, product_data)
    assert load_product(tmp_path / 'products/test.json').value('skus.0.price') == '123.45'
    assert (tmp_path / result['archive']).exists()
    assert not any(c['path'] == 'preflight' for c in app.snapshot('test.json')['checks'])
    assert not app.snapshot('test.json')['submit_ready']


def test_editor_rejects_stale_version_and_task_identity_change(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'save_configuration'), 'configuration saving is missing'
    with pytest.raises(ValueError, match='version'):
        app.save_configuration('test.json', 'stale', product_data)
    old = app.snapshot('test.json')['version']
    product_data['task_id'] = 'changed-task'
    with pytest.raises(ValueError, match='task'):
        app.save_configuration('test.json', old, product_data)


def test_incomplete_data_can_be_saved_without_inventing_defaults(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'save_configuration'), 'configuration saving is missing'
    version = app.snapshot('test.json')['version']
    product_data['skus'][0]['price'] = {'value': None, 'source': 'supplier', 'confirmed': False, 'reason': 'waiting quote'}
    result = app.save_configuration('test.json', version, product_data)
    assert result['state'] == 'pending_data'
    assert any(i['path'] == 'skus.0.price' for i in result['issues'])


def test_import_rebases_relative_material_root_without_overwriting_existing(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'import_configuration'), 'local product selection is missing'
    source = tmp_path / 'external' / 'product.json'
    source.parent.mkdir()
    product_data['task_id'] = 'new-product-task'
    source.write_text(json.dumps(product_data), encoding='utf-8')
    result = app.import_configuration(str(source))
    imported = load_product(tmp_path / 'products' / result['product'])
    assert imported.data['media']['root'] == str(source.parent.resolve())
    assert imported.value('skus.0.price') == '100.00'
    assert source.read_text(encoding='utf-8') == json.dumps(product_data)
    with pytest.raises(ValueError, match='task'):
        app.import_configuration(str(source))


def test_configuration_cannot_change_after_unknown_submission(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'save_configuration'), 'configuration saving is missing'
    p = load_product(tmp_path / 'products/test.json')
    s = Store(app.state_path)
    s.register(p)
    s.observe(p, [{'path': 'preflight', 'ok': True}], 'live_playwright', {})
    s.reserve_submit(p, {'complete': True, 'ids': []})
    s.close()
    with pytest.raises(ValueError, match='submission'):
        app.save_configuration('test.json', p.version, product_data)


def test_native_selection_is_a_separate_strict_interface(tmp_path, product_data, monkeypatch):
    app = dashboard(tmp_path, product_data)
    assert hasattr(app, 'select_local'), 'native selection interface is missing'
    with pytest.raises(ValueError):
        app.select_local('powershell')
    import qianniu.ui as ui
    captured = []
    def run(argv, **kwargs):
        captured.append(argv)
        return type('Result', (), {'returncode': 0, 'stdout': json.dumps({'path': str(tmp_path)})})()
    monkeypatch.setattr(ui.subprocess, 'run', run)
    assert app.select_local('folder')['path'] == str(tmp_path)
    assert captured[0][-2:] == ['qianniu.picker', 'folder']


def test_invalid_image_structure_does_not_overwrite_configuration(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    before = (tmp_path / 'products/test.json').read_bytes()
    version = app.configuration('test.json')['version']
    product_data['skus'][0]['image']['value'] = ['wrong']
    with pytest.raises(ValueError):
        app.save_configuration('test.json', version, product_data)
    assert (tmp_path / 'products/test.json').read_bytes() == before


def test_invalid_import_does_not_create_configuration(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    product_data['task_id'] = 'invalid-import'
    product_data['skus'][0]['image']['value'] = 123
    source = tmp_path / 'broken.json'
    source.write_text(json.dumps(product_data), encoding='utf-8')
    with pytest.raises(ValueError):
        app.import_configuration(str(source))
    assert not (tmp_path / 'products/invalid-import.json').exists()


def test_browser_can_edit_save_and_reload_without_touching_platform(tmp_path, product_data):
    import threading
    from playwright.sync_api import sync_playwright
    from qianniu.ui import make_server
    app = dashboard(tmp_path, product_data)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='商品资料', exact=True).click()
            page.get_by_role('button', name='编辑配置', exact=True).click()
            group = page.get_by_role('group', name='SKU 1 · 价格', exact=True)
            group.get_by_role('textbox', name='值', exact=True).fill('123.45')
            group.get_by_role('textbox', name='依据来源', exact=True).fill('new supplier quote')
            page.get_by_role('button', name='保存新版本', exact=True).click()
            page.locator('#config-dialog').wait_for(state='hidden')
            assert app.configuration('test.json')['data']['skus'][0]['price']['value'] == '123.45'
            assert app.configuration('test.json')['data']['skus'][0]['area']['value'] == '6'
            page.reload()
            page.get_by_role('button', name='商品资料', exact=True).click()
            page.get_by_role('button', name='编辑配置', exact=True).click()
            assert group.get_by_role('textbox', name='值', exact=True).input_value() == '123.45'
            assert not app.snapshot('test.json')['submit_ready']
            assert app.get_job() is None and app.snapshot('test.json')['submission'] is None
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
