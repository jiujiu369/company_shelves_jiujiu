"""Isolated, read-only evidence collection. Never owns or closes the user's browser."""
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

from .browser import Browser, PlatformPause
from urllib.parse import urlsplit


def collect(request):
    endpoint = request['endpoint']
    if urlsplit(endpoint).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise PlatformPause('local evidence endpoint required')
    with sync_playwright() as p:
        remote = p.chromium.connect_over_cdp(endpoint, timeout=3000)
        pages = [page for context in remote.contexts for page in context.pages
                 if page.url == request['url'] and not page.is_closed()]
        if len(pages) != 1:
            raise PlatformPause('evidence target tab not unique')
        b = Browser({}, Path(request['json']).parent)
        b.page = pages[0]
        snapshot = b._snapshot_unbounded()
        path = Path(request['json'])
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        if request.get('png'):
            try:
                if any(frame.locator('input[type="password"]:visible').count() for frame in b.page.frames):
                    raise PlatformPause('login screenshot omitted to protect credentials')
                b.page.screenshot(path=request['png'], full_page=True, timeout=3000,
                                  mask=[frame.locator('input[type="password"],input[type="hidden"],'
                                                      'input[name*="token" i],input[name*="secret" i]')
                                        for frame in b.page.frames])
            except Exception as e:
                snapshot['screenshot_error'] = type(e).__name__
                path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    collect(json.loads(sys.stdin.read()))
