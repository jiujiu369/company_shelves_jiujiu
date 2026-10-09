import base64
from io import BytesIO

from PIL import Image

from test_platform import ui
from qianniu.data import Product
from qianniu.platform import Qianniu
from qianniu.browser import PlatformPause
import pytest


def uri(image):
    buf = BytesIO()
    image.save(buf, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()


def test_preview_audit_detects_actual_wrong_order_and_never_claims_filename(ui, tmp_path, product_data):
    product_data['media']['root'] = str(tmp_path)
    product_data['media']['detail'] = ['a.png', 'b.png']
    images = [Image.new('RGB', (12, 12), c) for c in ('red', 'blue')]
    for name, image in zip(product_data['media']['detail'], images):
        image.save(tmp_path / name)
    ui.page.set_content('<span>test shop</span>' + ''.join(f'<img src="{uri(im)}">' for im in reversed(images)))
    a = Qianniu(ui, {'detail_preview': {'frame_url': '/publish', 'images': 'img'}}, None)
    result = a.audit_detail_preview(Product(product_data))
    assert result['actual_names'] == ['b.png', 'a.png'] and not result['content_order_ok']
    assert not result['platform_filenames_verified'] and not result['ok']


def test_preview_audit_does_not_accept_wrong_pixels_with_same_dimensions(ui, tmp_path, product_data):
    product_data['media']['root'] = str(tmp_path)
    Image.new('RGB', (12, 12), 'red').save(tmp_path / 'detail.png')
    ui.page.set_content('<span>test shop</span>' + f'<img src="{uri(Image.new("RGB", (12, 12), "blue"))}">')
    a = Qianniu(ui, {'detail_preview': {'frame_url': '/publish', 'images': 'img'}}, None)
    result = a.audit_detail_preview(Product(product_data))
    assert result['groups'][0]['matches'] == [] and not result['content_order_ok']


def test_lossy_preview_candidates_are_advisory_not_acceptance(ui, tmp_path, product_data):
    product_data['media']['root'] = str(tmp_path)
    original = Image.new('RGB', (12, 12))
    original.putdata([(x * 17, y * 17, (x+y) * 8) for y in range(12) for x in range(12)])
    original.save(tmp_path / 'detail.png')
    buf = BytesIO()
    original.save(buf, format='JPEG', quality=75)
    src = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()
    ui.page.set_content('<span>test shop</span>' + f'<img src="{src}">')
    a = Qianniu(ui, {'detail_preview': {'frame_url': '/publish', 'images': 'img'}}, None)
    result = a.audit_detail_preview(Product(product_data))
    group = result['groups'][0]
    assert group['segments'][0]['format'] == 'JPEG'
    assert group['similarity_candidates'][0]['name'] == 'detail.png'
    assert group['similarity_candidates'][0]['mae'] > 0
    assert not result['ok'] and not result['content_order_ok']


def test_unresolved_preview_difference_stops_further_form_writes(ui, tmp_path, product_data):
    product_data['media']['root'] = str(tmp_path)
    Image.new('RGB', (12, 12), 'red').save(tmp_path / 'detail.png')
    ui.page.set_content('<span>test shop</span><label>宝贝标题<input></label>'
                        + f'<img src="{uri(Image.new("RGB", (12, 12), "blue"))}">')
    a = Qianniu(ui, {'detail_preview': {'frame_url': '/publish', 'images': 'img'},
                    'fields': {'fields.title': {'selector': {'label': '宝贝标题'}}}}, None)
    with pytest.raises(PlatformPause):
        a.fill_known(Product(product_data), ['fields.title'])
    assert ui.page.get_by_label('宝贝标题').input_value() == ''
