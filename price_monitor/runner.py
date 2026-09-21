"""1 回分の監視処理: master 読込 → 取得 → 絞り込み → 差分検出 → 書き込み。"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .matching import filter_offers
from .models import AVAILABLE, Event, Listing, Summary, WatchItem
from .rakuten import RakutenAuthError, RakutenClient, RakutenError
from .sheets import SheetsRepo
from .transform import reconcile, summarize

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
MAX_PAGES = 3  # 1 ページ 30 件。3 ページ（90 件）まで見る
# 関連度順だと、ショップ数の多い商品で上位 N 件の顔ぶれが実行ごとに入れ替わり、
# 偽の「未検出/再検出」や最安値のぶれが出る。価格の安い順なら安定し、監視目的にも合う
SORT = "+itemPrice"


@dataclass
class RunReport:
    targets: int = 0
    succeeded: int = 0
    failures: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    events: int = 0
    fatal: str | None = None
    summaries: list[Summary] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and not self.fatal


def fetch_offers(client: RakutenClient, item: WatchItem):
    if item.pinned_codes:
        offers = []
        for code in item.pinned_codes:
            offers.extend(client.search(item_code=code, max_pages=1))
        return offers
    if not item.keyword:
        raise ValueError("検索キーワードも固定itemCodeも空です")
    # 除外キーワードは API（NGKeyword）に送らない。API は説明文まで見て除外するため、
    # 単品の出品まで消えてしまう。商品名だけを見るローカル判定（filter_offers）で除外する
    return client.search(
        keyword=item.keyword,
        min_price=item.min_price,
        max_pages=MAX_PAGES,
        sort=SORT,
    )


def _sort_key(l: Listing):
    return (l.item_id, l.status != AVAILABLE, l.price, l.shop_name)


def run(client: RakutenClient, repo: SheetsRepo, *, dry_run: bool = False) -> RunReport:
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    report = RunReport()
    items = repo.read_watch_items()
    previous: dict[str, dict[str, Listing]] = defaultdict(dict)
    for l in repo.read_latest():
        previous[l.item_id][l.item_code] = l

    listings: list[Listing] = []
    events: list[Event] = []
    fresh: list[Summary] = []  # 今回正常に取得できた商品のサマリー（trend に追記する）
    summaries: list[Summary] = []

    for item in items:
        prev = previous.get(item.item_id, {})
        if not item.enabled:
            listings.extend(prev.values())  # 監視 OFF の間は状態を凍結
            continue
        report.targets += 1
        try:
            offers = filter_offers(fetch_offers(client, item), item)
        except RakutenAuthError as exc:
            report.fatal = str(exc)
            break
        except (RakutenError, ValueError) as exc:
            log.error("%s: 取得失敗: %s", item.item_id, exc)
            report.failures.append(f"{item.item_id}: {exc}")
            carried = list(prev.values())
            listings.extend(carried)
            summaries.append(
                summarize(item, carried, max((l.last_seen for l in carried), default=""))
            )
            continue

        result = reconcile(item, prev, offers, now)
        report.succeeded += 1
        report.anomalies.extend(result.anomalies)
        listings.extend(result.listings)
        events.extend(result.events)
        summary = summarize(item, result.listings, now)
        summaries.append(summary)
        fresh.append(summary)
        log.info(
            "%s: 出品 %d 件 / 最安 %s / 変化 %d 件",
            item.item_id, len(result.listings), summary.min_price, len(result.events),
        )

    report.events = len(events)
    report.summaries = summaries
    listings.sort(key=_sort_key)

    if not dry_run:
        _persist(repo, report, now, listings, events, summaries, fresh)
    return report


def _persist(repo, report, now, listings, events, summaries, fresh) -> None:
    if report.fatal:
        # 認証エラー時は何も更新しない（既存データを壊さない）。ログだけ残す
        _write_log(repo, report, now)
        return
    # 履歴 → 最新 の順。途中で失敗しても変化イベントが欠けるより重複する方が扱いやすい
    repo.append_history(events)
    repo.write_latest(listings)
    repo.write_summary(summaries)
    repo.append_trend(fresh)
    _write_log(repo, report, now)


def _write_log(repo, report: RunReport, now: str) -> None:
    message = report.fatal or "; ".join([*report.failures, *report.anomalies])[:500]
    try:
        repo.append_log(
            [now, "OK" if report.ok else "ERROR", report.targets, report.succeeded,
             len(report.failures), report.events, message]
        )
    except Exception:  # ログ書き込みの失敗で本処理の結果を隠さない
        log.exception("log シートへの書き込みに失敗しました")
