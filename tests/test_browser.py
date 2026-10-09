import pytest
import socket
import subprocess
import time
from urllib.request import build_opener, ProxyHandler

from qianniu.browser import Browser, LocatorError, PlatformPause


@pytest.fixture
def browser(tmp_path):
    with Browser({"profile_dir": str(tmp_path / "profile"), "channel": "chrome",
                  "headless": True, "allowed_hosts": ["127.0.0.1"]}, tmp_path) as b:
        yield b


def test_duplicate_field_does_not_select_first(browser):
    browser.page.set_content('<label>库存<input></label><label>库存<input></label>')
    with pytest.raises(LocatorError):
        browser.resolve({"label": "库存"})


def test_scoped_label_changes_only_target_row(browser):
    browser.page.set_content('<section aria-label="JG6-6"><label>库存<input value="1"></label></section>'
                             '<section aria-label="JG6-8"><label>库存<input value="2"></label></section>')
    selector = {"scope": [{"role": "region", "name": "JG6-6"}], "label": "库存"}
    browser.act(selector, "fill", "999", guard=False)
    assert browser.resolve(selector).input_value() == "999"
    assert browser.page.get_by_role("region", name="JG6-8").get_by_label("库存").input_value() == "2"


def test_missing_control_pauses_instead_of_guessing(browser):
    browser.page.set_content('<button>立即上架</button>')
    with pytest.raises(LocatorError):
        browser.resolve({"role": "button", "name": "放入仓库"})


def test_hidden_duplicate_does_not_make_visible_field_ambiguous(browser):
    browser.page.set_content('<label style="display:none">价格<input></label><label>价格<input value="100"></label>')
    assert browser.resolve({"label": "价格"}).input_value() == "100"


def test_captcha_blocks_actions_even_when_button_exists(browser):
    browser.page.set_content('<p>请完成验证码</p><button>提交宝贝信息</button>')
    with pytest.raises(PlatformPause):
        browser.check_challenge()


def test_persistent_context_reopens_same_login_cookie(tmp_path):
    config = {"profile_dir": str(tmp_path / "profile"), "channel": "chrome", "headless": True}
    with Browser(config, tmp_path) as b:
        b.context.add_cookies([{"name": "session", "value": "fixture-login", "domain": "127.0.0.1",
                               "path": "/", "expires": 2000000000}])
    with Browser(config, tmp_path) as b:
        assert any(c["name"] == "session" and c["value"] == "fixture-login" for c in b.context.cookies())


def test_incorrect_host_blocked_before_browser_input(browser):
    browser.page.set_content('<button>提交宝贝信息</button>')
    with pytest.raises(PlatformPause):
        browser.guard()


def test_label_without_html_for_uses_unique_nearby_control(browser):
    browser.page.set_content('<section><div><span>型号</span><input value="JG"></div>'
                             '<div><span>品牌</span><input value="施科特"></div></section>')
    assert browser.resolve({"field": "型号"}).input_value() == "JG"


def test_nearby_ambiguous_controls_are_not_guessed(browser):
    browser.page.set_content('<div><span>库存</span><input><input></div>')
    with pytest.raises(LocatorError):
        browser.resolve({"field": "库存"})


def test_external_cdp_endpoint_rejected(tmp_path):
    with pytest.raises(PlatformPause):
        with Browser({"profile_dir": str(tmp_path), "cdp_url": "http://192.168.1.9:9222"}, tmp_path):
            pass


def test_rechecks_uniqueness_after_action_interval(browser):
    browser.page.set_content('''<label>库存<input value="1"></label><script>
      setTimeout(()=>{document.body.insertAdjacentHTML('beforeend','<label>库存<input value="2"></label>')},100)
      </script>''')
    browser.last_action = time.monotonic()
    with pytest.raises(LocatorError):
        browser.act({"label": "库存"}, "fill", "999", guard=False)
    assert [e.input_value() for e in browser.page.locator("input").all()] == ["1", "2"]


def test_stop_request_prevents_next_input(browser, tmp_path):
    browser.page.set_content('<label>库存<input value="1"></label>')
    stop = tmp_path / "STOP"
    stop.write_text("operator pause", encoding="utf-8")
    browser.config["stop_file"] = str(stop)
    with pytest.raises(PlatformPause):
        browser.act({"label": "库存"}, "fill", "999", guard=False)
    assert browser.page.get_by_label("库存").input_value() == "1"


def test_cdp_attach_preserves_browser_after_program_exit(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    owner = subprocess.Popen([r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--headless=new",
                              f"--user-data-dir={tmp_path / 'owner'}", f"--remote-debugging-port={port}",
                              "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with build_opener(ProxyHandler({})).open(endpoint + "/json/version", timeout=1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        with Browser({"cdp_url": endpoint}, tmp_path) as attached:
            attached.page.set_content('<title>Existing form</title><label>库存<input value="999"></label>')
        with Browser({"cdp_url": endpoint}, tmp_path) as again:
            assert again.resolve({"label": "库存"}).input_value() == "999"
            assert again.page.title() == "Existing form"
            again.remote.close()
    finally:
        if owner.poll() is None:
            owner.terminate()
        owner.wait(timeout=10)
