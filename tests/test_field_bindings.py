from test_platform import ui
from qianniu.platform import Qianniu


def test_custom_select_reads_display_not_empty_search_input(ui):
    ui.page.set_content('<span class="choice"><span class="values">镀锡</span><input role="combobox"></span>')
    a = Qianniu(ui, {}, None)
    binding = {'selector': {'css': '.choice input'}, 'value_selector': {'css': '.choice .values'}}
    assert a._read_binding(binding) == '镀锡'


def test_checked_radio_value_is_from_checked_control_not_constant(ui):
    ui.page.set_content('<label>放入仓库<input type="radio"></label>')
    a = Qianniu(ui, {}, None)
    binding = {'selector': {'label': '放入仓库'}, 'action': 'check', 'checked_value': '放入仓库'}
    assert a._read_binding(binding) is None
    a._write_binding(binding, '放入仓库')
    assert a._read_binding(binding) == '放入仓库'


def test_display_normalization_only_maps_observed_actual_value(ui):
    ui.page.set_content('<div id="dispatch">48小时内发货</div>')
    a = Qianniu(ui, {}, None)
    binding = {'selector': {'css': '#dispatch'}, 'value_map': {'48小时内发货': '48小时以内'}}
    assert a._read_binding(binding) == '48小时以内'
    ui.page.locator('#dispatch').evaluate("e=>e.textContent='今日发'")
    assert a._read_binding(binding) == '今日发'


def test_custom_choice_uses_existing_visible_option_and_reads_result(ui):
    ui.page.set_content('<label>安装方式<input onclick="document.querySelector(\'button\').remove()"></label><button onclick="document.querySelector(\'input\').value=\'压接\';this.remove()">压接</button>')
    a = Qianniu(ui, {}, None)
    binding = {'selector': {'label': '安装方式'}, 'action': 'choice', 'option': {'text': ''}}
    a._write_binding(binding, '压接')
    assert a._read_binding(binding) == '压接'
