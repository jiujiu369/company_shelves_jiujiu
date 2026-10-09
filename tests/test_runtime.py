import subprocess
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qianniu.runtime import ProcessLock
from qianniu.store import Store
from qianniu.data import Product


def test_lock_rejects_second_writer_and_releases_after_exit(tmp_path):
    path = tmp_path / "run.lock"
    with ProcessLock(path):
        with pytest.raises(RuntimeError):
            with ProcessLock(path):
                pass
    with ProcessLock(path):
        pass


def test_click_claim_is_durable_and_only_once(tmp_path, product_data):
    p, s = Product(product_data), Store(tmp_path / "state.sqlite")
    s.register(p)
    s.observe(p, [{"path": "preflight", "ok": True}], "live_playwright", {})
    token = s.reserve_submit(p, {"ids": [], "complete": True})
    s.attempt_state(p.task_id, "dispatch_started")
    s.claim_click(token)
    s.close()
    other = Store(tmp_path / "state.sqlite")
    with pytest.raises(RuntimeError):
        other.claim_click(token)
    assert any(e["kind"] == "click_claimed" for e in other.report(p.task_id)["events"])
    other.close()


def test_cli_validate_produces_pending_list_without_browser(tmp_path):
    result = subprocess.run([sys.executable, "-m", "qianniu", "validate", "products/product.example.json",
                             "--output", str(tmp_path / "pending.json")], capture_output=True, text=True,
                            encoding="utf-8", timeout=15)
    assert result.returncode == 2 and (tmp_path / "pending.json").exists()
    assert '"pending_data"' in result.stdout and '"skus.0.price"' in result.stdout


def test_start_browser_uses_nested_browser_configuration(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"Browser":"test Chrome"}')

        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"browser": {"profile_dir": "profile", "cdp_url":
                          f"http://127.0.0.1:{server.server_port}"}}), encoding="utf-8")
    try:
        result = subprocess.run([sys.executable, "-m", "qianniu", "--config", str(config), "start-browser"],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert result.returncode == 0 and "browser_already_running" in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_menu_can_exit_without_launching_browser():
    result = subprocess.run([sys.executable, "-m", "qianniu", "menu"], input="0\n", capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0 and "商品资料" in result.stdout


def test_cli_rejects_expected_product_version_before_browser_or_registration(tmp_path):
    output = tmp_path / 'rejected.json'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'state': 'never-created.sqlite'}), encoding='utf-8')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'qianniu', '--config', str(config), '--expected-product-version',
        'old-version', 'validate', 'products/product.example.json', '--output', str(output)], capture_output=True,
        text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 3
    assert 'product version' in json.loads(output.read_text(encoding='utf-8'))['reason']


def test_cli_rejects_changed_runtime_configuration_before_browser(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'state': 'never-created.sqlite'}), encoding='utf-8')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'qianniu', '--config', str(config),
        '--expected-config-digest', 'old-config', 'start-browser'], capture_output=True,
        text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 3 and 'configuration' in result.stdout
    assert not (tmp_path / 'never-created.sqlite').exists()


def test_browser_guard_rejects_changed_expected_files_without_input(tmp_path):
    import hashlib
    from qianniu.browser import Browser, PlatformPause
    config = tmp_path / 'product.json'
    config.write_text('original', encoding='utf-8')
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    browser = Browser({'expected_files': {str(config): digest}}, tmp_path)
    browser.check_stop()
    config.write_text('changed price', encoding='utf-8')
    with pytest.raises(PlatformPause, match='configuration or product changed'):
        browser.check_stop()
