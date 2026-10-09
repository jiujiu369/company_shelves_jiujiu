import json
import threading

from playwright.sync_api import sync_playwright
from test_ui import dashboard


def test_mobile_navigation_shows_content_not_shared_workflow(tmp_path, product_data):
    from qianniu.ui import make_server
    app = dashboard(tmp_path, product_data)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 400, 'height': 850})
            writes = []
            page.on('request', lambda r: writes.append(r.url) if r.method == 'POST' else None)
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.locator('#version').filter(has_text='资料版本').wait_for()
            for label, panel in [('核对明细', 'checks'), ('执行记录', 'events'), ('商品资料', 'facts')]:
                page.get_by_role('button', name=label, exact=True).click()
                assert not page.locator('#pipeline').is_visible(), 'shared workflow still obscures content'
                assert page.locator('#' + panel).is_visible()
                assert page.locator('#' + panel).bounding_box()['y'] < 700
                assert page.url.endswith('#' + panel)
            page.reload()
            assert page.locator('#facts').is_visible()
            page.get_by_role('button', name='工作台', exact=True).click()
            assert page.locator('#pipeline').is_visible()
            assert not writes, 'opening or navigation must not start any task'
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_model_assistance_uses_existing_service_without_browser_or_fact_write(tmp_path, product_data, monkeypatch):
    from qianniu import __main__ as cli
    app = dashboard(tmp_path, product_data)
    calls = []
    class Model:
        def __init__(self, config): calls.append(config)
        def suggest(self, facts):
            return {'title': 'candidate', 'guide_title': 'candidate', 'sources': [facts[0]['source']],
                    'confirmed': False, 'submission_authorized': False}
    monkeypatch.setattr(cli, 'Ollama', Model)
    config = json.loads(app.config_path.read_text())
    config['ollama'] = {'url': 'http://192.168.1.9:11434', 'model': 'qwen3.5:4b-q4_K_M'}
    app.config_path.write_text(json.dumps(config), encoding='utf-8')
    before = (tmp_path / 'products/test.json').read_bytes()
    args = cli.parser().parse_args(['--config', str(app.config_path), 'model-suggest', str(tmp_path / 'products/test.json')])
    code, result = cli.invoke(args)
    assert code == 0 and result['state'] == 'model_advice_ready'
    assert result['submission_authorized'] is False
    assert calls[0]['model'] == 'qwen3.5:4b-q4_K_M'
    assert (tmp_path / 'products/test.json').read_bytes() == before
    assert app._records(product_data['task_id'])['submission'] is None


def test_three_utility_buttons_dispatch_and_show_feedback(tmp_path, product_data, monkeypatch):
    import time
    from qianniu.ui import make_server
    app = dashboard(tmp_path, product_data)
    actions = []
    def start(name, action, version=None, **kwargs):
        actions.append(action)
        app.job = {'action': action, 'product': name, 'task_id': product_data['task_id'],
            'status': 'finished', 'started': time.time(), 'ended': time.time(), 'exit_code': 0,
            'result': {'state': 'data_ready'}}
        return app.job
    monkeypatch.setattr(app, 'start', start)
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 400, 'height': 850})
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.locator('#version').filter(has_text='资料版本').wait_for()
            for label, command in [('校验资料','validate'),('打开专用浏览器','start-browser'),('检查登录','probe')]:
                with page.expect_response(lambda r: r.url.endswith('/api/action')):
                    page.get_by_role('button', name=label, exact=True).click()
                page.get_by_text('当前未运行 · 上次任务：', exact=False).wait_for()
                assert actions[-1] == command
                page.wait_for_function('()=>{const r=document.getElementById("run-bar").getBoundingClientRect();return r.top>=0 && r.top<innerHeight}')
                assert 0 <= page.locator('#run-bar').bounding_box()['y'] < 850
            assert actions == ['validate','start-browser','probe']
            page.get_by_role('button', name='编辑当前商品配置', exact=True).click()
            page.get_by_role('textbox', name='图片目录', exact=True).wait_for()
            page.locator('#media-mode').select_option('local_folder')
            page.locator('#folder-upload-confirmed').check()
            page.get_by_role('textbox', name='图片目录', exact=True).fill(str(tmp_path))
            assert not page.locator('#folder-upload-confirmed').is_checked()
            page.locator('#folder-upload-confirmed').check()
            with page.expect_response(lambda r: r.url.endswith('/api/save-configuration')):
                page.get_by_role('button', name='保存新版本', exact=True).click()
            saved = app.configuration('test.json')['data']['media']
            assert saved['input_mode'] == 'local_folder'
            assert saved['local_folder_authorization']['root'] == str(tmp_path)
            assert saved['local_folder_authorization']['confirmed'] is True
            assert actions == ['validate','start-browser','probe']
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
