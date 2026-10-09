from unittest.mock import Mock
import pytest

from qianniu.browser import active_port_endpoint, PlatformPause


def test_active_port_waits_for_chrome_to_release_and_finish_its_file():
    path = Mock()
    path.read_text.side_effect = [PermissionError('sharing violation'), '', '9223\n/devtools/browser/example\n']
    assert active_port_endpoint(path, timeout=1) == 'ws://127.0.0.1:9223/devtools/browser/example'
    assert path.read_text.call_count == 3


@pytest.mark.parametrize('value', ['9223\nhttp://remote', '0\n/devtools/browser/test', 'bad', '999999\n/devtools/browser/test'])
def test_active_port_invalid_or_missing_record_is_bounded_pause(value):
    path = Mock()
    path.read_text.return_value = value
    with pytest.raises(PlatformPause):
        active_port_endpoint(path, timeout=.01)
