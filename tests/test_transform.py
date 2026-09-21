from dataclasses import replace

from price_monitor.models import AVAILABLE, NOT_FOUND, SOLD_OUT, Listing, Offer, WatchItem
from price_monitor.transform import is_anomalous, reconcile, summarize

ITEM = WatchItem(item_id="i1", name="商品1", keyword="k", own_price=1000)
T0, T1 = "2026-09-20 08:00", "2026-09-20 12:00"


def offer(code="a:1", price=1000, available=True, shop="A", point=1):
    return Offer(code, f"name {code}", shop.lower(), shop, price, point, True, available, 0, 0.0, "u")


def listing(code="a:1", price=1000, status=AVAILABLE, shop="A"):
    return Listing("i1", code, shop.lower(), shop, "n", price, 1, True, status, 0, 0.0, "u", T0, T0)


def kinds(result):
    return [e.event for e in result.events]


def test_first_run_marks_everything_new():
    r = reconcile(ITEM, {}, [offer("a:1"), offer("b:1", 900, shop="B")], T0)
    assert kinds(r) == ["新規", "新規"]
    assert all(l.first_seen == T0 == l.last_seen for l in r.listings)


def test_price_drop_and_rise():
    prev = {"a:1": listing("a:1", 1000), "b:1": listing("b:1", 1000, shop="B")}
    r = reconcile(ITEM, prev, [offer("a:1", 900), offer("b:1", 1100, shop="B")], T1)
    assert kinds(r) == ["値下げ", "値上げ"]
    down = r.events[0]
    assert (down.old_price, down.new_price) == (1000, 900)
    assert r.listings[0].first_seen == T0 and r.listings[0].last_seen == T1


def test_no_change_no_event_but_last_seen_updates():
    r = reconcile(ITEM, {"a:1": listing()}, [offer()], T1)
    assert r.events == [] and r.listings[0].last_seen == T1


def test_sold_out_and_back_in_stock():
    r = reconcile(ITEM, {"a:1": listing()}, [offer(available=False)], T1)
    assert kinds(r) == ["販売不可"] and r.listings[0].status == SOLD_OUT
    r2 = reconcile(ITEM, {"a:1": r.listings[0]}, [offer()], T1)
    assert kinds(r2) == ["販売再開"]


def test_missing_offer_becomes_not_found_once_then_stays_quiet():
    r = reconcile(ITEM, {"a:1": listing()}, [], T1)
    assert kinds(r) == ["未検出"]
    assert r.listings[0].status == NOT_FOUND and r.listings[0].price == 1000
    assert r.listings[0].last_seen == T0  # 最後に見た時刻を保持
    assert reconcile(ITEM, {"a:1": r.listings[0]}, [], T1).events == []


def test_reappearing_offer_is_redetected():
    gone = replace(listing(), status=NOT_FOUND)
    r = reconcile(ITEM, {"a:1": gone}, [offer()], T1)
    assert kinds(r) == ["再検出"]


def test_anomalous_price_keeps_previous_value_and_is_reported():
    r = reconcile(ITEM, {"a:1": listing(price=1000)}, [offer(price=10)], T1)
    assert r.events == [] and len(r.anomalies) == 1
    assert r.listings[0].price == 1000 and r.listings[0].status == AVAILABLE


def test_anomaly_helper():
    assert is_anomalous(0, None) and is_anomalous(-5, 100)
    assert is_anomalous(10000, 1000) and is_anomalous(100, 1000)
    assert not is_anomalous(9999, 1000) and not is_anomalous(500, 1000) and not is_anomalous(1, None)


def test_summary_uses_only_available_listings():
    listings = [
        listing("a:1", 1200, shop="A"),
        listing("b:1", 950, shop="B"),
        listing("c:1", 500, status=SOLD_OUT, shop="C"),
        listing("d:1", 300, status=NOT_FOUND, shop="D"),
    ]
    s = summarize(ITEM, listings, T1)
    assert (s.min_price, s.min_shop) == (950, "B")
    assert (s.gap, s.gap_rate, s.own_is_lowest) == (50, 5.3, "いいえ")
    assert (s.available_shops, s.total_shops) == (2, 4)
    assert s.median_price == 950


def test_summary_counts_shops_not_listings():
    # 同じショップが同じ商品を 3 ページ出していても 1 ショップ
    listings = [
        listing("a:1", 1500, shop="A"),
        listing("a:2", 1600, shop="A"),
        listing("a:3", 1700, status=SOLD_OUT, shop="A"),
        listing("b:1", 1800, shop="B"),
    ]
    s = summarize(ITEM, listings, T1)
    assert (s.available_shops, s.total_shops) == (2, 2)
    assert s.min_price == 1500


def test_summary_when_own_is_cheapest_or_nothing_available():
    cheap = summarize(replace(ITEM, own_price=900), [listing(price=950)], T1)
    assert cheap.own_is_lowest == "はい" and cheap.gap == -50
    empty = summarize(ITEM, [listing(status=SOLD_OUT)], T1)
    assert empty.min_price is None and empty.own_is_lowest == "" and empty.available_shops == 0


def test_effective_price_subtracts_points():
    assert listing().effective_price == 990  # 倍率 1 = 1%
    assert replace(listing(price=10000), point_rate=10).effective_price == 9000
