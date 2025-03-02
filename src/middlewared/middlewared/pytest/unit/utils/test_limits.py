import json

from aiohttp.http_websocket import WSCloseCode
import pytest

from middlewared.utils import limits


def test__limit_unauthenticated_excetion():
    data = 'x' * (limits.MsgSizeLimit.UNAUTHENTICATED + 1)
    with pytest.raises(limits.MsgSizeError) as err:
        limits.parse_message(False, data)

    assert err.value.limit is limits.MsgSizeLimit.UNAUTHENTICATED
    assert err.value.datalen == len(data)
    assert err.value.ws_close_code is WSCloseCode.INVALID_TEXT
    assert err.value.ws_errmsg == 'Anonymous connection max message length is 8 kB'


def test__limit_authenticated_basic_exception():
    data = json.dumps({'msg': 'method', 'method': 'canary', 'params': ['x' * (limits.MsgSizeLimit.AUTHENTICATED + 1)]})
    with pytest.raises(limits.MsgSizeError) as err:
        limits.parse_message(True, data)

    assert err.value.limit is limits.MsgSizeLimit.AUTHENTICATED
    assert err.value.datalen == len(data)
    assert err.value.method_name == 'canary'
    assert err.value.ws_close_code is WSCloseCode.MESSAGE_TOO_BIG
    assert err.value.ws_errmsg == 'Max message length is 64 kB'


def test__limit_authenticated_extended_exception():
    data = json.dumps({'msg': 'method', 'method': 'canary', 'params': ['x' * (limits.MsgSizeLimit.EXTENDED + 1)]})
    with pytest.raises(limits.MsgSizeError) as err:
        limits.parse_message(True, data)

    assert err.value.limit is limits.MsgSizeLimit.EXTENDED
    assert err.value.datalen == len(data)
    assert err.value.ws_close_code is WSCloseCode.MESSAGE_TOO_BIG
    assert err.value.ws_errmsg == 'Max message length is 64 kB'


def test__limit_unauthenticated_parse():
    data = {'msg': 'method', 'method': 'canary', 'params': ['x' * 1000]}
    parsed = limits.parse_message(False, json.dumps(data))
    assert parsed == data


def test__limit_authenticated_parse():
    data = {'msg': 'method', 'method': 'canary', 'params': ['x' * 1000]}
    parsed = limits.parse_message(True, json.dumps(data))
    assert parsed == data


@pytest.mark.parametrize("value", [False, 0, "0"])
def test__invalid_type(value):
    with pytest.raises(ValueError) as ve:
        limits.parse_message(True, json.dumps(value))

    assert ve.value.args[0] == "Invalid Message Format"


def test__invalid_type__list():
    with pytest.raises(ValueError) as ve:
        limits.parse_message(True, json.dumps([]))

    assert ve.value.args[0] == "Batch messages are not supported at this time"
