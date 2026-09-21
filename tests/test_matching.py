from price_monitor.matching import filter_offers, split_terms, squash
from price_monitor.models import Offer, WatchItem


def offer(code, name, shop="shopa"):
    return Offer(code, name, shop, shop, 1000, 1, True, True, 0, 0.0, "")


def item(**kw):
    return WatchItem(item_id="i1", name="n", keyword="k", **kw)


def test_split_terms_handles_mixed_separators():
    assert split_terms("中古, ケース　互換、 ジャンク\n訳あり") == ("中古", "ケース", "互換", "ジャンク", "訳あり")
    assert split_terms("") == ()


def test_squash_absorbs_width_case_space_and_hyphen():
    assert squash("ＡＢＣ－１２３") == squash("abc 123") == "abc123"


def test_model_number_must_appear_regardless_of_notation():
    offers = [offer("a:1", "ブランド ABC-123 本体"), offer("b:1", "ブランド XYZ-999")]
    kept = filter_offers(offers, item(model="abc123"))
    assert [o.item_code for o in kept] == ["a:1"]


def test_multiple_required_terms_must_all_appear_in_any_order():
    offers = [
        offer("a:1", "グーン ぐんぐん吸収パンツ Mサイズ ディズニー 66枚入"),
        offer("b:1", "グーンぐんぐん吸収パンツＭサイズ66枚"),
        offer("c:1", "グーン まっさらさら通気 パンツ Mサイズ 66枚"),  # 別シリーズ
        offer("d:1", "グーン ぐんぐん吸収パンツ Lサイズ 66枚"),  # サイズ違い
    ]
    kept = filter_offers(offers, item(model="ぐんぐん吸収パンツ Mサイズ 66枚"))
    assert [o.item_code for o in kept] == ["a:1", "b:1"]


def test_ng_keywords_and_ng_shops():
    offers = [
        offer("a:1", "ABC-123 本体"),
        offer("b:1", "ABC-123 中古品"),
        offer("own:1", "ABC-123 本体", shop="OwnShop"),
    ]
    kept = filter_offers(offers, item(model="ABC-123", ng_keywords=("中古",), ng_shops=("ownshop",)))
    assert [o.item_code for o in kept] == ["a:1"]


def test_pinned_codes_override_other_filters():
    offers = [offer("a:1", "型番なし商品", shop="OwnShop"), offer("b:1", "ABC-123")]
    kept = filter_offers(offers, item(model="ABC-123", ng_shops=("ownshop",), pinned_codes=("A:1",)))
    assert [o.item_code for o in kept] == ["a:1"]
