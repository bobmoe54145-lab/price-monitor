import pytest

from price_monitor import runner
from price_monitor.config import RakutenSettings
from price_monitor.rakuten import RakutenClient
from price_monitor.sheets import (
    LATEST_HEADERS,
    MASTER_HEADERS,
    SheetError,
    SheetsRepo,
    parse_enabled,
    parse_int,
)
from tests.test_rakuten import FLAT, FakeResponse, FakeSession


def test_parse_int_variants():
    assert parse_int("1,980円") == 1980
    assert parse_int("￥２，５００") == 2500
    assert parse_int("") is None and parse_int("abc") is None


def test_parse_enabled_blank_is_on():
    assert parse_enabled("") and parse_enabled("ON") and parse_enabled("はい")
    assert not parse_enabled("off") and not parse_enabled("OFF") and not parse_enabled("×")


# ---- 最小限のスプレッドシート偽物 ----

class FakeWS:
    def __init__(self, title, values):
        self.title, self.values = title, values
        self.appended, self.updated, self.cleared = [], None, []
        self.row_count, self.col_count = 1000, 20

    def get_all_values(self):
        return self.values

    def col_values(self, _n):
        return [r[0] for r in self.values if r]

    def update(self, values, range_name=None, **_):
        self.updated = (range_name, values)
        self.values = [list(map(str, r)) for r in values]

    def batch_clear(self, ranges):
        self.cleared += ranges

    def append_rows(self, rows, **_):
        self.appended += rows
        self.values += [list(map(str, r)) for r in rows]


class FakeSS:
    def __init__(self, sheets):
        self.sheets = sheets

    def worksheet(self, name):
        import gspread

        if name not in self.sheets:
            raise gspread.WorksheetNotFound(name)
        return self.sheets[name]


def master_row(**kw):
    base = dict(zip(MASTER_HEADERS, ["p1", "商品1", "キーワード", "ABC-123", "中古 ケース", "own", "", "500", "¥1,000", ""]))
    base.update(kw)
    return [base[h] for h in MASTER_HEADERS]


def make_repo(master_rows, latest_rows=()):
    return SheetsRepo(FakeSS({
        "master": FakeWS("master", [MASTER_HEADERS, *master_rows]),
        "latest": FakeWS("latest", [LATEST_HEADERS, *latest_rows]),
        "summary": FakeWS("summary", [["h"]]),
        "history": FakeWS("history", [["h"]]),
        "trend": FakeWS("trend", [["h"]]),
        "log": FakeWS("log", [["h"]]),
    }))


def test_read_watch_items_parses_columns():
    (item,) = make_repo([master_row()]).read_watch_items()
    assert item.item_id == "p1" and item.model == "ABC-123"
    assert item.ng_keywords == ("中古", "ケース") and item.ng_shops == ("own",)
    assert (item.min_price, item.own_price, item.enabled) == (500, 1000, True)


def test_duplicate_id_and_missing_header_are_rejected():
    with pytest.raises(SheetError, match="重複"):
        make_repo([master_row(), master_row()]).read_watch_items()
    repo = SheetsRepo(FakeSS({"master": FakeWS("master", [["管理ID"], ["p1"]])}))
    with pytest.raises(SheetError, match="見出し"):
        repo.read_watch_items()


def test_missing_sheet_points_to_init_command():
    with pytest.raises(SheetError, match="init-sheets"):
        SheetsRepo(FakeSS({})).read_watch_items()


def make_client(responses):
    settings = RakutenSettings("A", "K", None, "20260701")
    return RakutenClient(settings, session=FakeSession(responses), sleep=lambda _: None)


def test_full_run_writes_all_sheets_and_second_run_detects_change():
    repo = make_repo([master_row()])
    # FLAT の商品名は "ABC-123" を含む。2 件目は税抜換算 11,000 円・販売不可
    report = runner.run(make_client([FakeResponse(200, FLAT)]), repo)
    assert report.ok and report.targets == 1 and report.events == 2  # 新規 x2

    ss = repo._ss.sheets
    latest_rows = ss["latest"].values[1:]
    assert len(latest_rows) == 2
    assert latest_rows[0][3] == "ショップA"  # 販売可が先頭
    assert [r[0] for r in ss["history"].appended] == [runner.datetime.now(runner.JST).strftime("%Y-%m-%d %H:%M")] * 2
    summary = ss["summary"].values[1]
    assert summary[3] == "9800" and summary[7] == "はい"  # 自社 1000 < 最安 9800
    assert ss["log"].appended[0][1] == "OK"

    # 2 回目: ショップ A が値下げ
    changed = {"pageCount": 1, "items": [dict(FLAT["items"][0], itemPrice=9000), FLAT["items"][1]]}
    report2 = runner.run(make_client([FakeResponse(200, changed)]), repo)
    assert report2.events == 1
    assert ss["history"].appended[-1][4] == "値下げ"


def test_run_searches_by_price_ascending_without_api_side_exclusion():
    session = FakeSession([FakeResponse(200, FLAT)])
    settings = RakutenSettings("A", "K", None, "20260701")
    client = RakutenClient(settings, session=session, sleep=lambda _: None)
    runner.run(client, make_repo([master_row()]), dry_run=True)
    params = session.calls[0]["params"]
    assert params["sort"] == "+itemPrice"
    assert "NGKeyword" not in params  # 除外は商品名でのローカル判定のみ


def test_dry_run_writes_nothing():
    repo = make_repo([master_row()])
    report = runner.run(make_client([FakeResponse(200, FLAT)]), repo, dry_run=True)
    assert report.ok and report.summaries
    ss = repo._ss.sheets
    assert ss["latest"].updated is None and ss["log"].appended == [] and ss["history"].appended == []


def test_auth_error_aborts_without_touching_data():
    repo = make_repo([master_row(), master_row(管理ID="p2")])
    client = make_client([FakeResponse(403, {"errorMessage": "CLIENT_IP_NOT_ALLOWED"})])
    report = runner.run(client, repo)
    ss = repo._ss.sheets
    assert report.fatal and not report.ok
    assert ss["latest"].updated is None  # 既存データを壊さない
    assert ss["log"].appended[0][1] == "ERROR"


def test_one_item_failure_does_not_stop_others():
    repo = make_repo([master_row(), master_row(管理ID="p2")])
    client = make_client([FakeResponse(400, None, text="bad"), FakeResponse(200, FLAT)])
    report = runner.run(client, repo)
    assert report.succeeded == 1 and len(report.failures) == 1 and not report.ok
    assert len(repo._ss.sheets["latest"].values) == 3  # 見出し + p2 の 2 行


def test_disabled_item_is_skipped_and_state_frozen():
    repo = make_repo([master_row(監視="OFF")])
    report = runner.run(make_client([]), repo)  # API は一度も呼ばれない
    assert report.targets == 0 and report.ok
