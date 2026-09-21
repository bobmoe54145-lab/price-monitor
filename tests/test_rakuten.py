import pytest
import requests

from price_monitor.config import RakutenSettings
from price_monitor.rakuten import (
    RakutenAuthError,
    RakutenClient,
    RakutenError,
    parse_offers,
)

SETTINGS = RakutenSettings("APPID", "SECRETKEY", "https://example.github.io", "20260701")

FLAT = {
    "count": 2, "page": 1, "pageCount": 1,
    "items": [
        {"itemName": "ブランドX ABC-123 本体", "itemCode": "shopa:1", "itemPrice": 9800,
         "availability": 1, "taxFlag": 0, "postageFlag": 0, "pointRate": 2,
         "reviewCount": 10, "reviewAverage": "4.5", "shopName": "ショップA",
         "shopCode": "shopa", "itemUrl": "https://item.rakuten.co.jp/shopa/1/"},
        {"itemName": "ブランドX ABC-123 税抜表示", "itemCode": "shopb:2", "itemPrice": 10000,
         "availability": 0, "taxFlag": 1, "postageFlag": 1, "shopName": "ショップB",
         "shopCode": "shopb", "itemUrl": "u"},
    ],
}
NESTED = {"Items": [{"Item": FLAT["items"][0]}]}


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text or (str(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params, "headers": headers})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses, **kw):
    sleeps = []
    session = FakeSession(responses)
    client = RakutenClient(SETTINGS, session=session, sleep=sleeps.append, **kw)
    return client, session, sleeps


def test_parse_flat_format():
    a, b = parse_offers(FLAT)
    assert (a.item_code, a.price, a.available, a.free_shipping, a.point_rate) == ("shopa:1", 9800, True, True, 2)
    assert a.review_average == 4.5
    # 税抜表示は 10% 上乗せして税込に換算、販売不可、送料別、倍率省略は 1
    assert (b.price, b.available, b.free_shipping, b.point_rate) == (11000, False, False, 1)


def test_parse_nested_format():
    (a,) = parse_offers(NESTED)
    assert a.item_code == "shopa:1"


def test_search_sends_credentials_origin_and_all_availability():
    client, session, _ = make_client([FakeResponse(200, FLAT)])
    client.search(keyword="ABC-123", ng_keyword="中古", min_price=1000)
    call = session.calls[0]
    assert call["url"].endswith("/IchibaItem/Search/20260701")
    assert call["headers"] == {
        "Origin": "https://example.github.io",
        "Referer": "https://example.github.io/",
    }
    p = call["params"]
    assert (p["applicationId"], p["accessKey"]) == ("APPID", "SECRETKEY")
    assert (p["keyword"], p["NGKeyword"], p["minPrice"], p["availability"]) == ("ABC-123", "中古", 1000, 0)


def test_origin_without_scheme_gets_https():
    from price_monitor.rakuten import _origin_headers

    assert _origin_headers("example.github.io/") == {
        "Origin": "https://example.github.io",
        "Referer": "https://example.github.io/",
    }
    assert _origin_headers(None) == {}


def test_search_requires_keyword_or_item_code():
    client, _, _ = make_client([])
    with pytest.raises(ValueError):
        client.search()


def test_pagination_stops_at_page_count_and_dedups():
    page1 = {"pageCount": 2, "items": FLAT["items"][:1]}
    page2 = {"pageCount": 2, "items": FLAT["items"]}  # 1 件目は重複
    client, session, _ = make_client([FakeResponse(200, page1), FakeResponse(200, page2)])
    offers = client.search(keyword="x", max_pages=5)
    assert len(session.calls) == 2
    assert [o.item_code for o in offers] == ["shopa:1", "shopb:2"]


def test_not_found_404_returns_empty():
    client, _, _ = make_client([FakeResponse(404, {"error": "not_found"})])
    assert client.search(keyword="x") == []


def test_unexpected_404_is_error_not_empty():
    # エンドポイント廃止などの 404 を「該当なし」と取り違えない
    client, _, _ = make_client([FakeResponse(404, None, text="Not Found")])
    with pytest.raises(RakutenError):
        client.search(keyword="x")


def test_retries_429_then_succeeds():
    client, session, sleeps = make_client(
        [FakeResponse(429, {"error": "too_many_requests"}, headers={"Retry-After": "3"}), FakeResponse(200, FLAT)]
    )
    assert len(client.search(keyword="x")) == 2
    assert len(session.calls) == 2
    assert 3 in sleeps


def test_gives_up_after_max_retries():
    client, session, _ = make_client([FakeResponse(503, None, text="down")] * 3, max_retries=2)
    with pytest.raises(RakutenError, match="503"):
        client.search(keyword="x")
    assert len(session.calls) == 3


def test_403_is_auth_error_without_retry():
    client, session, _ = make_client([FakeResponse(403, {"errorMessage": "CLIENT_IP_NOT_ALLOWED"})])
    with pytest.raises(RakutenAuthError):
        client.search(keyword="x")
    assert len(session.calls) == 1


def test_network_error_message_does_not_leak_access_key():
    leaky = requests.ConnectionError("HTTPSConnectionPool ... /Search/20260701?accessKey=SECRETKEY&applicationId=APPID")
    client, _, _ = make_client([leaky] * 2, max_retries=1)
    with pytest.raises(RakutenError) as info:
        client.search(keyword="x")
    assert "SECRETKEY" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


def test_throttles_between_requests():
    # 時計の呼び出し順: 1 回目の記録(0.0) → 2 回目の経過確認(0.2) → 2 回目の記録(0.2)
    ticks = iter([0.0, 0.2, 0.2])
    sleeps = []
    client = RakutenClient(
        SETTINGS, session=FakeSession([FakeResponse(200, {"pageCount": 2, "items": []})] * 2),
        sleep=sleeps.append, clock=lambda: next(ticks),
    )
    client.search(keyword="x", max_pages=2)
    assert sleeps and sleeps[0] == pytest.approx(0.9)
