"""MIS 盤中即時報價 parser 測試.

MIS 沒有官方文件，欄位是單字母代號、且未成交時大量欄位是 "-"。
這裡的重點是驗證這些「空值」情境不會產生假的資金流。
"""

import datetime as dt
import json

import pytest

from twflow.errors import ParseError
from twflow.sources.mis import channel, parse, parse_names


def entry(code="2330", z="1001.00", v="25000", a="1001_1002_1003_",
          b="999_998_997_", n="台積電", tlong="1787900000000", **kw):
    e = {"c": code, "z": z, "v": v, "a": a, "b": b, "n": n, "tlong": tlong}
    e.update(kw)
    return e


def payload(entries, rtcode="0000"):
    return json.dumps({"rtcode": rtcode, "rtmessage": "OK", "msgArray": entries})


class TestChannel:
    def test_listed_uses_tse_prefix(self):
        assert channel("2330", "TWSE") == "tse_2330.tw"

    def test_otc_uses_otc_prefix(self):
        assert channel("6488", "TPEX") == "otc_6488.tw"

    def test_unknown_market_falls_back_to_listed(self):
        assert channel("2330", "???") == "tse_2330.tw"


class TestParse:
    def test_parses_price_volume_and_best_quotes(self):
        q = parse(payload([entry()]))[0]
        assert q.code == "2330"
        assert q.price == 1001.0
        assert q.cum_volume == 25000.0
        # 五檔是底線分隔字串，只取最佳一檔
        assert q.ask1 == 1001.0   # a 由低到高，第一個是最佳賣價
        assert q.bid1 == 999.0    # b 由高到低，第一個是最佳買價

    def test_skips_stocks_with_no_trade_yet(self):
        # 開盤前或整天沒成交時 z 是 "-"，硬塞 0 進去會污染統計
        assert parse(payload([entry(z="-")])) == []

    def test_skips_zero_price(self):
        assert parse(payload([entry(z="0")])) == []

    def test_missing_book_yields_zero_not_crash(self):
        # 漲跌停鎖死時單邊五檔會消失
        q = parse(payload([entry(a="-", b="999_998_")]))[0]
        assert q.ask1 == 0.0
        assert q.bid1 == 999.0
        assert q.has_book is False

    def test_empty_book_strings_are_tolerated(self):
        q = parse(payload([entry(a="", b="")]))[0]
        assert q.ask1 == 0.0 and q.bid1 == 0.0

    def test_volume_dash_becomes_zero(self):
        q = parse(payload([entry(v="-")]))[0]
        assert q.cum_volume == 0.0

    def test_parses_epoch_millis_timestamp(self):
        q = parse(payload([entry(tlong="1787900000000")]))[0]
        assert isinstance(q.ts, dt.datetime)
        assert q.ts.tzinfo is not None

    def test_bad_timestamp_falls_back_to_now(self):
        # 時間戳只用來對齊分鐘桶，壞掉不該讓整批報價作廢
        q = parse(payload([entry(tlong="not-a-number")]))[0]
        assert isinstance(q.ts, dt.datetime)

    def test_handles_multiple_stocks(self):
        out = parse(payload([entry("2330"), entry("2317", z="200.5", n="鴻海")]))
        assert [q.code for q in out] == ["2330", "2317"]

    def test_skips_entries_without_a_code(self):
        assert parse(payload([entry(code="")])) == []

    def test_ignores_non_dict_entries(self):
        body = json.dumps({"rtcode": "0000", "msgArray": ["junk", entry()]})
        assert len(parse(body)) == 1

    def test_has_book_requires_sane_spread(self):
        # 賣價低於買價是異常資料，不該當成可用的五檔
        q = parse(payload([entry(a="990_", b="999_")]))[0]
        assert q.has_book is False


class TestErrors:
    def test_non_ok_rtcode_raises(self):
        with pytest.raises(ParseError, match="rtcode"):
            parse(payload([], rtcode="5001"))

    def test_missing_msgarray_raises(self):
        with pytest.raises(ParseError, match="msgArray"):
            parse(json.dumps({"rtcode": "0000"}))

    def test_html_error_page_raises(self):
        with pytest.raises(ParseError, match="不是合法 JSON"):
            parse("<html>Service Unavailable</html>")

    def test_empty_msgarray_is_not_an_error(self):
        # 全部都還沒成交是合法狀態，不是錯誤
        assert parse(payload([])) == []


class TestParseNames:
    def test_extracts_names(self):
        names = parse_names(payload([entry("2330", n="台積電"), entry("2317", n="鴻海")]))
        assert names == {"2330": "台積電", "2317": "鴻海"}

    def test_skips_entries_without_name(self):
        assert parse_names(payload([entry(n="")])) == {}
