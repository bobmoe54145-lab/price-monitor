"""CLI: python -m price_monitor {init-sheets|search|run}"""
from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_env, rakuten_settings, sheets_settings
from .matching import filter_offers, split_terms
from .models import WatchItem
from .rakuten import RakutenClient, RakutenError
from .runner import SORT, run
from .sheets import SheetError, SheetsRepo


def _cmd_init_sheets(_args) -> int:
    repo = SheetsRepo.connect(sheets_settings())
    created = repo.ensure_sheets()
    print("作成したシート:", ", ".join(created) if created else "なし（すべて作成済み）")
    return 0


def _cmd_search(args) -> int:
    """master に登録する前の候補確認用。itemCode の固定に使う。"""
    client = RakutenClient(rakuten_settings())
    # run と同じく、除外は API に任せず商品名でのローカル判定にする
    offers = client.search(
        keyword=args.keyword, min_price=args.min_price, max_pages=args.pages, sort=SORT
    )
    item = WatchItem(
        item_id="preview",
        name="",
        keyword=args.keyword,
        model=args.model or "",
        ng_keywords=split_terms(args.ng or ""),
    )
    kept = filter_offers(offers, item)
    print(f"API 取得 {len(offers)} 件 → 絞り込み後 {len(kept)} 件")
    for o in sorted(kept, key=lambda o: o.price):
        stock = "販売可" if o.available else "販売不可"
        print(f"{o.price:>8,}円  {stock}  {o.shop_name}  [{o.item_code}]  {o.item_name[:40]}")
    return 0


def _cmd_run(args) -> int:
    client = RakutenClient(rakuten_settings())
    repo = SheetsRepo.connect(sheets_settings())
    report = run(client, repo, dry_run=args.dry_run)

    if args.dry_run:
        for s in report.summaries:
            print(f"{s.item_id} {s.name}: 最安 {s.min_price} ({s.min_shop}) / 自社 {s.own_price} / "
                  f"販売中 {s.available_shops}/{s.total_shops} ショップ")
        print("（dry-run: シートには書き込んでいません）")
    print(f"対象 {report.targets} / 成功 {report.succeeded} / 失敗 {len(report.failures)} / "
          f"変化イベント {report.events} / 異常値スキップ {len(report.anomalies)}")
    for line in [*report.failures, *report.anomalies]:
        print(" -", line)
    if report.fatal:
        print("致命的エラー:", report.fatal)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="price_monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-sheets", help="スプレッドシートに必要なシートを作る").set_defaults(func=_cmd_init_sheets)

    p = sub.add_parser("search", help="キーワードで検索し、候補を表示する（シートには書かない）")
    p.add_argument("keyword")
    p.add_argument("--model", help="商品名に含まれるべき型番")
    p.add_argument("--ng", help="除外キーワード（スペース区切り）")
    p.add_argument("--min-price", type=int)
    p.add_argument("--pages", type=int, default=1)
    p.set_defaults(func=_cmd_search)

    p = sub.add_parser("run", help="監視を 1 回実行する")
    p.add_argument("--dry-run", action="store_true", help="取得と集計だけ行い、シートに書き込まない")
    p.set_defaults(func=_cmd_run)

    args = parser.parse_args(argv)
    load_env()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return args.func(args)
    except (ConfigError, SheetError, RakutenError) as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
