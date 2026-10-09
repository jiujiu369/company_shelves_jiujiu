from __future__ import annotations

import json
import hashlib
import random
import re
import time
import uuid
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import build_opener, ProxyHandler

from playwright.sync_api import sync_playwright


class PlatformPause(RuntimeError):
    pass


class LocatorError(PlatformPause):
    pass


def safe_url(url):
    p = urlsplit(url)
    query = [(k, "[redacted]" if re.search("token|ticket|password|session|auth", k, re.I) else v)
             for k, v in parse_qsl(p.query, keep_blank_values=True)]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))


def active_port_endpoint(path, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
            if (len(lines) >= 2 and lines[0].isdigit() and 0 < int(lines[0]) < 65536
                    and re.fullmatch(r'/devtools/browser/[A-Za-z0-9-]+', lines[1])):
                return 'ws://127.0.0.1:' + lines[0] + lines[1]
        except (OSError, UnicodeError):
            pass
        time.sleep(min(.1, max(0, deadline - time.monotonic())))
    raise PlatformPause('browser startup endpoint not ready within bounded wait')


class Browser:
    def __init__(self, config, evidence_dir):
        self.config = config
        self.evidence_dir = Path(evidence_dir)
        self.last_action = 0.0
        self.page = None

    @property
    def source(self):
        host = urlsplit(self.page.url).hostname or ""
        return "live_playwright" if any(host == h or host.endswith("." + h) for h in ("taobao.com", "tmall.com")) else "internal_browser_test"

    def __enter__(self):
        self.check_stop()
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        endpoint = self.config.get("cdp_url")
        if endpoint and urlsplit(endpoint).hostname not in ("127.0.0.1", "localhost", "::1"):
            raise PlatformPause("CDP endpoint must be local")
        self.p = sync_playwright().start()
        try:
            self.attached = bool(endpoint)
            if endpoint:
                if urlsplit(endpoint).scheme in ("http", "https"):
                    # Chrome 154 rejects the trailing slash used by older Playwright discovery.
                    with build_opener(ProxyHandler({})).open(endpoint.rstrip("/") + "/json/version", timeout=5) as response:
                        endpoint = json.load(response).get("webSocketDebuggerUrl", "")
                    if urlsplit(endpoint).scheme not in ("ws", "wss") or urlsplit(endpoint).hostname not in ("127.0.0.1", "localhost", "::1"):
                        raise PlatformPause("invalid local debugger websocket")
                self.remote = self.p.chromium.connect_over_cdp(endpoint, timeout=5000)
                if len(self.remote.contexts) != 1:
                    raise PlatformPause("browser context not unique")
                self.context = self.remote.contexts[0]
            else:
                self.context = self.p.chromium.launch_persistent_context(
                    user_data_dir=str(Path(self.config["profile_dir"]).resolve()),
                    channel=self.config.get("channel", "chrome"),
                    headless=self.config.get("headless", False),
                    viewport={"width": 1440, "height": 960}, accept_downloads=True,
                    args=["--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1"])
                port_file = Path(self.config["profile_dir"]) / "DevToolsActivePort"
                endpoint = active_port_endpoint(port_file)
            self.capture_endpoint = endpoint
            pages = [p for p in self.context.pages if not p.is_closed()]
            pattern = self.config.get("page_url_pattern")
            matched = [p for p in pages if pattern and re.search(pattern, p.url)]
            if not matched:
                entry_pattern = self.config.get('entry_url_pattern')
                matched = [p for p in pages if entry_pattern and re.search(entry_pattern, p.url)]
            if len(matched) == 1:
                self.page = matched[0]
            elif len(matched) > 1 or (self.attached and len(pages) > 1):
                raise PlatformPause("target tab not unique; configure page_url_pattern")
            else:
                self.page = pages[0] if len(pages) == 1 else self.context.new_page()
            self.page.set_default_timeout(self.config.get("timeout_ms", 30000))
            return self
        except BaseException:
            if not self.attached and getattr(self, 'context', None):
                self.context.close()
            self.p.stop()
            raise

    def __exit__(self, *_):
        try:
            if not self.attached:
                self.context.close()
        finally:
            self.p.stop()

    def goto(self, url):
        self.check_stop()
        self._host(url)
        self.pace()
        self.check_stop()
        self.page.goto(url, wait_until="domcontentloaded")

    def _host(self, url):
        host = urlsplit(url).hostname or ""
        allowed = self.config.get("allowed_hosts", ["taobao.com", "tmall.com"])
        if not any(host == h or host.endswith("." + h) for h in allowed):
            raise PlatformPause("unexpected page host: " + host)

    def check_challenge(self):
        for frame in self.page.frames:
            try:
                text = frame.locator("body").inner_text(timeout=2000)
            except Exception:
                continue
            if re.search(r"请完成验证码|拖动滑块|滑动验证|安全验证|访问被限制|请完成以下验证", text):
                raise PlatformPause("verification challenge requires user handling")

    def guard(self):
        self.check_stop()
        self._host(self.page.url)
        self.check_challenge()
        host = urlsplit(self.page.url).hostname or ""
        if host.startswith("login"):
            raise PlatformPause("login requires user handling")
        for frame in self.page.frames:
            if frame.locator('input[type="password"]:visible').count():
                raise PlatformPause("login form visible; user must sign in")

    def check_stop(self):
        path = self.config.get("stop_file")
        if path and Path(path).exists():
            raise PlatformPause("operator pause requested; no further inputs")
        for name, expected in self.config.get('expected_files', {}).items():
            try:
                actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
            except OSError:
                actual = None
            if actual != expected:
                raise PlatformPause('runtime configuration or product changed; refresh and reconfirm before inputs')

    def pace(self):
        interval = random.uniform(0.3, 1.2)
        remaining = self.last_action + interval - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.last_action = time.monotonic()

    def _make(self, root, selector):
        if "field" in selector:
            label = selector["field"]
            linked = root.get_by_label(label, exact=True)
            visible_linked = [l for l in linked.all() if l.is_visible()]
            if len(visible_linked) == 1:
                return visible_linked[0]
            if len(visible_linked) > 1:
                raise LocatorError("field label is not unique: " + label)
            labels = [l for l in root.get_by_text(re.compile(r"^\s*\*?\s*" + re.escape(label) + r"\s*$")).all()
                      if l.is_visible()]
            if len(labels) != 1:
                raise LocatorError("visible field text not unique: " + label)
            container = labels[0]
            for _ in range(6):
                container = container.locator("xpath=..")
                controls = container.locator(selector.get("control_css", 'input:visible,textarea:visible,select:visible'))
                count = controls.count()
                if count == 1:
                    return controls
                if count > 1:
                    raise LocatorError("field region has multiple controls: " + label)
                combos = container.locator('[role="combobox"]:visible')
                if combos.count() == 1:
                    return combos
            raise LocatorError("field has no unique nearby control: " + label)
        if "label" in selector:
            return root.get_by_label(selector["label"], exact=True)
        if "role" in selector:
            return root.get_by_role(selector["role"], name=selector.get("name"), exact=True)
        if "placeholder" in selector:
            return root.get_by_placeholder(selector["placeholder"], exact=True)
        if "text" in selector:
            return root.get_by_text(selector["text"], exact=True)
        if "css" in selector:
            return root.locator(selector["css"])
        raise LocatorError("selector needs role, label, placeholder, text or observed CSS")

    def resolve(self, selector, visible=True, missing_ok=False):
        roots = [self.page.main_frame]
        if selector.get("frame_url"):
            roots = [f for f in self.page.frames if re.search(selector["frame_url"], f.url)]
        matches = []
        for root in roots:
            for scope in selector.get("scope", []):
                root = self._make(root, scope)
            loc = self._make(root, selector)
            for item in loc.all():
                if not visible or item.is_visible():
                    matches.append(item)
        if not matches and missing_ok:
            return None
        if len(matches) != 1:
            raise LocatorError(f"expected one control, found {len(matches)}: {selector}")
        return matches[0]

    def act(self, selector, action, value=None, *, guard=True, before_action=None):
        if guard:
            self.guard()
        self.check_stop()
        self.pace()
        if guard:
            self.guard()
        target = self.resolve(selector, visible=action != 'files')
        if action == 'files' and not target.evaluate("e=>e.tagName==='INPUT' && e.type==='file'"):
            raise PlatformPause('unique file input required')
        if not target.is_enabled():
            raise PlatformPause("control disabled: " + str(selector))
        if before_action:
            before_action()
        if action == "fill":
            target.fill(str(value))
            target.blur()
            if target.input_value() != str(value):
                raise PlatformPause("field readback differs after fill")
        elif action == "click":
            target.click()
        elif action == "hover":
            target.hover()
        elif action == "check":
            target.check()
            if not target.is_checked():
                raise PlatformPause("selection did not persist")
        elif action == "select":
            target.select_option(label=str(value))
        elif action == 'files':
            target.set_input_files(value)
        else:
            raise ValueError("unsupported action: " + action)

    def wait(self, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.guard()
            result = predicate()
            if result:
                return result
            self.page.wait_for_timeout(300)
        raise PlatformPause("observable completion timed out; read current state before retry")

    def choose_files(self, opener, paths, *, before_action=None, before_transfer=None):
        self.guard()
        self.pace()
        target = self.resolve(opener)
        if not target.is_enabled() or not paths or any(not Path(p).is_file() for p in paths):
            raise PlatformPause('enabled upload control and existing selected files required')
        with self.page.expect_file_chooser(timeout=self.config.get('timeout_ms', 30000)) as pending:
            self.guard()
            self.check_stop()
            if before_action:
                before_action()
            target.click()
        chooser = pending.value
        if len(paths) > 1 and not chooser.is_multiple():
            raise PlatformPause('upload control cannot accept the selected image set')
        self.guard()
        self.check_stop()
        self.pace()
        if before_transfer:
            before_transfer()
        chooser.set_files(paths)

    def drag_before(self, source, destination):
        self.guard()
        self.pace()
        self.guard()
        start, end = self.resolve(source), self.resolve(destination)
        if not start.get_attribute('draggable') == 'true':
            raise PlatformPause('source is not an observed draggable module')
        start.scroll_into_view_if_needed()
        end.scroll_into_view_if_needed()
        a, b = start.bounding_box(), end.bounding_box()
        if not a or not b:
            raise PlatformPause('drag geometry unavailable')
        position = {'x': a['width'] * .15, 'y': a['height'] * .5}
        if source.get('drag_handle'):
            handle = start.locator(source['drag_handle'])
            if handle.count() != 1 or not handle.is_visible():
                raise PlatformPause('module drag handle not unique')
            h = handle.bounding_box()
            if not h:
                raise PlatformPause('module drag handle geometry unavailable')
            position = {'x': h['x'] + h['width'] / 2 - a['x'], 'y': h['y'] + h['height'] / 2 - a['y']}
        landing = {'x': b['width'] * .15, 'y': b['height'] * .5}
        if destination.get('drag_handle'):
            handle = end.locator(destination['drag_handle'])
            if handle.count() != 1 or not handle.is_visible() or not (h := handle.bounding_box()):
                raise PlatformPause('destination module handle not unique/visible')
            landing = {'x': h['x'] + h['width'] / 2 - b['x'], 'y': h['y'] + h['height'] / 2 - b['y']}
        trace = self.page.evaluate_handle('''() => {
          const rows=[], types=['dragstart','dragenter','dragover','drop','dragend'];
          const handler=e=>{if(rows.length<80)rows.push({type:e.type,tag:e.target.tagName,
            class:e.target.className,x:e.clientX,y:e.clientY});};
          types.forEach(t=>document.addEventListener(t,handler,true));
          return {rows,stop:()=>types.forEach(t=>document.removeEventListener(t,handler,true))};
        }''')
        held = False
        try:
            self.check_stop()
            self.page.mouse.move(a['x'] + position['x'], a['y'] + position['y'])
            self.page.mouse.down()
            held = True
            self.page.mouse.move(a['x'] + position['x'] + min(12, a['width'] / 3), a['y'] + position['y'], steps=4)
            self.page.mouse.move(b['x'] + landing['x'], b['y'] + landing['y'], steps=12)
            trace.evaluate('''async t=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
              t.regions=[...document.querySelectorAll('[draggable=true]')].slice(0,50)
                .map(e=>({tag:e.tagName,class:e.className,parentClass:e.parentElement.className}));}''')
            self.check_stop()
            self.page.mouse.up()
            held = False
        finally:
            if held:
                self.page.keyboard.press('Escape')
                self.page.mouse.up()
            rows = trace.evaluate('t=>{t.stop();return {events:t.rows,regions:t.regions};}')
            trace.dispose()
            path = self.evidence_dir / ('drag-events-' + uuid.uuid4().hex[:10] + '.json')
            path.write_text(json.dumps({'source': self.source, **rows}, ensure_ascii=False, indent=2), encoding='utf-8')

    def _snapshot_unbounded(self):
        result = {"url": safe_url(self.page.url), "title": self.page.title(), "frames": [],
                  "source": self.source, "time": time.time()}
        for frame in self.page.frames:
            try:
                result["frames"].append({"url": safe_url(frame.url), "dom": frame.evaluate("""() => ({
                  text: document.body?.innerText || '',
                  controls: [...document.querySelectorAll('input,textarea,select,button,[role],a,img')]
                    .map(e => ({tag:e.tagName,role:e.getAttribute('role'),id:e.id,class:e.className,
                      attributes: e.tagName==='IMG'?Object.fromEntries([...e.attributes]
                        .filter(a=>! /token|session|auth|secret|password/i.test(a.name))
                        .map(a=>[a.name,a.value])):{},
                      ancestors: (()=>{let p=e.parentElement,a=[];for(let i=0;p&&i<6;i++,p=p.parentElement){
                        if(p.tagName==='BODY'||p.tagName==='HTML')break;
                        a.push({tag:p.tagName,id:p.id,class:p.className,role:p.getAttribute('role'),
                                text:(p.innerText||'').slice(0,300)});}return a;})(),
                      name:e.getAttribute('name'),label:e.getAttribute('aria-label'),
                      placeholder:e.getAttribute('placeholder'),text:e.innerText || e.alt || '',
                      value:e.type==='password'||e.type==='hidden'||/token|session|auth|secret|password/i.test(e.name||e.id)?'[redacted]':e.value,
                      src:e.tagName==='IMG'?e.currentSrc:null,href:e.tagName==='A'?e.href:null,
                      checked:e.checked,disabled:e.disabled,
                      visible:!!(e.getBoundingClientRect().width&&e.getBoundingClientRect().height)}))
                })""")})
            except Exception as e:
                result["frames"].append({"url": safe_url(frame.url), "error": str(e)})
        for frame in result["frames"]:
            for control in frame.get("dom", {}).get("controls", []):
                for key in ("href", "src"):
                    if control.get(key):
                        control[key] = "[embedded image]" if control[key].startswith("data:") else safe_url(control[key])
                for key, value in control.get("attributes", {}).items():
                    if value.startswith(("http:", "https:", "//")):
                        control["attributes"][key] = safe_url(value)
                    elif value.startswith("data:"):
                        control["attributes"][key] = "[embedded image]"
        return result

    def snapshot(self):
        record = self.capture("snapshot", screenshot=False)
        return json.loads(Path(record["json"]).read_text(encoding="utf-8"))

    def capture(self, label, screenshot=True):
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", label) + "-" + uuid.uuid4().hex[:10]
        # The separate read-only worker can be killed even if an iframe renderer never answers.
        snapshot = {"url": safe_url(self.page.url), "source": self.source, "time": time.time(), "frames": []}
        path = self.evidence_dir / (name + ".json")
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        png = self.evidence_dir / (name + ".png")
        try:
            request = {"endpoint": self.capture_endpoint, "url": self.page.url, "json": str(path),
                       "png": str(png) if screenshot else None}
            subprocess.run([sys.executable, "-m", "qianniu.capture_worker"], input=json.dumps(request),
                           text=True, encoding="utf-8", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=8, check=True, cwd=str(Path(__file__).resolve().parent.parent),
                           creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            snapshot["capture_error"] = type(e).__name__ + ": bounded read-only capture failed"
            path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            if png.exists():
                png.unlink()
        return {"json": str(path), "screenshot": str(png) if png.exists() else None,
                "source": self.source, "url": snapshot["url"]}
