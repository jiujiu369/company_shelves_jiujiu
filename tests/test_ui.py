import importlib.util
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from qianniu.data import load_product
from qianniu.store import Store


def dashboard(tmp_path, product_data):
    assert importlib.util.find_spec('qianniu.ui') is not None, 'visual workbench is not implemented'
    from qianniu.ui import Dashboard
    (tmp_path / 'products').mkdir()
    (tmp_path / 'profiles').mkdir()
    (tmp_path / 'products' / 'test.json').write_text(json.dumps(product_data), encoding='utf-8')
    (tmp_path / 'config.json').write_text(json.dumps({'state': 'records/state.sqlite', 'profile': 'profiles/test.json'}), encoding='utf-8')
    (tmp_path / 'profiles' / 'test.json').write_text(json.dumps({'submit': {'verified': False}, 'warehouse': None}), encoding='utf-8')
    return Dashboard(tmp_path)


def test_preparation_shows_missing_sources_and_optional_pack(tmp_path, product_data):
    product_data['skus'][0]['price'] = {'value': None, 'reason': 'supplier has not quoted', 'source': 'supplier', 'confirmed': False}
    app = dashboard(tmp_path, product_data)
    view = app.snapshot('test.json')
    assert any(i['path'] == 'skus.0.price' and i['source'] == 'supplier' and i['blocking'] for i in view['issues'])
    assert any(i['path'] == 'skus.0.pack_quantity' and not i['blocking'] for i in view['issues'])
    assert not view['submit_ready']
    assert {p['id'] for p in view['preparations']} >= {'data', 'media', 'logistics', 'shop', 'materials', 'units', 'exclusive'}


def test_preparation_ack_does_not_create_platform_checks_or_submission(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    view = app.snapshot('test.json')
    app.acknowledge('test.json', view['version'], ['shop', 'materials', 'units', 'exclusive'])
    other = type(app)(tmp_path)
    assert other.snapshot('test.json')['preparation_ready']
    assert not other.snapshot('test.json')['submit_ready']
    s = Store(app.state_path)
    report = s.report(view['task_id'])
    assert report['checks'] == [] and report['attempts'] == []
    assert any(e['kind'] == 'ui_preparation_ack' for e in report['events'])
    s.close()


def test_product_change_invalidates_preparation_ack(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    view = app.snapshot('test.json')
    app.acknowledge('test.json', view['version'], list(app.manual_preparations))
    product_data['fields']['model']['value'] = 'changed model'
    (tmp_path / 'products' / 'test.json').write_text(json.dumps(product_data), encoding='utf-8')
    assert not app.snapshot('test.json')['preparation_ready']
    with pytest.raises(ValueError, match='version'):
        app.acknowledge('test.json', view['version'], list(app.manual_preparations))


@pytest.mark.parametrize('name', ['../config.json', '..\\config.json', 'D:/secrets.json'])
def test_product_path_is_whitelisted(tmp_path, product_data, name):
    app = dashboard(tmp_path, product_data)
    with pytest.raises(ValueError):
        app.snapshot(name)


def test_submit_and_arbitrary_command_are_rejected_without_starting_job(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    with pytest.raises(ValueError):
        app.start('test.json', 'submit')
    with pytest.raises(ValueError):
        app.start('test.json', 'powershell')
    with pytest.raises(ValueError):
        app.start('test.json', 'fill')
    assert app.get_job() is None


def test_real_validate_job_produces_persistent_result_and_no_platform_success(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    result = app.start('test.json', 'validate')
    deadline = time.time() + 20
    while app.get_job()['status'] == 'running' and time.time() < deadline:
        time.sleep(.05)
    job = app.get_job()
    assert job['id'] == result['id'] and job['status'] == 'finished'
    assert job['exit_code'] == 2  # Missing fixture image files, not a warehouse result.
    assert job['result']['state'] == 'pending_data'
    assert (app.jobs_dir / job['id'] / 'job.json').exists()
    assert not app.snapshot('test.json')['submit_ready']


def test_unknown_submission_blocks_fill_but_allows_preparation_read(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    p = load_product(tmp_path / 'products/test.json')
    s = Store(app.state_path)
    s.register(p)
    s.observe(p, [{'path': 'preflight', 'ok': True}], 'live_playwright', {})
    s.reserve_submit(p, {'complete': True, 'ids': []})
    s.close()
    assert app.snapshot('test.json')['submission']['state'] == 'intent_recorded'
    with pytest.raises(ValueError, match='submission'):
        app.start('test.json', 'fill')


def test_only_configured_media_can_be_served(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    with pytest.raises(ValueError):
        app.media_file('test.json', '../config.json')
    with pytest.raises(ValueError):
        app.evidence_file('test.json', '../../config.json')


def test_resume_without_intent_requires_preparation(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    with pytest.raises(ValueError, match='准备'):
        app.start('test.json', 'resume')


def test_task_id_cannot_escape_record_directory(tmp_path, product_data):
    product_data['task_id'] = '../../browser-profile'
    app = dashboard(tmp_path, product_data)
    with pytest.raises(ValueError, match='task'):
        app.snapshot('test.json')


def test_ui_respects_existing_cli_process_lock(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    from qianniu.runtime import ProcessLock
    with ProcessLock(tmp_path / '运行记录/run.lock'):
        with pytest.raises(RuntimeError, match='another program'):
            app.start('test.json', 'validate')
    assert app.get_job() is None


def test_manual_not_submitted_resume_still_requires_preparation(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    p = load_product(tmp_path / 'products/test.json')
    s = Store(app.state_path)
    s.register(p)
    s.observe(p, [{'path': 'preflight', 'ok': True}], 'live_playwright', {})
    s.reserve_submit(p, {'complete': True, 'ids': []})
    s.attempt_state(p.task_id, 'manual_not_submitted')
    s.close()
    with pytest.raises(ValueError, match='准备'):
        app.start('test.json', 'resume')


def test_new_revision_preparation_never_relabels_old_warehouse_success(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    p = load_product(tmp_path / 'products/test.json')
    s = Store(app.state_path)
    s.register(p)
    s.observe(p, [{'path': 'preflight', 'ok': True}], 'live_playwright', {})
    s.reserve_submit(p, {'complete': True, 'ids': []})
    s.attempt_state(p.task_id, 'confirmed_warehouse')
    s.state(p.task_id, 'confirmed_warehouse', item_id='888888888888')
    s.close()
    product_data['fields']['model']['value'] = 'new model'
    (tmp_path / 'products/test.json').write_text(json.dumps(product_data), encoding='utf-8')
    view = app.snapshot('test.json')
    app.acknowledge('test.json', view['version'], list(app.manual_preparations))
    assert app.snapshot('test.json').get('confirmed_current_version') is False


def test_start_rejects_displayed_old_product_version(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    view = app.snapshot('test.json')
    app.acknowledge('test.json', view['version'], list(app.manual_preparations))
    with pytest.raises(ValueError, match='version'):
        app.start('test.json', 'fill', version='old-version')
    assert app.get_job() is None


def test_changed_runtime_configuration_is_not_silently_used(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    config = json.loads((tmp_path / 'config.json').read_text(encoding='utf-8'))
    config['state'] = 'other.sqlite'
    (tmp_path / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    with pytest.raises(ValueError, match='configuration'):
        app.start('test.json', 'validate')
    assert not (tmp_path / 'other.sqlite').exists()


def test_restarted_workbench_keeps_conservative_pause_available(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    directory = app.jobs_dir / 'interrupted-job'
    directory.mkdir(parents=True)
    (directory / 'job.json').write_text(json.dumps({'id': 'interrupted-job', 'status': 'running', 'task_id': 'test-1', 'product': 'test.json', 'action': 'fill'}), encoding='utf-8')
    other = type(app)(tmp_path)
    assert other.get_job().get('pause_available') is True
    other.pause('test.json')
    assert json.loads((tmp_path / '运行记录/STOP').read_text(encoding='utf-8'))['task_id'] == 'test-1'


@pytest.mark.parametrize('change', ['product', 'configuration'])
def test_child_rejects_changes_between_ui_check_and_process_read(tmp_path, product_data, monkeypatch, change):
    app = dashboard(tmp_path, product_data)
    import qianniu.ui as ui
    original = ui.subprocess.Popen
    def changed_before_spawn(*args, **kwargs):
        if change == 'product':
            product_data['fields']['model']['value'] = 'changed immediately before spawn'
            (tmp_path / 'products/test.json').write_text(json.dumps(product_data), encoding='utf-8')
        else:
            config = json.loads((tmp_path / 'config.json').read_text(encoding='utf-8'))
            config['state'] = 'new-state.sqlite'
            (tmp_path / 'config.json').write_text(json.dumps(config), encoding='utf-8')
        return original(*args, **kwargs)
    monkeypatch.setattr(ui.subprocess, 'Popen', changed_before_spawn)
    app.start('test.json', 'validate')
    deadline = time.time() + 15
    while app.get_job()['status'] == 'running' and time.time() < deadline:
        time.sleep(.05)
    assert app.get_job()['exit_code'] == 3
    assert change in app.get_job()['result']['reason']
    assert not app.state_path.exists() and not (tmp_path / 'new-state.sqlite').exists()


def test_browser_preparation_navigation_layout_and_real_validation(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    from PIL import Image
    from qianniu.ui import make_server
    from playwright.sync_api import sync_playwright
    for name in ('main.png', 'detail.png', 'sku.png'):
        Image.new('RGB', (200, 200), '#18756a').save(tmp_path / 'products' / name)
    server = make_server(app, 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(url)
            page.get_by_text('0/4 人工确认', exact=True).wait_for()
            assert page.locator('#product-image').evaluate('e=>e.complete && e.naturalWidth===200')
            assert page.locator('#fill').is_disabled() and page.locator('#submit').is_disabled()
            for checkbox in page.locator('[data-preparation]').all():
                checkbox.check()
                page.get_by_text('准备确认已保存；不会放行提交。', exact=True).wait_for()
                page.wait_for_function('() => !document.querySelector("[data-preparation]").disabled')
            page.get_by_text('4/4 人工确认', exact=True).wait_for()
            assert page.locator('#fill').is_enabled() and page.locator('#submit').is_disabled()
            page.get_by_role('button', name='校验资料', exact=True).click()
            page.wait_for_function('() => document.getElementById("run-title").textContent.startsWith("当前未运行")')
            assert app.get_job()['exit_code'] == 0
            assert not app.snapshot('test.json')['submit_ready']
            page.get_by_role('button', name='商品资料', exact=True).click()
            assert page.locator('#facts').is_visible()
            page.get_by_role('button', name='工作台', exact=True).click()
            for width in (1440, 768, 375):
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert page.get_by_role('heading', name='准备检查').is_visible()
            assert not errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join()


def test_http_rejects_cross_origin_and_missing_token(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    from qianniu.ui import make_server
    server = make_server(app, 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(url + '/api/bootstrap') as response:
            boot = json.load(response)
        payload = json.dumps({'product': 'test.json', 'action': 'submit', 'version': app.snapshot('test.json')['version']}).encode()
        for headers in ({'Origin': 'https://other.example', 'X-UI-Token': boot['token']}, {'Origin': url}):
            with pytest.raises(HTTPError) as error:
                urlopen(Request(url + '/api/action', data=payload, headers=headers), timeout=5)
            assert error.value.code == 403
        with pytest.raises(HTTPError) as error:
            urlopen(Request(url + '/api/action', data=payload, headers={'Origin': url, 'X-UI-Token': boot['token']}), timeout=5)
        assert error.value.code == 409
    finally:
        server.shutdown()
        server.server_close()
        t.join()


def test_submit_dialog_keeps_displayed_version_after_polling_changes(tmp_path, product_data):
    app = dashboard(tmp_path, product_data)
    from qianniu.ui import make_server
    from playwright.sync_api import sync_playwright
    server = make_server(app, 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    fixture = app.snapshot('test.json')
    fixture.update(version='v1', submit_ready=True)
    requests = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page()
            # A local UI fixture exposes the dialog; no submit request reaches the CLI.
            page.route('**/api/state?*', lambda route: route.fulfill(body=json.dumps(fixture), content_type='application/json; charset=utf-8'))
            def reject_submission(route):
                requests.append(route.request.post_data_json)
                route.fulfill(status=409, body=json.dumps({'error': 'old confirmed version rejected'}), content_type='application/json; charset=utf-8')
            page.route('**/api/action', reject_submission)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.locator('#submit:enabled').wait_for()
            page.locator('#submit').click()
            fixture['version'] = 'v2'
            page.wait_for_function('() => document.getElementById("version").textContent.includes("v2")')
            page.locator('#confirm-submit').click()
            page.get_by_text('old confirmed version rejected', exact=True).wait_for()
            assert requests[0]['version'] == 'v1'
            assert app.get_job() is None
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        t.join()
