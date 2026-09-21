"""ドメインモデル。"""
from __future__ import annotations

from dataclasses import dataclass

AVAILABLE = "販売可"
SOLD_OUT = "販売不可"
NOT_FOUND = "未検出"


@dataclass(frozen=True)
class WatchItem:
    """master シートの 1 行（監視対象の商品）。"""

    item_id: str
    name: str
    keyword: str
    model: str = ""
    ng_keywords: tuple[str, ...] = ()
    ng_shops: tuple[str, ...] = ()
    pinned_codes: tuple[str, ...] = ()
    min_price: int | None = None
    own_price: int | None = None
    enabled: bool = True


@dataclass(frozen=True)
class Offer:
    """API から取得した、あるショップの出品 1 件。"""

    item_code: str
    item_name: str
    shop_code: str
    shop_name: str
    price: int
    point_rate: int
    free_shipping: bool | None
    available: bool
    review_count: int
    review_average: float
    url: str


@dataclass(frozen=True)
class Listing:
    """latest シートの 1 行。出品の最新状態。"""

    item_id: str
    item_code: str
    shop_code: str
    shop_name: str
    item_name: str
    price: int
    point_rate: int
    free_shipping: bool | None
    status: str
    review_count: int
    review_average: float
    url: str
    first_seen: str
    last_seen: str

    @property
    def effective_price(self) -> int:
        """ポイント還元を差し引いた概算価格（ポイント倍率 1 = 1%）。"""
        return self.price - self.price * self.point_rate // 100


@dataclass(frozen=True)
class Event:
    """history シートの 1 行。価格・販売状況の変化。"""

    at: str
    item_id: str
    item_code: str
    shop_name: str
    event: str
    old_price: int | None
    new_price: int | None
    old_status: str
    new_status: str


@dataclass(frozen=True)
class Summary:
    """summary シートの 1 行。商品ごとの価格比較。"""

    item_id: str
    name: str
    own_price: int | None
    min_price: int | None
    min_shop: str
    gap: int | None  # 自社価格 - 最安値（正なら自社が高い）
    gap_rate: float | None  # 最安値に対する差の割合（%）
    own_is_lowest: str  # "はい" / "いいえ" / ""
    median_price: int | None
    min_effective_price: int | None
    available_shops: int
    total_shops: int
    updated_at: str
