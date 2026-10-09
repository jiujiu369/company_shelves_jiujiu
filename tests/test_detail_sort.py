import copy
import pytest

from qianniu.browser import PlatformPause
from qianniu.detail_sort import sort_plan, image_key


def audit():
    def group(name, segments):
        return {'matches': [], 'similarity_candidates': [{'name': name, 'rms': 3, 'acceptance': False}],
                'segments': [{'src': s, 'width': 12, 'height': 6, 'sha256': s} for s in segments]}
    return {'expected_names': ['a.png', 'b.png'], 'module_count': 3,
            'groups': [group('b.png', ['https://img.test/b~crop,0,0,12,6~.png',
                                      'https://img.test/b~crop,0,6,12,6~.png']),
                       group('a.png', ['https://img.test/a.png'])], 'ok': False}


def test_authorized_sort_plan_preserves_every_existing_segment_without_accepting_content():
    plan = sort_plan(audit(), '修改顺序就行')
    assert plan['current_names'] == ['b.png', 'a.png']
    assert plan['desired_keys'] == ['https://img.test/a.png', 'https://img.test/b~crop,0,0,12,6~.png',
                                   'https://img.test/b~crop,0,6,12,6~.png']
    assert plan['content_accepted'] is False
    assert image_key('https://img.test/a.png_320x320q80_.webp') == 'https://img.test/a.png'


@pytest.mark.parametrize('change', ['no_authorization', 'ambiguous', 'distant', 'duplicate', 'inverted_segment', 'count'])
def test_sort_plan_pauses_on_conflicts(change):
    data, authorization = audit(), '修改顺序就行'
    if change == 'no_authorization':
        authorization = ''
    elif change == 'ambiguous':
        data['groups'][1]['similarity_candidates'].append({'name': 'b.png', 'rms': 3.1})
    elif change == 'distant':
        data['groups'][1]['similarity_candidates'][0]['rms'] = 30
    elif change == 'duplicate':
        data['groups'][1] = copy.deepcopy(data['groups'][0])
    elif change == 'inverted_segment':
        data['groups'][0]['segments'].reverse()
    elif change == 'count':
        data['module_count'] = 2
    with pytest.raises(PlatformPause):
        sort_plan(data, authorization)


def test_native_drag_moves_existing_tile_using_unique_source_and_target(ui):
    ui.page.set_content('''<div id="tiles" style="display:flex;gap:20px">
      <div id="b" draggable="true" style="width:90px;height:90px;background:red">b</div>
      <div id="a" draggable="true" style="width:90px;height:90px;background:blue">a</div></div>
      <script>let source;document.addEventListener('dragstart',e=>{source=e.target;});
      document.addEventListener('dragover',e=>e.preventDefault());
      document.addEventListener('drop',e=>{e.preventDefault();e.target.parentElement.insertBefore(source,e.target);});</script>''')
    ui.drag_before({'css': '#a'}, {'css': '#b'})
    assert ui.page.locator('#tiles > div').all_inner_texts() == ['a', 'b']


def test_module_handle_avoids_dragging_nested_material_control(ui):
    ui.page.set_content('''<div id="tiles" style="display:flex;gap:20px">
      <div id="b" draggable="true" style="width:90px;height:90px"><span class="handle">b</span>
        <div draggable="true" style="height:70px" ondragover="event.stopPropagation()" ondrop="event.stopPropagation()">nested</div></div>
      <div id="a" draggable="true" style="width:90px;height:90px"><span class="handle">a</span>
        <div draggable="true">nested</div></div></div>
      <script>let source;document.addEventListener('dragstart',e=>{source=e.target;});
      document.addEventListener('dragover',e=>e.preventDefault());
      document.addEventListener('drop',e=>{e.preventDefault();let target=e.target.closest('#tiles > div');
        if(source.parentElement.id==='tiles')target.parentElement.insertBefore(source,target);});</script>''')
    ui.drag_before({'css': '#a', 'drag_handle': '.handle'}, {'css': '#b', 'drag_handle': '.handle'})
    assert ui.page.locator('#tiles > div').evaluate_all('(es)=>es.map(e=>e.id)') == ['a', 'b']


def test_native_drag_waits_for_browser_delivered_drag_events(ui):
    ui.page.set_content('''<div id="tiles" style="display:flex;gap:20px">
      <div id="b" draggable="true" style="width:90px;height:90px">b</div>
      <div id="a" draggable="true" style="width:90px;height:90px">a</div></div>
      <script>let source,ready=false;document.addEventListener('dragstart',e=>{
        source=e.target;requestAnimationFrame(()=>requestAnimationFrame(()=>ready=true));});
      document.addEventListener('dragover',e=>e.preventDefault());
      document.addEventListener('drop',e=>{e.preventDefault();if(ready)e.target.parentElement.insertBefore(source,e.target);});</script>''')
    ui.drag_before({'css': '#a'}, {'css': '#b'})
    assert ui.page.locator('#tiles > div').evaluate_all('(es)=>es.map(e=>e.id)') == ['a', 'b']


from test_platform import ui


def test_sort_adapter_moves_only_existing_images_and_persists_authorization(ui, tmp_path, product_data, monkeypatch):
    from qianniu.platform import Qianniu
    from qianniu.data import Product
    from qianniu.store import Store
    ui.page.set_content('''<span>test shop</span><div id="tiles" style="display:flex;gap:20px">
      <div draggable="true" style="width:90px;height:90px"><img src="https://img.test/b.png"></div>
      <div draggable="true" style="width:90px;height:90px"><img src="https://img.test/a.png"></div></div>
      <button>提交宝贝信息</button>
      <script>let source;document.addEventListener('dragstart',e=>{source=e.target.closest('[draggable]');});
      document.addEventListener('dragover',e=>e.preventDefault());document.addEventListener('drop',e=>{
        e.preventDefault();let target=e.target.closest('[draggable]');target.parentElement.insertBefore(source,target);});</script>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'detail_sort': {'container': {'css': '#tiles'}, 'items': '[draggable=true]', 'image': 'img'},
                    'detail_preview': {'frame_url': '/publish', 'images': '#tiles img'}}, s)
    def read_audit(_):
        names = [image_key(u).rsplit('/', 1)[-1] for u in ui.page.locator('#tiles img').evaluate_all('(es)=>es.map(e=>e.src)')]
        return {'expected_names': ['a.png', 'b.png'], 'module_count': 2, 'ok': False,
                'groups': [{'matches': [n], 'segments': [{'src': 'https://img.test/' + n, 'sha256': n,
                                                         'width': 12, 'height': 12}]} for n in names]}
    monkeypatch.setattr(a, 'audit_detail_preview', read_audit)
    result = a.reorder_details(p, '修改顺序就行')
    assert result['order_ok'] and result['module_count'] == 2 and not result['content_accepted']
    assert s.intent(p.task_id) is None
    assert any(e['kind'] == 'detail_sort_authorization' for e in s.report(p.task_id)['events'])
    assert ui.page.get_by_role('button').inner_text() == '提交宝贝信息'
    assert a.reorder_details(p, '修改顺序就行')['moves'] == 0
