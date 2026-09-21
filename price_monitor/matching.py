"""検索結果から「監視対象と同じ商品」だけを残す。"""
from __future__ import annotations

import re
import unicodedata

from .models import Offer, WatchItem

_SPLIT = re.compile(r"[,、，\s]+")
_NOISE = re.compile(r"[\s\-‐‑‒–—―−]+")


def split_terms(value: str) -> tuple[str, ...]:
    return tuple(t for t in _SPLIT.split(value.strip()) if t)


def squash(text: str) -> str:
    """全角/半角・大小文字・空白・ハイフンの違いを吸収する（型番の表記ゆれ対策）。"""
    return _NOISE.sub("", unicodedata.normalize("NFKC", text).casefold())


def filter_offers(offers: list[Offer], item: WatchItem) -> list[Offer]:
    if item.pinned_codes:
        # 人が確定した itemCode は、他の絞り込みより優先する
        pinned = {c.casefold() for c in item.pinned_codes}
        return [o for o in offers if o.item_code.casefold() in pinned]

    # 必須語はスペース/カンマ区切りで複数指定でき、すべて商品名に含まれるものだけ残す
    required = [squash(t) for t in split_terms(item.model)]
    ng_words = [squash(w) for w in item.ng_keywords]
    ng_shops = {s.casefold() for s in item.ng_shops}

    kept = []
    for offer in offers:
        name = squash(offer.item_name)
        if not all(t in name for t in required):
            continue
        if any(w and w in name for w in ng_words):
            continue
        if offer.shop_code.casefold() in ng_shops:
            continue
        kept.append(offer)
    return kept
