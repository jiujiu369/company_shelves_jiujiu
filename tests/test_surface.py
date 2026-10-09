from test_browser import browser


def test_surface_exposes_control_regions_without_raw_html(browser):
    browser.page.set_content('<section class="field-title"><span>宝贝标题</span><input placeholder="标题"></section>'
                             '<img alt="主图01.png" data-origin="https://example.com/img.png?token=private">')
    snapshot = browser.snapshot()
    controls = snapshot['frames'][0]['dom']['controls']
    field = next(c for c in controls if c['tag'] == 'INPUT')
    assert any(a['class'] == 'field-title' and '宝贝标题' in a['text'] for a in field['ancestors'])
    image = next(c for c in controls if c['tag'] == 'IMG')
    assert image['attributes']['data-origin'].endswith('token=%5Bredacted%5D')
    assert all('outer' not in c for c in controls)
