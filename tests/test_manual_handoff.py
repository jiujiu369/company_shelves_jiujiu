import copy
import pytest
from test_platform import ui
from qianniu.data import Product
from qianniu.platform import Qianniu
from qianniu.store import Store
from qianniu.browser import PlatformPause


def test_manual_order_checkpoint_only_unblocks_independent_steps(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><label>型号<input></label>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'detail_preview': {}, 'fields': {'fields.model': {'selector': {'label': '型号'}}}}, s)
    record = {'content_order_ok': False, 'ok': False, 'groups': [{'segments': [{'src': 'actual-image', 'sha256': 'actual-hash'}]}]}
    a.profile['detail_preview'] = {'observed': True}
    monkeypatch.setattr(a, 'audit_detail_preview', lambda _: copy.deepcopy(record))
    with pytest.raises(PlatformPause):
        a.fill_known(p, ['fields.model'])
    result = a.ack_manual_details(p, '跳过这个排序，我已经修改了顺序')
    assert result['skip_automatic_sort'] and not result['content_accepted']
    a.fill_known(p, ['fields.model'])
    assert ui.page.get_by_label('型号').input_value() == 'JG铜接线端子'
    assert not a.read_form(p).complete and s.intent(p.task_id) is None
    record['groups'][0]['segments'][0]['sha256'] = 'changed'
    with pytest.raises(PlatformPause):
        a.fill_known(p, ['fields.model'])


def test_manual_report_requires_direct_statement(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {}, s)
    with pytest.raises(PlatformPause):
        a.ack_manual_details(p, '')


def test_stage_single_sku_mode_never_confirms_creation(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><div class="sku-table">+ 创建规格</div>
      <dialog open><label>自定义填写规格<input type="radio"></label><input placeholder="输入规格"><button>确认创建</button></dialog>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'specification': {'empty_region': {'css': '.sku-table'}, 'empty_text': '+ 创建规格',
                    'region': {'css': 'dialog'}, 'custom_mode': {'scope': [{'css': 'dialog'}], 'label': '自定义填写规格'},
                    'name': {'scope': [{'css': 'dialog'}], 'placeholder': '输入规格'}, 'name_inputs': 'input[placeholder="输入规格"]'}}}, s)
    result = a.stage_single_sku(p)
    assert result['state'] == 'sku_name_staged' and not result['committed']
    assert ui.page.get_by_label('自定义填写规格').is_checked()
    assert ui.page.get_by_placeholder('输入规格').input_value() == 'JG6-6'
    assert ui.page.get_by_role('button').inner_text() == '确认创建' and s.intent(p.task_id) is None


def test_sku_picker_opens_only_the_exact_staged_sku(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><section id="panel"><div class="row">
      <input placeholder="输入规格" value="JG6-6"><button onclick="document.querySelector('dialog').showModal()">图片</button>
      </div></section><dialog>素材<input type="checkbox"></dialog>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'specification': {'region': {'css': '#panel'}, 'name_inputs': 'input',
                    'material_open': {'scope': [{'css': '#panel'}], 'css': '.row:has(input[value={name}]) button'}}}}, s)
    assert a.open_sku_images(p)['state'] == 'sku_image_picker_opened'
    assert ui.page.locator('dialog').is_visible() and not ui.page.locator('dialog input').is_checked()
    assert s.intent(p.task_id) is None


def test_material_folder_readback_preserves_identity_and_never_selects_image(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><ul id="tree">
      <li value="folder-jg"><div role="treeitem" aria-level="1" aria-expanded="true" aria-selected="false">JG</div></li>
      <li value="folder-sku"><div role="treeitem" aria-level="2" aria-selected="false" onclick="this.setAttribute('aria-selected','true')">sku</div></li>
      </ul><div class="pic" data-folder="folder-sku"><span class="name">sku.png</span><span class="size">1440x1440</span><input type="checkbox" value="asset-6"><img src="thumb.png"></div>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    profile = {'sku_materials': {'tree': {'css': '#tree'}, 'cards': {'css': '.pic'},
               'list_folder': {'css': '.pic'}, 'list_folder_attribute': 'data-folder',
               'filename': '.name', 'dimensions': '.size', 'checkbox': 'input[type=checkbox]', 'image': 'img'}}
    a = Qianniu(ui, profile, s)
    result = a.browse_sku_materials(p)
    assert result['folder'] == 'JG/sku' and result['folder_ids'] == ['folder-jg', 'folder-sku']
    assert result['materials'][0]['filename'] == 'sku.png'
    assert result['materials'][0]['material_id'] == 'asset-6' and not result['selected']
    assert not ui.page.locator('.pic input').is_checked() and s.intent(p.task_id) is None


def test_material_folder_refuses_same_named_child_from_other_parent(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><ul id="tree">
      <li value="jg"><div role="treeitem" aria-level="1" aria-expanded="true">JG</div></li>
      <li value="other"><div role="treeitem" aria-level="1">other</div></li>
      <li value="sku"><div role="treeitem" aria-level="2">sku</div></li></ul>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'sku_materials': {'tree': {'css': '#tree'}}}, s)
    with pytest.raises(PlatformPause, match='folder hierarchy'):
        a.browse_sku_materials(p)


def test_sku_preview_uses_exact_name_and_does_not_associate_image(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><section id="cards"><div class="pic"><span class="name">施科特_SKU_JG6-6.png</span>
      <input type="checkbox" value="asset6"><button class="preview" onclick="document.querySelector('dialog').showModal()">预览</button></div></section><dialog>大图预览</dialog>''')
    product_data['skus'][0]['image']['value'] = '施科特_SKU_JG6-6.png'
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'sku_materials': {'cards': {'css': '#cards'}, 'card_items': '.pic',
                  'filename': '.name', 'checkbox': 'input', 'full_view': '.preview'}}, s)
    result = a.open_sku_preview(p)
    assert result['material_id'] == 'asset6' and not result['selected']
    assert ui.page.locator('dialog').is_visible() and not ui.page.locator('.pic input').is_checked()


def test_name_only_sku_creation_reads_actual_row_and_preserves_submission_gate(ui, tmp_path, product_data):
    a, p, s = creation_adapter(ui, tmp_path, product_data)
    result = a.commit_single_sku_name(p)
    assert result['state'] == 'sku_name_created_image_pending' and result['actual_names'] == ['JG6-6']
    assert not result['image_accepted'] and not result['submitted'] and s.intent(p.task_id) is None


def test_split_sku_header_reads_fake_textarea_and_offscreen_cells_safely(ui, tmp_path):
    ui.page.set_content('''<div id="sku"><div class="head" title="商品规格"></div><div class="head" title="价格"></div><div class="head" title="数量"></div>
      <table><tbody><tr><td><textarea data-real="true">JG6-6</textarea><textarea data-fake="true" style="visibility:hidden"></textarea></td>
      <td><input value="100"></td><td><input value="999"></td></tr></tbody></table></div><div id="count">1 / 1</div>''')
    s = Store(tmp_path / 'state.sqlite')
    a = Qianniu(ui, {'sku': {'table': {'css': '#sku'}, 'headers': '.head', 'header_attribute': 'title',
        'controls': 'input,textarea:not([data-fake=true])', 'columns': {'name': '商品规格', 'price': '价格', 'inventory': '数量'},
        'count': {'css': '#count'}, 'count_pattern': r'1\s*/\s*(1)'}}, s)
    result = a.read_all_skus()
    assert result['rows'] == [{'name': 'JG6-6', 'price': '100.00', 'inventory': 999}]
    assert result['declared_count'] == 1


def test_more_batch_open_is_restricted_to_reread_single_sku(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><button onclick="document.querySelector(\'dialog\').showModal()">更多批量</button><dialog>批量填写</dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'batch': {'single_sku_only': True, 'open': {'role': 'button', 'name': '更多批量'}}}, s)
    monkeypatch.setattr(a, 'read_all_skus', lambda: {'rows': [{'name': 'JG6-6'}], 'declared_count': 1})
    assert a.open_sku_batch(p)['state'] == 'sku_batch_opened'
    assert ui.page.locator('dialog').is_visible() and s.intent(p.task_id) is None


def test_batch_refuses_conflicting_existing_price_before_opening(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><button>更多批量</button>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'batch': {'single_sku_only': True, 'fields': {'price': {}}, 'open': {'role': 'button', 'name': '更多批量'}}}, s)
    monkeypatch.setattr(a, 'read_all_skus', lambda: {'rows': [{'name': 'JG6-6', 'price': '200.00'}], 'declared_count': 1})
    with pytest.raises(PlatformPause, match='conflicting existing SKU'):
        a.fill_batch(p, p.known_paths())


def test_area_editor_opens_after_single_sku_range_check(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><button>更多批量</button><button id="area" onclick="document.querySelector(\'dialog\').showModal()">导线截面积</button><dialog>面积单位</dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'batch': {'single_sku_only': True, 'open': {'role': 'button', 'name': '更多批量'}, 'area_open': {'css': '#area'}}}, s)
    monkeypatch.setattr(a, 'read_all_skus', lambda: {'rows': [{'name': 'JG6-6'}], 'declared_count': 1})
    assert a.open_sku_batch(p, area=True)['state'] == 'sku_area_opened'
    assert ui.page.locator('dialog').is_visible()


def test_measurement_write_requires_and_reads_actual_unit(ui, tmp_path):
    ui.page.set_content('''<button id="open">导线截面积</button><div id="editor"><span id="unit">平方毫米</span>
      <input placeholder="请输入数量" oninput="document.querySelector('#result').value=this.value+'平方毫米'"></div>
      <input id="result"><button id="finish">关闭编辑</button>''')
    a = Qianniu(ui, {}, Store(tmp_path / 'state.sqlite'))
    binding = {'selector': {'css': '#open'}, 'action': 'measurement', 'measurement_unit': '平方毫米',
               'editor': {'css': '#editor'}, 'unit_selector': {'css': '#unit'},
               'number_selector': {'placeholder': '请输入数量'}, 'finish': {'css': '#finish'},
               'value_selector': {'css': '#result'}}
    a._write_binding(binding, '6')
    assert a._read_binding(binding) == '6'
    ui.page.locator('#unit').evaluate("e=>e.textContent='平方米'")
    with pytest.raises(PlatformPause, match='measurement unit'):
        a._write_binding(binding, '6')


def test_batch_finishes_measurement_overlay_before_other_controls(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><button>应用</button>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'batch': {'single_sku_only': True, 'fields': {'price': {'key': 'price'}, 'area': {'key': 'area', 'action': 'measurement'}},
                             'apply': {'role': 'button', 'name': '应用'}}}, s)
    row = {'name': 'JG6-6', 'price': None, 'area': None}
    order = []
    monkeypatch.setattr(a, 'read_all_skus', lambda: {'rows': [row.copy()], 'declared_count': 1})
    monkeypatch.setattr(a, 'open_sku_batch', lambda _: None)
    def write(binding, value):
        order.append(binding['key'])
        row[binding['key']] = value
    monkeypatch.setattr(a, '_write_binding', write)
    monkeypatch.setattr(a, '_read_binding', lambda binding: '')
    a.fill_batch(p, p.known_paths())
    assert order == ['area', 'price']


def test_logistics_panel_selects_observed_single_origin_step(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><label>大陆及港澳台<input type="radio"></label><label>单一发货地<input type="radio"></label>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'logistics': {'selector': {'label': '大陆及港澳台'}, 'after': [{'label': '单一发货地'}]}}}, s)
    a.open_panel(p, 'logistics')
    assert ui.page.get_by_label('大陆及港澳台').is_checked() and ui.page.get_by_label('单一发货地').is_checked()


def test_panel_can_open_observed_non_submitting_editor_after_radio(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><label>大陆及港澳台<input type="radio"></label><button onclick="document.querySelector(\'dialog\').showModal()">地区</button><dialog>地区选项</dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'logistics': {'selector': {'label': '大陆及港澳台'}, 'after': [{'role': 'button', 'name': '地区', 'action': 'click'}]}}}, s)
    a.open_panel(p, 'logistics')
    assert ui.page.locator('dialog').is_visible()


def test_origin_selects_configured_province_city_and_rereads_combined_value(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('''<span>test shop</span><button id="origin">安徽 / 合肥</button><div id="options">
      <div role="option" title="浙江">浙江 icon</div><div role="option" title="温州" onclick="document.querySelector('#origin').textContent='浙江 / 温州'">温州</div></div>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'origin': {'control': {'css': '#origin'}, 'value': {'css': '#origin'},
        'options': {'css': '#options'}, 'option_css': '[role=option][title={name}]', 'province_names': {'浙江省': '浙江'}, 'city_names': {'温州市': '温州'}}}, s)
    original_wait = ui.wait
    monkeypatch.setattr(ui, 'wait', lambda predicate, timeout=30: original_wait(predicate, timeout=2))
    result = a.set_origin(p, '安徽 / 合肥')
    assert result['actual'] == '浙江 / 温州' and result['province'] == '浙江省' and result['city'] == '温州市'
    assert not result['submitted'] and s.intent(p.task_id) is None
    ui.page.locator('#origin').evaluate("e=>e.textContent='上海 / 上海'")
    with pytest.raises(PlatformPause, match='origin changed'):
        a.set_origin(p, '安徽 / 合肥')


def test_origin_reads_display_attribute_not_empty_search_input(ui, tmp_path):
    ui.page.set_content('<input role="combobox" aria-valuetext="浙江 / 温州" value="">')
    a = Qianniu(ui, {'origin': {'value': {'role': 'combobox'}, 'value_attribute': 'aria-valuetext'}}, Store(tmp_path / 'state.sqlite'))
    assert a._origin_value() == '浙江 / 温州'


def test_phase_failure_persists_pause_even_when_capture_fails(ui, tmp_path, product_data, monkeypatch):
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {}, s)
    def failed():
        raise PlatformPause('ambiguous control')
    def capture(_):
        assert s.report(p.task_id)['state'] == 'paused'
        raise RuntimeError('capture unavailable')
    monkeypatch.setattr(a, 'capture', capture)
    with pytest.raises(PlatformPause, match='ambiguous control'):
        a.run_phase(p, failed)
    assert s.report(p.task_id)['events'][-1]['kind'] == 'phase_failure'
    assert s.report(p.task_id)['state'] == 'paused'


def test_unit_mapping_needs_sku_specific_confirmed_one_to_one_source(ui, tmp_path, product_data):
    p = Product(product_data)
    a = Qianniu(ui, {}, Store(tmp_path / 'state.sqlite'))
    row = {'name': 'JG6-6', 'inventory_unit': '件', 'price': '100.00', 'inventory': 999}
    assert a._business_units(p, row)['inventory_unit'] == '件'
    p.data['platform_unit_rules'] = [{'sku': 'JG6-6', 'platform': '件', 'business': '包', 'factor': 1,
                                    'source': '用户直接确认一件就是一包', 'confirmed': True}]
    mapped = a._business_units(p, row)
    assert mapped['inventory_unit'] == mapped['sales_unit'] == '包'
    assert mapped['platform_inventory_unit'] == '件' and mapped['price'] == '100.00' and mapped['inventory'] == 999
    p.data['platform_unit_rules'][0]['confirmed'] = False
    with pytest.raises(PlatformPause, match='unit mapping'):
        a._business_units(p, row)


def creation_adapter(ui, tmp_path, product_data, extra='', close=True):
    html = '<table><thead><tr><th>商品规格</th></tr></thead><tbody><tr><td>JG6-6</td></tr>' + extra + '</tbody></table>'
    import json
    action = "document.querySelector('#table').innerHTML=" + json.dumps(html)
    if close:
        action += ";document.querySelector('dialog').close()"
    ui.page.set_content('<span>test shop</span><div id="table">+ 创建规格</div><dialog open><input value="JG6-6">'
                        + '<button id="confirm">确认创建</button></dialog>')
    ui.page.locator('#confirm').evaluate('(e,code)=>e.onclick=()=>{window.clicks=(window.clicks||0)+1;eval(code)}', action)
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'panels': {'specification': {'region': {'css': 'dialog'}, 'name_inputs': 'input',
        'empty_region': {'css': '#table'}, 'empty_text': '+ 创建规格', 'staged_images': 'img',
        'confirm': {'css': '#confirm'}}}, 'sku': {'table': {'css': '#table table'}, 'columns': {'name': '商品规格'},
        'fully_rendered': True}}, s)
    original = ui.wait
    ui.wait = lambda predicate, timeout=30: original(predicate, timeout=.8)
    return a, p, s


def test_creation_requires_confirmed_source_before_any_click(ui, tmp_path, product_data):
    product_data['skus'][0]['name']['confirmed'] = False
    a, p, s = creation_adapter(ui, tmp_path, product_data)
    with pytest.raises(PlatformPause, match='confirmed SKU name'):
        a.commit_single_sku_name(p)
    assert ui.page.evaluate('window.clicks||0') == 0


def test_creation_rejects_extra_actual_sku(ui, tmp_path, product_data):
    a, p, s = creation_adapter(ui, tmp_path, product_data, '<tr><td>extra</td></tr>')
    with pytest.raises(PlatformPause, match='actual SKU'):
        a.commit_single_sku_name(p)
    assert ui.page.evaluate('window.clicks') == 1


def test_creation_timeout_cannot_confirm_twice(ui, tmp_path, product_data):
    a, p, s = creation_adapter(ui, tmp_path, product_data, close=False)
    ui.page.locator('#confirm').evaluate('e=>e.onclick=()=>{window.clicks=(window.clicks||0)+1}')
    with pytest.raises(PlatformPause):
        a.commit_single_sku_name(p)
    with pytest.raises(PlatformPause, match='creation result unknown'):
        a.commit_single_sku_name(p)
    assert ui.page.evaluate('window.clicks') == 1


def test_manual_handoff_blocks_sort_across_product_version(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    s.event(p.task_id, 'manual_detail_order', {'skip_automatic_sort': True, 'version': 'old-version'})
    a = Qianniu(ui, {}, s)
    with pytest.raises(PlatformPause, match='manual detail order'):
        a.reorder_details(p, 'old authorization')


def test_batch_preserves_open_modal_conflict(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><dialog open><input aria-label="价格" value="200"><button>应用</button></dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'batch': {'single_sku_only': True, 'region': {'css': 'dialog'},
        'fields': {'price': {'selector': {'label': '价格'}}}, 'apply': {'role': 'button', 'name': '应用'}}}, s)
    monkeypatch.setattr(a, 'read_all_skus', lambda: {'rows': [{'name': 'JG6-6', 'price': None}], 'declared_count': 1})
    with pytest.raises(PlatformPause, match='staged batch conflict'):
        a.fill_batch(p, p.known_paths())
    assert ui.page.get_by_label('价格').input_value() == '200'


def test_upload_panel_requires_exact_file_folder_authorization(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><button onclick="document.querySelector(\'dialog\').showModal()">本地上传</button><dialog>上传</dialog>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'local_upload': {'open': {'role': 'button', 'name': '本地上传'}}}, s)
    with pytest.raises(PlatformPause, match='upload authorization'):
        a.open_local_upload(p, 'detail.png', 'JG/详情图')
    p.data['media']['local_upload_authorizations'] = [{'file': 'detail.png', 'folder': 'JG/详情图', 'source': '用户同意', 'confirmed': True}]
    result = a.open_local_upload(p, 'detail.png', 'JG/详情图')
    assert ui.page.locator('dialog').is_visible() and not result['uploaded']


def test_folder_must_not_accept_stale_same_name_cards(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('''<span>test shop</span><ul id="tree"><li value="jg"><div role="treeitem" aria-level="1" aria-expanded="true">JG</div></li>
        <li value="sku"><div role="treeitem" aria-level="2" onclick="this.setAttribute('aria-selected','true')">sku</div></li></ul>
        <div id="list" data-folder="old"><div class="pic"><span class="name">sku.png</span><span class="size">1440x1440</span><input type="checkbox" value="old-asset"><img src="x.png"></div></div>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'sku_materials': {'tree': {'css': '#tree'}, 'cards': {'css': '#list'}, 'card_items': '.pic',
        'list_folder': {'css': '#list'}, 'list_folder_attribute': 'data-folder',
        'filename': '.name', 'dimensions': '.size', 'checkbox': 'input', 'image': 'img'}}, s)
    original = ui.wait
    monkeypatch.setattr(ui, 'wait', lambda predicate, timeout=30: original(predicate, timeout=.5))
    with pytest.raises(PlatformPause):
        a.browse_sku_materials(p)
    assert not [e for e in s.report(p.task_id)['events'] if e['kind'] == 'sku_material_folder']


def test_folder_transition_waits_for_new_card_identity(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><ul id="tree"><li value="jg"><div role="treeitem" aria-level="1" aria-expanded="true">JG</div></li>
        <li value="sku"><div role="treeitem" aria-level="2" onclick="this.setAttribute('aria-selected','true');setTimeout(()=>document.querySelector('input').setAttribute('value','new-asset'),500)">sku</div></li></ul>
        <div class="pic"><span class="name">sku.png</span><span class="size">1440x1440</span><input type="checkbox" value="old-asset"><img src="x.png"></div>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'sku_materials': {'tree': {'css': '#tree'}, 'cards': {'css': '.pic'},
        'list_transition': True, 'filename': '.name', 'dimensions': '.size', 'checkbox': 'input', 'image': 'img'}}, s)
    assert a.browse_sku_materials(p)['materials'][0]['material_id'] == 'new-asset'


def test_upload_destination_opener_never_transfers_a_file(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><input aria-label="上传至" onclick="window.opened=true">')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    p.data['media']['local_upload_authorizations'] = [{'file': 'detail.png', 'folder': 'JG/详情图', 'confirmed': True, 'source': 'user'}]
    a = Qianniu(ui, {'local_upload': {'destination': {'label': '上传至'}}}, s)
    assert not a.open_upload_folder(p, 'detail.png', 'JG/详情图')['uploaded']
    assert ui.page.evaluate('window.opened') is True


def test_upload_folder_choice_reads_full_path_not_leaf(ui, tmp_path, product_data):
    ui.page.set_content('''<span>test shop</span><input aria-label="上传至" aria-valuetext="JG / sku">
        <section id="options"><div role="option" title="JG">JG</div><div role="option" title="详情图"
        onclick="document.querySelector('input').setAttribute('aria-valuetext','JG / 详情图')">详情图</div></section>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    p.data['media']['local_upload_authorizations'] = [{'file': 'detail.png', 'folder': 'JG/详情图', 'source': 'user', 'confirmed': True}]
    a = Qianniu(ui, {'local_upload': {'destination': {'label': '上传至'}, 'options': {'css': '#options'},
        'option_css': '[role=option][title={name}]'}}, s)
    assert a.set_upload_folder(p, 'detail.png', 'JG/详情图')['actual_folder'] == 'JG/详情图'


def test_upload_unknown_result_cannot_transfer_twice(ui, tmp_path, product_data, monkeypatch):
    from PIL import Image
    Image.new('RGB', (200, 400), 'red').save(tmp_path / 'detail.png')
    product_data['media']['root'] = str(tmp_path)
    product_data['media']['local_upload_authorizations'] = [{'file': 'detail.png', 'folder': 'JG/详情图', 'source': 'user', 'confirmed': True}]
    ui.page.set_content('''<span>test shop</span><input aria-label="上传至" aria-valuetext="JG / 详情图">
        <label>原图上传<input type="checkbox" checked></label><input type="file" onchange="window.transfers=(window.transfers||0)+1"><section id="files"></section>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    profile = {'local_upload': {'destination': {'label': '上传至'}, 'original': {'label': '原图上传'},
        'input': {'css': 'input[type=file]'}, 'files': {'css': '#files'}, 'complete_text': '上传成功'}}
    a = Qianniu(ui, profile, s)
    original = ui.wait
    monkeypatch.setattr(ui, 'wait', lambda predicate, timeout=30: original(predicate, timeout=.5))
    with pytest.raises(PlatformPause):
        a.upload_local_material(p, 'detail.png', 'JG/详情图')
    with pytest.raises(PlatformPause, match='upload result unknown'):
        a.upload_local_material(p, 'detail.png', 'JG/详情图')
    assert ui.page.evaluate('window.transfers') == 1


def test_inspection_close_preserves_selected_material(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span><section id="panel"><input type="checkbox" checked><button>关闭</button></section>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    s.event(p.task_id, 'panel_inspection', {'panel': 'materials'})
    a = Qianniu(ui, {'panels': {'materials': {'region': {'css': '#panel'}, 'staged_checkboxes': 'input[type=checkbox]',
                 'close': {'role': 'button', 'name': '关闭'}}}}, s)
    with pytest.raises(PlatformPause, match='staged selections'):
        a.close_panel(p, 'materials')
    assert ui.page.locator('input').is_checked()


def test_detail_metadata_mapping_never_accepts_reencoding_or_sorts(ui, tmp_path, product_data):
    from PIL import Image
    Image.new('RGB', (200, 400), 'red').save(tmp_path / 'detail.png')
    product_data['media']['root'] = str(tmp_path)
    p = Product(product_data)
    a = Qianniu(ui, {}, None)
    audit = {'version': p.version, 'groups': [{'segments': [
        {'src': 'https://img.alicdn.com/x/asset~crop,0,0,200,200~.png', 'width': 200, 'height': 200, 'sha256': 'a'},
        {'src': 'https://img.alicdn.com/x/asset~crop,0,200,200,200~.png', 'width': 200, 'height': 200, 'sha256': 'b'}]}]}
    metadata = {'folder': 'JG/详情图', 'materials': [{'filename': 'detail.png', 'material_id': 'new-v4', 'dimensions': '200x400',
        'preview': 'https://img.alicdn.com/x/asset.png_320x320?t=1'}]}
    result = a.map_detail_materials(p, audit, metadata)
    assert result['actual_names'] == ['detail.png'] and result['groups'][0]['material_id'] == 'new-v4'
    assert result['platform_filenames_verified'] and not result['content_accepted'] and not result['ok']
    metadata['materials'][0]['filename'] = 'old-v3.png'
    with pytest.raises(PlatformPause, match='filename/version'):
        a.map_detail_materials(p, audit, metadata)


def test_unit_mapping_preserves_conflicting_sales_unit(ui, tmp_path, product_data):
    p = Product(product_data)
    p.data['platform_unit_rules'] = [{'sku': 'JG6-6', 'platform': '件', 'business': '包', 'factor': 1, 'source': 'user', 'confirmed': True}]
    a = Qianniu(ui, {}, None)
    with pytest.raises(PlatformPause, match='sales unit'):
        a._business_units(p, {'name': 'JG6-6', 'inventory_unit': '件', 'sales_unit': '只'})


def test_file_action_can_use_unique_hidden_input(ui):
    ui.page.set_content('<input type="file" style="display:none">')
    ui.act({'css': 'input[type=file]'}, 'files', {'name': 'x.png', 'mimeType': 'image/png', 'buffer': b'content'})
    assert ui.page.locator('input').evaluate('e=>e.files[0].name') == 'x.png'


def test_upload_lock_uses_destination_filename_not_path_alias(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span>')
    product_data['media']['detail'] = ['./detail.png']
    product_data['media']['local_upload_authorizations'] = [{'file': './detail.png', 'folder': 'JG/详情图', 'confirmed': True, 'source': 'user'}]
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    s.event(p.task_id, 'local_upload_intent', {'file': 'detail.png', 'folder': 'JG/详情图'})
    a = Qianniu(ui, {}, s)
    with pytest.raises(PlatformPause, match='upload result unknown'):
        a.upload_local_material(p, './detail.png', 'JG/详情图')


def test_upload_rechecks_destination_after_intent_capture(ui, tmp_path, product_data, monkeypatch):
    from PIL import Image
    Image.new('RGB', (200, 400), 'red').save(tmp_path / 'detail.png')
    product_data['media']['root'] = str(tmp_path)
    product_data['media']['local_upload_authorizations'] = [{'file': 'detail.png', 'folder': 'JG/详情图', 'source': 'user', 'confirmed': True}]
    ui.page.set_content('''<span>test shop</span><input aria-label="上传至" aria-valuetext="JG / 详情图">
        <label>原图上传<input type="checkbox" checked></label><input type="file" onchange="window.transfers=(window.transfers||0)+1"><section id="files"></section>''')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'local_upload': {'destination': {'label': '上传至'}, 'original': {'label': '原图上传'},
        'input': {'css': 'input[type=file]'}, 'files': {'css': '#files'}}}, s)
    def capture(label):
        if label == 'upload-before':
            ui.page.get_by_label('上传至').evaluate("e=>e.setAttribute('aria-valuetext','JG / sku')")
        return {}
    monkeypatch.setattr(a, 'capture', capture)
    with pytest.raises(PlatformPause):
        a.upload_local_material(p, 'detail.png', 'JG/详情图')
    assert ui.page.evaluate('window.transfers||0') == 0


def test_manual_uploaded_material_prevents_program_duplicate_upload(ui, tmp_path, product_data):
    ui.page.set_content('<span>test shop</span>')
    product_data['media']['local_upload_authorizations'] = [{'file': 'detail.png', 'folder': 'JG/详情图', 'source': 'user', 'confirmed': True}]
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    s.event(p.task_id, 'sku_material_folder', {'folder': 'JG/详情图', 'materials': [{'filename': 'detail.png', 'material_id': 'user-created-v4'}]})
    a = Qianniu(ui, {}, s)
    with pytest.raises(PlatformPause, match='already exists'):
        a.upload_local_material(p, 'detail.png', 'JG/详情图')


def test_warehouse_inspection_keeps_unsaved_form_unchanged(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span><input value="unsaved">')
    original = ui.page
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'warehouse_entry': {'url': 'http://127.0.0.1/list', 'page_url_pattern': '/list'}}, s)
    def goto(url):
        ui.page.route(url, lambda route: route.fulfill(body='<span>test shop</span><div>仓库中(50)</div>', content_type='text/html; charset=utf-8'))
        ui.page.goto(url)
    monkeypatch.setattr(ui, 'goto', goto)
    result = a.open_product_list(p)
    assert ui.page is original and original.locator('input').input_value() == 'unsaved'
    assert result['state'] == 'product_list_opened' and not result['warehouse_verified']
    assert s.intent(p.task_id) is None


def test_product_list_waits_for_delayed_shop_header(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    a = Qianniu(ui, {'warehouse_entry': {'url': 'http://127.0.0.1/list', 'page_url_pattern': '/list'}}, s)
    def goto(url):
        ui.page.route(url, lambda route: route.fulfill(body="<body><script>setTimeout(()=>document.body.innerHTML='<span>test shop</span>',400)</script></body>", content_type='text/html; charset=utf-8'))
        ui.page.goto(url)
    monkeypatch.setattr(ui, 'goto', goto)
    assert a.open_product_list(p)['state'] == 'product_list_opened'


def test_warehouse_tab_click_is_not_complete_warehouse_query(ui, tmp_path, product_data, monkeypatch):
    ui.page.set_content('<span>test shop</span>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    s.register(p)
    selector = {'css': '[role=tab][data-ui-test=in_stock]'}
    a = Qianniu(ui, {'warehouse_entry': {'url': 'http://127.0.0.1/list', 'page_url_pattern': '/list', 'tab': selector}}, s)
    def goto(url):
        ui.page.route(url, lambda route: route.fulfill(body='''<span>test shop</span><div role="tab" data-ui-test="in_stock" aria-selected="false" onclick="this.setAttribute('aria-selected','true')">仓库中(50)</div>''', content_type='text/html; charset=utf-8'))
        ui.page.goto(url)
    monkeypatch.setattr(ui, 'goto', goto)
    result = a.open_product_list(p, warehouse=True)
    assert result['state'] == 'warehouse_tab_selected' and not result['warehouse_verified']


def test_list_shop_binding_excludes_hidden_account_menu(ui, tmp_path, product_data):
    ui.page.set_content('<div id="shop">test shop<div hidden>account menu</div></div>')
    p, s = Product(product_data), Store(tmp_path / 'state.sqlite')
    a = Qianniu(ui, {'warehouse_entry': {'page_url_pattern': '.*', 'shop': {'css': '#shop'}}}, s)
    assert a._identity(p, require_category=False)['shop'] == 'test shop'
    ui.page.locator('#shop').evaluate("e=>e.firstChild.textContent='different shop'")
    with pytest.raises(PlatformPause):
        a._identity(p, require_category=False)


def test_warehouse_pagination_waits_for_rows_not_only_page_number(ui):
    ui.page.set_content('''<span id="number">1</span><table id="table"><tr><td>ID:123456789012</td></tr></table>
      <button id="next" onclick="document.querySelector('#number').textContent='2';setTimeout(()=>document.querySelector('td').textContent='ID:123456789013',600)">Next</button>''')
    a = Qianniu(ui, {'warehouse': {'table': {'css': '#table'}, 'rows': 'tr',
        'page_number': {'css': '#number'}}}, None)
    a._turn_warehouse_page({'css': '#next'}, '2')
    assert ui.page.locator('td').inner_text() == 'ID:123456789013'
