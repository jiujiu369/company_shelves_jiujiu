"""Read-only workbench screenshots; never starts Qianniu operations."""
from pathlib import Path
import sys

from PIL import Image, ImageStat
from playwright.sync_api import sync_playwright


root = Path(__file__).resolve().parent.parent
url = next((arg for arg in sys.argv[1:] if arg.startswith('http://127.0.0.1:')), 'http://127.0.0.1:8766')
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url)
    page.get_by_role('heading', name='准备检查').wait_for()
    page.wait_for_function('() => document.getElementById("product-image").naturalWidth > 0')
    assert page.locator('#submit').is_disabled()
    assert page.locator('svg.lucide').count() > 10
    if '--validate' in sys.argv:
        page.get_by_role('button', name='校验资料', exact=True).click()
        page.wait_for_function('() => document.getElementById("run-title").textContent.startsWith("当前未运行")')
        assert '资料预检通过' in page.locator('#run-detail').inner_text()
        assert page.locator('#submit').is_disabled()
    for name, width, height in [('desktop', 1440, 1000), ('mobile', 375, 900)]:
        page.set_viewport_size({'width': width, 'height': height})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        path = root / '运行记录' / f'ui-workbench-{name}.png'
        page.screenshot(path=str(path), full_page=True)
        with Image.open(path) as im:
            assert sum(ImageStat.Stat(im.convert('RGB')).stddev) > 12, 'blank screenshot'
        print(str(path))
    page.get_by_role('button', name='核对明细').click()
    page.locator('#checks:not([hidden])').wait_for()
    assert page.locator('#checks-table tbody tr').count() > 0
    assert not errors, errors
    page.get_by_role('button', name='商品资料', exact=True).click()
    for name, width, height in [('desktop', 1440, 1000), ('mobile', 375, 900)]:
        page.set_viewport_size({'width': width, 'height': height})
        assert not page.locator('#pipeline').is_visible()
        assert page.locator('#facts').bounding_box()['y'] < 700
        path = root / '运行记录' / f'ui-model-facts-{name}.png'
        page.screenshot(path=str(path))
        print(str(path))
    if '--editor' in sys.argv:
        page.get_by_role('button', name='商品资料', exact=True).click()
        page.get_by_role('button', name='编辑配置', exact=True).click()
        page.get_by_role('textbox', name='图片目录', exact=True).wait_for()
        for name, width, height in [('desktop', 1440, 1000), ('mobile', 375, 900)]:
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            path = root / '运行记录' / f'ui-config-editor-{name}.png'
            page.screenshot(path=str(path))
            print(str(path))
        page.get_by_role('button', name='取消', exact=True).click()
        assert not errors, errors
    browser.close()
