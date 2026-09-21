"""前回の状態と今回の取得結果を突き合わせ、変化の検出と価格比較を行う。"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, replace

from .models import (
    AVAILABLE,
    NOT_FOUND,
    SOLD_OUT,
    Event,
    Listing,
    Offer,
    Summary,
    WatchItem,
)

# 前回比でこの倍率以上/以下になった価格は誤データとみなして採用しない
ANOMALY_FACTOR = 10


@dataclass(frozen=True)
class Reconciled:
    listings: list[Listing]
    events: list[Event]
    anomalies: list[str]


def is_anomalous(price: int, prev_price: int | None) -> bool:
    if price <= 0:
        return True
    if prev_price and prev_price > 0:
        return price >= prev_price * ANOMALY_FACTOR or price * ANOMALY_FACTOR <= prev_price
    return False


def reconcile(
    item: WatchItem,
    previous: dict[str, Listing],
    offers: list[Offer],
    now: str,
) -> Reconciled:
    listings: list[Listing] = []
    events: list[Event] = []
    anomalies: list[str] = []
    seen: set[str] = set()

    def emit(code, shop, event, old_price, new_price, old_status, new_status):
        events.append(
            Event(now, item.item_id, code, shop, event, old_price, new_price, old_status, new_status)
        )

    for offer in offers:
        if offer.item_code in seen:
            continue
        seen.add(offer.item_code)
        prev = previous.get(offer.item_code)

        if is_anomalous(offer.price, prev.price if prev else None):
            anomalies.append(
                f"{item.item_id} {offer.shop_name} {offer.item_code}: "
                f"異常な価格 {offer.price}（前回 {prev.price if prev else '-'}）のため採用せず"
            )
            if prev:
                listings.append(prev)  # 前回の値を維持（未検出扱いにもしない）
            continue

        status = AVAILABLE if offer.available else SOLD_OUT
        listings.append(
            Listing(
                item_id=item.item_id,
                item_code=offer.item_code,
                shop_code=offer.shop_code,
                shop_name=offer.shop_name,
                item_name=offer.item_name,
                price=offer.price,
                point_rate=offer.point_rate,
                free_shipping=offer.free_shipping,
                status=status,
                review_count=offer.review_count,
                review_average=offer.review_average,
                url=offer.url,
                first_seen=prev.first_seen if prev else now,
                last_seen=now,
            )
        )

        if prev is None:
            emit(offer.item_code, offer.shop_name, "新規", None, offer.price, "", status)
            continue
        if offer.price != prev.price:
            kind = "値下げ" if offer.price < prev.price else "値上げ"
            emit(offer.item_code, offer.shop_name, kind, prev.price, offer.price, prev.status, status)
        if status != prev.status:
            if prev.status == NOT_FOUND:
                kind = "再検出"
            else:
                kind = "販売再開" if status == AVAILABLE else "販売不可"
            emit(offer.item_code, offer.shop_name, kind, prev.price, offer.price, prev.status, status)

    for code, prev in previous.items():
        if code in seen:
            continue
        if prev.status == NOT_FOUND:
            listings.append(prev)
            continue
        listings.append(replace(prev, status=NOT_FOUND))
        emit(code, prev.shop_name, "未検出", prev.price, prev.price, prev.status, NOT_FOUND)

    return Reconciled(listings, events, anomalies)


def summarize(item: WatchItem, listings: list[Listing], updated_at: str) -> Summary:
    available = [l for l in listings if l.status == AVAILABLE]
    cheapest = min(available, key=lambda l: (l.price, l.shop_name), default=None)

    gap = gap_rate = None
    own_is_lowest = ""
    if cheapest and item.own_price:
        gap = item.own_price - cheapest.price
        gap_rate = round(gap / cheapest.price * 100, 1)
        own_is_lowest = "はい" if item.own_price <= cheapest.price else "いいえ"

    return Summary(
        item_id=item.item_id,
        name=item.name,
        own_price=item.own_price,
        min_price=cheapest.price if cheapest else None,
        min_shop=cheapest.shop_name if cheapest else "",
        gap=gap,
        gap_rate=gap_rate,
        own_is_lowest=own_is_lowest,
        median_price=statistics.median_low(l.price for l in available) if available else None,
        min_effective_price=min((l.effective_price for l in available), default=None),
        # 同じショップが同じ商品を複数ページで出すことがあるため、出品数ではなくショップ数で数える
        available_shops=len({l.shop_code for l in available}),
        total_shops=len({l.shop_code for l in listings}),
        updated_at=updated_at,
    )
