"""楽天市場商品検索 API のクライアント。

仕様: https://webservice.rakuten.co.jp/documentation/ichiba-item-search
（2026 年の改定で applicationId + accessKey が必須、旧 app.rakuten.co.jp は廃止方向）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterator

import requests

from .config import RakutenSettings
from .models import Offer

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search"
RETRY_STATUS = {429, 500, 502, 503, 504}
TAX_RATE = 1.10  # taxFlag=1（税抜表示）の商品を税込に換算する率


class RakutenError(Exception):
    pass


class RakutenAuthError(RakutenError):
    """認証・許可設定の誤り。リトライしても直らないので実行全体を止める。"""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iter_items(payload: dict) -> Iterator[dict]:
    """formatVersion 1（入れ子）でも 2（フラット）でも読めるようにする。"""
    raw = payload.get("items")
    if raw is None:
        raw = payload.get("Items") or []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        yield entry.get("item") or entry.get("Item") or entry


def parse_offers(payload: dict) -> list[Offer]:
    offers: dict[str, Offer] = {}
    for item in _iter_items(payload):
        code = str(item.get("itemCode") or "")
        if not code:
            continue
        price = _int(item.get("itemPrice"))
        if _int(item.get("taxFlag")) == 1:
            price = int(price * TAX_RATE)
        postage = item.get("postageFlag")
        offers[code] = Offer(
            item_code=code,
            item_name=str(item.get("itemName") or ""),
            shop_code=str(item.get("shopCode") or ""),
            shop_name=str(item.get("shopName") or ""),
            price=price,
            point_rate=_int(item.get("pointRate"), 1),
            free_shipping=None if postage is None else _int(postage) == 0,
            available=_int(item.get("availability")) == 1,
            review_count=_int(item.get("reviewCount")),
            review_average=_float(item.get("reviewAverage")),
            url=str(item.get("itemUrl") or ""),
        )
    return list(offers.values())


class RakutenClient:
    def __init__(
        self,
        settings: RakutenSettings,
        *,
        session: requests.Session | None = None,
        min_interval: float = 1.1,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._session = session or requests.Session()
        self._min_interval = min_interval
        self._max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    @property
    def _url(self) -> str:
        return f"{BASE_URL}/{self._settings.api_version}"

    def search(
        self,
        *,
        keyword: str | None = None,
        item_code: str | None = None,
        ng_keyword: str | None = None,
        min_price: int | None = None,
        max_pages: int = 2,
        sort: str = "standard",
    ) -> list[Offer]:
        if not keyword and not item_code:
            raise ValueError("keyword か item_code のどちらかが必要です")
        if keyword:
            short = [t for t in keyword.split() if len(t) < 2]
            if short:
                # API の仕様: キーワードの各語は 2 文字以上（違反すると 400 になる）
                raise ValueError(
                    f"検索キーワードの各語は2文字以上にしてください（1文字の語: {', '.join(short)}）"
                )
        params: dict[str, Any] = {
            "format": "json",
            "formatVersion": 2,
            "hits": 30,
            # 販売不可の出品も取得し、在庫状態の変化を追えるようにする
            "availability": 0,
            "sort": sort,
        }
        if item_code:
            params["itemCode"] = item_code
        else:
            params["keyword"] = keyword
        if ng_keyword:
            params["NGKeyword"] = ng_keyword
        if min_price:
            params["minPrice"] = min_price

        found: dict[str, Offer] = {}
        for page in range(1, max_pages + 1):
            payload = self._get({**params, "page": page})
            if payload is None:
                break
            for offer in parse_offers(payload):
                found.setdefault(offer.item_code, offer)
            if page >= _int(payload.get("pageCount"), 1):
                break
        return list(found.values())

    def _throttle(self) -> None:
        if self._last_request is not None:
            wait = self._min_interval - (self._clock() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = self._clock()

    def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(2**attempt, 30)
        if retry_after and retry_after.isdigit():
            delay = max(delay, min(int(retry_after), 60))
        log.warning("リトライ待機 %s 秒 (試行 %d)", delay, attempt + 1)
        self._sleep(delay)

    def _get(self, params: dict[str, Any]) -> dict | None:
        """1 リクエスト。該当なしは None。認証情報を含む URL を例外・ログに出さない。"""
        s = self._settings
        query = {**params, "applicationId": s.app_id, "accessKey": s.access_key}
        headers = _origin_headers(s.origin)

        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                resp = self._session.get(self._url, params=query, headers=headers, timeout=30)
            except requests.RequestException as exc:
                # str(exc) には accessKey 付きの URL が入りうるため型名だけ出す
                if attempt >= self._max_retries:
                    raise RakutenError(f"通信エラー: {type(exc).__name__}") from None
                self._backoff(attempt)
                continue

            status = resp.status_code
            if status == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise RakutenError("API の応答が JSON ではありません") from None
            if status in (401, 403):
                raise RakutenAuthError(
                    f"HTTP {status}: {_error_text(resp)} "
                    "（アプリ ID・アクセスキー・許可サイト/Origin・IP 制限を確認してください）"
                )
            if status == 404 and _error_code(resp) == "not_found":
                return None
            if status in RETRY_STATUS and attempt < self._max_retries:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            raise RakutenError(f"HTTP {status}: {_error_text(resp)}")
        raise RakutenError("リトライ上限に達しました")  # pragma: no cover


def _origin_headers(origin: str | None) -> dict[str, str]:
    """登録した許可サイトを Origin と Referer の両方で送る（Referer 欠落は 403 になる）。"""
    if not origin:
        return {}
    origin = origin.strip().rstrip("/")
    if "://" not in origin:
        origin = "https://" + origin  # Origin/Referer は URL 形式。ドメインだけ登録された場合に補う
    return {"Origin": origin, "Referer": origin + "/"}


def _error_code(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", ""))
    except (ValueError, AttributeError):
        return ""


def _error_text(resp: requests.Response) -> str:
    return (resp.text or "").strip().replace("\n", " ")[:200]
