"""Google スプレッドシートの読み書き。

書き込みは RAW（数式として解釈させない）で行う。商品名が "=" で始まっていても式にならない。
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Sequence

import gspread
from gspread.utils import rowcol_to_a1

from .config import SheetsSettings
from .matching import split_terms
from .models import Event, Listing, Summary, WatchItem

SHEET_MASTER = "master"
SHEET_LATEST = "latest"
SHEET_SUMMARY = "summary"
SHEET_HISTORY = "history"
SHEET_TREND = "trend"
SHEET_LOG = "log"

MASTER_HEADERS = [
    "管理ID", "商品名", "検索キーワード", "型番(必須)", "除外キーワード",
    "除外ショップ", "固定itemCode", "価格下限", "自社価格", "監視",
]
LATEST_HEADERS = [
    "管理ID", "itemCode", "ショップコード", "ショップ名", "商品名", "価格",
    "ポイント倍率", "実質価格(概算)", "送料", "販売状況", "レビュー数", "評価",
    "URL", "初回検出", "最終検出",
]
SUMMARY_HEADERS = [
    "管理ID", "商品名", "自社価格", "最安値", "最安ショップ", "自社との差額",
    "差率(%)", "自社が最安", "中央値", "最安の実質価格(概算)",
    "販売中ショップ数", "監視ショップ数", "更新日時",
]
HISTORY_HEADERS = [
    "日時", "管理ID", "itemCode", "ショップ名", "イベント",
    "旧価格", "新価格", "旧状況", "新状況",
]
TREND_HEADERS = ["日時", "管理ID", "最安値", "最安ショップ", "中央値", "販売中ショップ数"]
LOG_HEADERS = ["日時", "結果", "対象商品数", "成功", "失敗", "イベント数", "メッセージ"]

_SHEETS = {
    SHEET_MASTER: MASTER_HEADERS,
    SHEET_LATEST: LATEST_HEADERS,
    SHEET_SUMMARY: SUMMARY_HEADERS,
    SHEET_HISTORY: HISTORY_HEADERS,
    SHEET_TREND: TREND_HEADERS,
    SHEET_LOG: LOG_HEADERS,
}

_OFF_VALUES = {"off", "false", "0", "×", "x", "no", "n", "停止", "無効", "いいえ"}
_SHIPPING_LABEL = {True: "送料込", False: "送料別", None: ""}
_SHIPPING_FROM_LABEL = {v: k for k, v in _SHIPPING_LABEL.items()}


class SheetError(Exception):
    pass


def parse_int(value: Any) -> int | None:
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"[,円¥\s]", "", text)
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return int(float(text))
    return None


def parse_enabled(value: Any) -> bool:
    """空欄は ON 扱い。明示的に OFF と読める値だけ無効にする。"""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold() not in _OFF_VALUES


def _cell(value: Any) -> Any:
    return "" if value is None else value


class SheetsRepo:
    def __init__(self, spreadsheet: gspread.Spreadsheet) -> None:
        self._ss = spreadsheet

    @classmethod
    def connect(cls, settings: SheetsSettings) -> "SheetsRepo":
        if settings.credentials_info is not None:
            info = settings.credentials_info
            client = gspread.service_account_from_dict(info)
        else:
            try:
                with open(settings.credentials_file, encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                raise SheetError(
                    "GOOGLE_APPLICATION_CREDENTIALS のファイルを読めません（パスと JSON 形式を確認）"
                ) from None
            client = gspread.service_account_from_dict(info)
        email = info.get("client_email", "（不明）")
        try:
            return cls(client.open_by_key(settings.spreadsheet_id))
        except PermissionError:
            raise SheetError(
                f"スプレッドシートを開けません。{email} を「編集者」として共有しているか確認してください"
            ) from None
        except gspread.SpreadsheetNotFound:
            raise SheetError("SPREADSHEET_ID のスプレッドシートが見つかりません（ID を確認）") from None

    # ---- セットアップ ----

    def ensure_sheets(self) -> list[str]:
        """足りないシートを見出し付きで作る。既存シートには触らない。戻り値は作成したシート名。"""
        created = []
        existing = {ws.title for ws in self._ss.worksheets()}
        for name, headers in _SHEETS.items():
            if name in existing:
                continue
            ws = self._ss.add_worksheet(title=name, rows=1000, cols=max(len(headers), 10))
            ws.update(values=[headers], range_name="A1")
            ws.freeze(rows=1)
            ws.format("1:1", {"textFormat": {"bold": True}})
            if name == SHEET_MASTER:
                sample = [
                    "sample-001", "（例）商品名", "検索キーワード 型番", "型番",
                    "中古 ケース", "", "", "", "", "OFF",
                ]
                ws.append_rows([sample])
            created.append(name)
        return created

    # ---- 読み込み ----

    def _ws(self, name: str) -> gspread.Worksheet:
        try:
            return self._ss.worksheet(name)
        except gspread.WorksheetNotFound:
            raise SheetError(
                f"シート '{name}' がありません。`python -m price_monitor init-sheets` を実行してください"
            ) from None

    def _records(self, name: str, headers: Sequence[str]) -> list[dict[str, str]]:
        values = self._ws(name).get_all_values()
        if not values:
            raise SheetError(f"シート '{name}' が空です。見出し行が必要です")
        header = [h.strip() for h in values[0]]
        missing = [h for h in headers if h not in header]
        if missing:
            raise SheetError(f"シート '{name}' に見出しがありません: {', '.join(missing)}")
        index = {h: header.index(h) for h in headers}
        records = []
        for row in values[1:]:
            if not any(str(c).strip() for c in row):
                continue
            records.append(
                {h: (str(row[i]).strip() if i < len(row) else "") for h, i in index.items()}
            )
        return records

    def read_watch_items(self) -> list[WatchItem]:
        items, seen = [], set()
        for n, rec in enumerate(self._records(SHEET_MASTER, MASTER_HEADERS), start=2):
            item_id = rec["管理ID"]
            if not item_id:
                raise SheetError(f"master {n} 行目: 管理ID が空です")
            if item_id in seen:
                raise SheetError(f"master {n} 行目: 管理ID '{item_id}' が重複しています")
            seen.add(item_id)
            items.append(
                WatchItem(
                    item_id=item_id,
                    name=rec["商品名"],
                    keyword=rec["検索キーワード"],
                    model=rec["型番(必須)"],
                    ng_keywords=split_terms(rec["除外キーワード"]),
                    ng_shops=split_terms(rec["除外ショップ"]),
                    pinned_codes=split_terms(rec["固定itemCode"]),
                    min_price=parse_int(rec["価格下限"]),
                    own_price=parse_int(rec["自社価格"]),
                    enabled=parse_enabled(rec["監視"]),
                )
            )
        return items

    def read_latest(self) -> list[Listing]:
        out = []
        for rec in self._records(SHEET_LATEST, LATEST_HEADERS):
            out.append(
                Listing(
                    item_id=rec["管理ID"],
                    item_code=rec["itemCode"],
                    shop_code=rec["ショップコード"],
                    shop_name=rec["ショップ名"],
                    item_name=rec["商品名"],
                    price=parse_int(rec["価格"]) or 0,
                    point_rate=parse_int(rec["ポイント倍率"]) or 1,
                    free_shipping=_SHIPPING_FROM_LABEL.get(rec["送料"]),
                    status=rec["販売状況"],
                    review_count=parse_int(rec["レビュー数"]) or 0,
                    review_average=float(rec["評価"] or 0),
                    url=rec["URL"],
                    first_seen=rec["初回検出"],
                    last_seen=rec["最終検出"],
                )
            )
        return out

    # ---- 書き込み ----

    def write_latest(self, listings: Sequence[Listing]) -> None:
        rows = [
            [
                l.item_id, l.item_code, l.shop_code, l.shop_name, l.item_name, l.price,
                l.point_rate, l.effective_price, _SHIPPING_LABEL[l.free_shipping], l.status,
                l.review_count, l.review_average, l.url, l.first_seen, l.last_seen,
            ]
            for l in listings
        ]
        self._replace_table(SHEET_LATEST, LATEST_HEADERS, rows)

    def write_summary(self, summaries: Sequence[Summary]) -> None:
        rows = [
            [
                s.item_id, s.name, s.own_price, s.min_price, s.min_shop, s.gap, s.gap_rate,
                s.own_is_lowest, s.median_price, s.min_effective_price,
                s.available_shops, s.total_shops, s.updated_at,
            ]
            for s in summaries
        ]
        self._replace_table(SHEET_SUMMARY, SUMMARY_HEADERS, rows)

    def append_history(self, events: Sequence[Event]) -> None:
        self._append(
            SHEET_HISTORY,
            [
                [e.at, e.item_id, e.item_code, e.shop_name, e.event,
                 e.old_price, e.new_price, e.old_status, e.new_status]
                for e in events
            ],
        )

    def append_trend(self, summaries: Sequence[Summary]) -> None:
        self._append(
            SHEET_TREND,
            [
                [s.updated_at, s.item_id, s.min_price, s.min_shop, s.median_price, s.available_shops]
                for s in summaries
            ],
        )

    def append_log(self, row: Sequence[Any]) -> None:
        self._append(SHEET_LOG, [list(row)])

    def _append(self, name: str, rows: list[list[Any]]) -> None:
        if rows:
            # append_rows / update は既定で RAW（数式として解釈しない）
            self._ws(name).append_rows([[_cell(c) for c in r] for r in rows])

    def _replace_table(self, name: str, headers: list[str], rows: list[list[Any]]) -> None:
        """見出し＋全行を上書きする。書き込み成功後に余った旧データだけを消す（途中失敗で全消しにならない）。"""
        ws = self._ws(name)
        values = [headers, *[[_cell(c) for c in r] for r in rows]]
        if ws.row_count < len(values):
            ws.add_rows(len(values) - ws.row_count + 100)
        if ws.col_count < len(headers):
            ws.add_cols(len(headers) - ws.col_count)
        old_rows = len(ws.col_values(1))
        ws.update(values=values, range_name="A1")
        if old_rows > len(values):
            ws.batch_clear([f"A{len(values) + 1}:{rowcol_to_a1(old_rows, len(headers))}"])
