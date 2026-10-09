import copy
import json
import subprocess
import time

import pytest
from PIL import Image

from qianniu.browser import PlatformPause
from qianniu.data import Product
from qianniu.media import manifest
from qianniu.platform import Qianniu
from qianniu.workflow import Observation, compare
from test_platform import ui
from test_workflow import setup_run


def test_unknown_pack_quantity_requires_actual_empty(product_data):
    product_data['skus'][0]['pack_quantity']['value'] = None
    product_data['skus'][0]['pack_quantity']['confirmed'] = False
    p = Product(product_data)
    actual = p.expected()
    actual['skus.0.pack_quantity'] = 20
    assert not compare(p, Observation(actual, complete=True))['ok']


def test_conflict_stops_other_fields_immediately(ui, product_data):
    ui.page.set_content('<span>test shop</span><label>型号<input value="other"></label>'
                        '<label>品牌<input></label>')
    a = Qianniu(ui, {'fields': {'fields.model': {'selector': {'label': '型号'}},
                               'fields.brand': {'selector': {'label': '品牌'}}}}, None)
    with pytest.raises(PlatformPause):
        a.fill_known(Product(product_data), ['fields.model', 'fields.brand'])
    assert ui.page.get_by_label('品牌').input_value() == ''


def test_receipt_can_attribute_one_of_two_candidates(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.found = [{'item_id': n, 'warehouse': True, 'shop': 'test shop'}
               for n in ('888888888888', '222222222222')]
    assert w.run(p, submit=True)['state'] == 'tested_warehouse'


def test_manual_attribution_resolves_multiple_candidates(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.fail_submit = True
    a.found = [{'item_id': n, 'warehouse': True, 'shop': 'test shop'}
               for n in ('111111111111', '222222222222')]
    assert w.run(p, submit=True)['state'] == 'submission_unknown'
    evidence = tmp_path / 'human.txt'
    evidence.write_text('operator checked task attribution', encoding='utf-8')
    s.resolve_submission(p.task_id, 'operator', 'candidate', [evidence],
                         'authorize checking this item', '222222222222')
    assert w.recover(p)['item_id'] == '222222222222'
    assert a.clicks == 1


def test_submit_intent_preserves_all_checks_and_image_proofs(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    s.register(p)
    obs = Observation(p.expected(), complete=True, source='internal_test',
                      evidence={'materials': {'sku': [{'platform_id': '123', 'content_ok': True}]}})
    w._observe(p, obs)
    s.reserve_submit(p, {'ids': [], 'complete': True})
    old = copy.deepcopy(s.intent(p.task_id)['proof'])
    assert any(c['path'] == 'skus.0.price' for c in old['checks'])
    assert old['evidence']['materials']['sku'][0]['content_ok']
    obs.values['skus.0.price'] = '999.00'
    w._observe(p, obs)
    assert s.intent(p.task_id)['proof'] == old
    assert len(s.report(p.task_id)['observations']) == 2


def test_image_change_during_fill_blocks_intent(tmp_path, product_data):
    product_data['media']['root'] = str(tmp_path)
    names = ['main.png', 'detail.png', 'sku.png']
    product_data['media']['main'] = names[:1]
    product_data['media']['detail'] = names[1:2]
    product_data['skus'][0]['image']['value'] = names[2]
    for n in names:
        Image.new('RGB', (12, 12), 'red').save(tmp_path / n)
    p, s, a, w = setup_run(tmp_path, product_data)
    p.media_digest = manifest(p)['files']
    a.fill_known = lambda *args: Image.new('RGB', (12, 12), 'blue').save(tmp_path / 'sku.png')
    assert w.run(p, submit=True)['state'] == 'paused'
    assert a.clicks == 0 and s.intent(p.task_id) is None


def test_pause_persisted_before_capture(tmp_path, product_data):
    p, s, a, w = setup_run(tmp_path, product_data)
    a.read_form = lambda p: (_ for _ in ()).throw(PlatformPause('conflict'))
    def capture(label):
        assert s.report(p.task_id)['state'] == 'paused'
        return {'capture_error': 'timed out'}
    a.capture = capture
    result = w.run(p)
    assert result['state'] == 'paused'
    assert result['evidence'] == {'capture_error': 'timed out'}


def test_capture_timeout_is_bounded_and_saved(ui, monkeypatch):
    def hung(*args, **kwargs):
        assert kwargs['timeout'] <= 10
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'])
    monkeypatch.setattr('qianniu.browser.subprocess.run', hung)
    start = time.monotonic()
    result = ui.capture('timeout-test')
    assert time.monotonic() - start < 1
    saved = json.loads(__import__('pathlib').Path(result['json']).read_text(encoding='utf-8'))
    assert saved['capture_error'] and result['screenshot'] is None


def test_capture_kills_a_nonreturning_worker(ui, monkeypatch, tmp_path):
    import sys
    from qianniu.runtime import ProcessLock
    original = subprocess.run
    def hung_process(*args, **kwargs):
        return original([sys.executable, '-c', 'import time; time.sleep(60)'], timeout=0.2,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    monkeypatch.setattr('qianniu.browser.subprocess.run', hung_process)
    start = time.monotonic()
    with ProcessLock(tmp_path / 'worker.lock'):
        record = ui.capture('hanging-worker')
    with ProcessLock(tmp_path / 'worker.lock'):
        assert record['screenshot'] is None
    assert time.monotonic() - start < 3


def test_saved_snapshot_redacts_hidden_password_links_and_html(ui):
    ui.page.set_content('<div role="group"><input type="password" value="secret-password">'
                        '<input type="hidden" name="csrf-token" value="secret-token"></div>'
                        '<a href="https://example.com/?token=secret-link#secret-fragment">link</a>')
    saved = json.dumps(ui.snapshot())
    for secret in ('secret-password', 'secret-token', 'secret-link', 'secret-fragment', 'outerHTML'):
        assert secret not in saved


def test_sku_image_labels_are_not_pixel_proof(ui, tmp_path, product_data):
    import base64
    from io import BytesIO
    local = tmp_path / 'sku.png'
    Image.new('RGB', (12, 12), 'red').save(local)
    product_data['media']['root'] = str(tmp_path)
    p = Product(product_data)
    buf = BytesIO()
    Image.new('RGB', (12, 12), 'blue').save(buf, format='PNG')
    src = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    ui.page.set_content('<table aria-label="SKU"><thead><tr><th>规格</th><th>图片</th></tr></thead>'
                        '<tbody><tr><td>JG6-6</td><td data-id="real-material">'
                        f'<span class="filename">sku.png</span><img src="{src}"></td></tr></tbody></table>')
    a = Qianniu(ui, {'sku': {'table': {'role': 'table', 'name': 'SKU'}, 'fully_rendered': True,
                              'columns': {'name': '规格', 'image': '图片'},
                              'image': {'filename': '.filename', 'image': 'img', 'id_attribute': 'data-id'}}}, None)
    a.product = p
    with pytest.raises(PlatformPause):
        a.read_all_skus()


def test_category_entry_pause_preserves_observed_shop(ui, product_data):
    ui.goto('http://127.0.0.1/category')
    ui.page.set_content('<span>test shop</span>搜索发品')
    result = Qianniu(ui, {}, None).read_form(Product(product_data))
    assert result.values['shop'] == 'test shop'
    assert 'category' in str(result.blockers)
