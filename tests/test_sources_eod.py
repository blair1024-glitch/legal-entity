"""其餘盤後來源的 parser 測試：TAIFEX、TPEx、產業別、外資持股、券商分點.

這些來源的欄位結構在開發環境無法對照真實回應驗證，所以測試的重點是
**格式變動時不會靜默算錯**：欄位改名、順序調換、民國／西元年混用、
單位換算，全部都要有明確的斷言。
"""

import datetime as dt
import json

import pytest

from twflow.errors import ParseError
from twflow.sources import bsr, taifex, tpex_insti, twse_meta, twse_qfiis
from twflow.tradingcal import roc_to_date


# ---------------------------------------------------------------- 期交所

TAIFEX_HEADER = (
    "日期,商品名稱,身份別,多方交易口數,多方契約金額(千元),空方交易口數,"
    "空方契約金額(千元),多空交易口數淨額,多空契約金額淨額(千元),"
    "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,"
    "空方未平倉契約金額(千元),多空未平倉口數淨額,多空未平倉契約金額淨額(千元)"
)


def taifex_csv(rows, header=TAIFEX_HEADER):
    return header + "\n" + "\n".join(rows)


class TestTaifex:
    def test_parses_open_interest_not_trading_volume(self):
        # 未平倉才代表法人手上的部位，交易口數只是當日進出
        row = "2026/08/27,臺股期貨,外資,100,1,200,2,-100,-1,5000,50,3000,30,2000,20"
        out = taifex.parse(taifex_csv([row]))
        assert len(out) == 1
        r = out[0]
        assert r["long_oi"] == 5000     # 多方未平倉，不是多方交易口數 100
        assert r["short_oi"] == 3000
        assert r["net_oi"] == 2000

    def test_converts_contract_value_from_thousands_to_dollars(self):
        row = "2026/08/27,臺股期貨,外資,0,0,0,0,0,0,5000,50,3000,30,2000,20"
        assert taifex.parse(taifex_csv([row]))[0]["net_value"] == 20_000.0

    def test_accepts_roc_calendar_dates(self):
        row = "115/08/27,臺股期貨,外資,0,0,0,0,0,0,10,0,5,0,5,0"
        assert taifex.parse(taifex_csv([row]))[0]["trade_date"] == "2026-08-27"

    def test_normalises_party_aliases(self):
        rows = [
            "2026/08/27,臺股期貨,外資及陸資,0,0,0,0,0,0,10,0,5,0,5,0",
            "2026/08/27,臺股期貨,自營商(避險),0,0,0,0,0,0,10,0,5,0,5,0",
        ]
        parties = {r["party"] for r in taifex.parse(taifex_csv(rows))}
        assert parties == {"外資", "自營商"}

    def test_derives_net_when_column_absent(self):
        header = ("日期,商品名稱,身份別,多方未平倉口數,空方未平倉口數")
        row = "2026/08/27,臺股期貨,外資,5000,3000"
        assert taifex.parse(taifex_csv([row], header))[0]["net_oi"] == 2000

    def test_survives_column_reordering(self):
        header = ("身份別,日期,空方未平倉口數,多方未平倉口數,商品名稱")
        row = "外資,2026/08/27,3000,5000,臺股期貨"
        r = taifex.parse(taifex_csv([row], header))[0]
        assert r["long_oi"] == 5000 and r["short_oi"] == 3000

    def test_strips_bom(self):
        row = "2026/08/27,臺股期貨,外資,0,0,0,0,0,0,10,0,5,0,5,0"
        assert len(taifex.parse("﻿" + taifex_csv([row]))) == 1

    def test_missing_required_column_raises(self):
        with pytest.raises(ParseError, match="缺少必要欄位"):
            taifex.parse("日期,商品名稱\n2026/08/27,臺股期貨")

    def test_empty_csv_raises(self):
        with pytest.raises(ParseError, match="沒有資料列"):
            taifex.parse(TAIFEX_HEADER)

    def test_rows_with_unparseable_dates_are_skipped_not_fatal(self):
        rows = [
            "壞掉的日期,臺股期貨,外資,0,0,0,0,0,0,10,0,5,0,5,0",
            "2026/08/27,臺股期貨,投信,0,0,0,0,0,0,10,0,5,0,5,0",
        ]
        assert len(taifex.parse(taifex_csv(rows))) == 1


# ---------------------------------------------------------------- 上櫃

class TestTpexInsti:
    def test_parses_english_keys(self):
        body = json.dumps([{
            "SecuritiesCompanyCode": "6488",
            "CompanyName": "環球晶",
            "ForeignInvestorsBuySell": "1,000",
            "SecuritiesInvestmentTrustBuySell": "500",
            "DealersBuySell": "200",
            "TotalInstitutionalInvestorsBuySell": "1,700",
        }])
        r = tpex_insti.parse(body)[0]
        assert r["code"] == "6488"
        assert r["foreign_net"] == 1000.0
        assert r["market"] == "TPEX"

    def test_parses_chinese_keys(self):
        body = json.dumps([{
            "股票代號": "6488", "股票名稱": "環球晶",
            "外資及陸資買賣超股數": "1000", "投信買賣超股數": "500",
            "自營商買賣超股數": "200", "三大法人買賣超股數": "1700",
        }])
        assert tpex_insti.parse(body)[0]["total_net"] == 1700.0

    def test_derives_total_when_missing(self):
        body = json.dumps([{
            "SecuritiesCompanyCode": "6488",
            "ForeignInvestorsBuySell": "1000",
            "SecuritiesInvestmentTrustBuySell": "500",
            "DealersBuySell": "200",
        }])
        assert tpex_insti.parse(body)[0]["total_net"] == 1700.0

    def test_filters_non_four_digit_codes(self):
        body = json.dumps([
            {"SecuritiesCompanyCode": "6488", "ForeignInvestorsBuySell": "1"},
            {"SecuritiesCompanyCode": "00679B", "ForeignInvestorsBuySell": "1"},
        ])
        assert [r["code"] for r in tpex_insti.parse(body)] == ["6488"]

    def test_empty_list_raises_rather_than_returning_nothing(self):
        with pytest.raises(ParseError, match="沒有任何上櫃資料"):
            tpex_insti.parse("[]")

    def test_non_list_payload_raises(self):
        with pytest.raises(ParseError, match="預期 JSON 陣列"):
            tpex_insti.parse('{"stat":"OK"}')


# ---------------------------------------------------------------- 產業別

class TestTwseMeta:
    def test_maps_industry_code_to_name(self):
        assert twse_meta.normalize_industry("24") == "半導體業"

    def test_zero_pads_single_digit_codes(self):
        assert twse_meta.normalize_industry("1") == "水泥工業"

    def test_passes_through_chinese_names(self):
        assert twse_meta.normalize_industry("半導體業") == "半導體業"

    def test_blank_becomes_unclassified(self):
        assert twse_meta.normalize_industry("") == "未分類"

    def test_unknown_code_is_kept_verbatim(self):
        # 新增的產業別代號不該被吞掉變成「未分類」
        assert twse_meta.normalize_industry("99") == "99"

    def test_parses_company_records(self):
        body = json.dumps([
            {"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24"},
            {"公司代號": "1101", "公司簡稱": "台泥", "產業別": "水泥工業"},
        ])
        out = twse_meta.parse(body, market="TWSE")
        assert out[0]["industry"] == "半導體業"
        assert out[1]["industry"] == "水泥工業"
        assert all(r["market"] == "TWSE" for r in out)

    def test_accepts_alternate_key_names(self):
        body = json.dumps([{"Code": "2330", "Name": "台積電", "IndustryName": "半導體業"}])
        assert twse_meta.parse(body)[0]["code"] == "2330"

    def test_filters_etfs_and_warrants(self):
        body = json.dumps([
            {"公司代號": "2330", "產業別": "24"},
            {"公司代號": "0050", "產業別": ""},
            {"公司代號": "00940", "產業別": ""},
        ])
        # 0050 是 4 碼數字，會保留；00940 是 5 碼，濾掉
        assert [r["code"] for r in twse_meta.parse(body)] == ["2330", "0050"]

    def test_empty_raises(self):
        with pytest.raises(ParseError, match="沒有任何公司資料"):
            twse_meta.parse("[]")


# ---------------------------------------------------------------- 外資持股

class TestTwseQfiis:
    def test_parses_holding_ratio(self):
        body = json.dumps({
            "stat": "OK",
            "fields": ["證券代號", "證券名稱", "發行股數",
                       "全體外資及陸資持有股數", "外資及陸資持股比率"],
            "data": [["2330", "台積電", "25,930,380,458", "18,700,000,000", "72.12"]],
        })
        r = twse_qfiis.parse(body)[0]
        assert r["code"] == "2330"
        assert r["foreign_ratio"] == 72.12
        assert r["issued_shares"] == 25_930_380_458.0

    def test_works_without_optional_columns(self):
        body = json.dumps({
            "stat": "OK",
            "fields": ["證券代號", "外資及陸資持股比率"],
            "data": [["2330", "72.12"]],
        })
        assert twse_qfiis.parse(body)[0]["foreign_ratio"] == 72.12

    def test_missing_ratio_column_raises(self):
        body = json.dumps({"stat": "OK", "fields": ["證券代號", "收盤價"],
                           "data": [["2330", "1000"]]})
        with pytest.raises(ParseError):
            twse_qfiis.parse(body)


# ---------------------------------------------------------------- 券商分點

class TestBsr:
    def test_sums_across_price_levels(self):
        # BSR 是分價量表，同一分點會出現在多列不同價位，必須累加
        csv = (
            "序號,券商,價格,買進股數,賣出股數\n"
            "1,1020 合庫,100.0,1000,0\n"
            "2,1020 合庫,100.5,2000,0\n"
            "3,9200 華南永昌,100.0,0,500\n"
        )
        out = {r["broker_id"]: r for r in bsr.parse(csv, "2330")}
        assert out["1020"]["buy_shares"] == 3000
        assert out["1020"]["net_shares"] == 3000
        assert out["9200"]["net_shares"] == -500

    def test_handles_two_groups_per_row(self):
        # BSR 下載檔把兩組分點橫向排在同一列
        csv = (
            "序號,券商,價格,買進股數,賣出股數,序號,券商,價格,買進股數,賣出股數\n"
            "1,1020 合庫,100.0,1000,0,2,9200 華南永昌,100.0,0,500\n"
        )
        out = {r["broker_id"]: r for r in bsr.parse(csv, "2330")}
        assert out["1020"]["buy_shares"] == 1000
        assert out["9200"]["sell_shares"] == 500

    def test_skips_preamble_rows_before_the_header(self):
        csv = (
            "查詢日期:2026/08/27\n"
            "股票代號:2330 台積電\n"
            "\n"
            "序號,券商,價格,買進股數,賣出股數\n"
            "1,1020 合庫,100.0,1000,0\n"
        )
        assert len(bsr.parse(csv, "2330")) == 1

    def test_broker_without_id_still_parsed(self):
        csv = "序號,券商,價格,買進股數,賣出股數\n1,某某證券,100.0,1000,0\n"
        r = bsr.parse(csv, "2330")[0]
        assert r["broker_name"] == "某某證券"
        assert r["broker_id"] == "某某證券"

    def test_rows_with_no_activity_are_dropped(self):
        csv = "序號,券商,價格,買進股數,賣出股數\n1,1020 合庫,100.0,0,0\n2,9200 永昌,100,5,0\n"
        assert [r["broker_id"] for r in bsr.parse(csv, "2330")] == ["9200"]

    def test_missing_header_raises(self):
        with pytest.raises(ParseError, match="券商"):
            bsr.parse("完全不相干的內容\n1,2,3\n", "2330")

    def test_identifies_state_brokers_by_name(self):
        assert bsr.is_state_broker("合作金庫") is True
        assert bsr.is_state_broker("1020 合庫") is True
        assert bsr.is_state_broker("凱基") is False

    def test_state_broker_summary_nets_across_brokers(self):
        rows = [
            {"trade_date": "2026-08-27", "code": "2330",
             "broker_name": "合庫", "net_shares": 1000},
            {"trade_date": "2026-08-27", "code": "2330",
             "broker_name": "華南永昌", "net_shares": -300},
            {"trade_date": "2026-08-27", "code": "2330",
             "broker_name": "凱基", "net_shares": 9999},   # 非官股，應排除
        ]
        out = bsr.state_broker_summary(rows)
        assert len(out) == 1
        assert out[0]["state_net_shares"] == 700
        assert out[0]["brokers"] == 2


# ---------------------------------------------------------------- 日期工具

class TestRocDate:
    @pytest.mark.parametrize("raw,expected", [
        ("115/08/27", "2026-08-27"),   # 民國
        ("2026/08/27", "2026-08-27"),  # 西元
        ("2026-08-27", "2026-08-27"),  # 破折號
    ])
    def test_accepts_both_calendars(self, raw, expected):
        assert roc_to_date(raw).isoformat() == expected

    def test_rejects_malformed(self):
        with pytest.raises(ValueError):
            roc_to_date("八月二十七")


class TestQfiisWholeMarketGuard:
    """MI_QFIIS 必須確認拿到的是全市場，而不是某個產業的子集.

    實測發現：這個端點不帶 selectType 時會回 8 檔水泥股而且 stat=OK。
    若候選機制照單全收，資料庫裡就會存進一份「看起來正常」的殘缺資料，
    自選股的外資持股比率會整片是空的，而且完全看不出哪裡錯了。
    """

    def _payload(self, n):
        return json.dumps({
            "stat": "OK",
            "fields": ["證券代號", "證券名稱", "外資及陸資持股比率"],
            "data": [[f"{1101 + i}", f"股票{i}", "12.34"] for i in range(n)],
        })

    def test_rejects_a_partial_dataset(self, monkeypatch):
        from twflow.httpclient import Response
        from twflow.sources import twse_qfiis

        calls = []

        class FakeFetcher:
            def get(self, url, params=None, **kw):
                calls.append(params.get("selectType"))
                # 兩個候選都只回一小撮資料
                return Response(url=url, status=200, text=self_outer._payload(8))

        self_outer = self
        with pytest.raises(ParseError, match="不像全市場|拿不到"):
            twse_qfiis.fetch(FakeFetcher(), dt.date(2026, 8, 27))
        # 第一個候選不夠格，應該有繼續試下一個
        assert len(calls) == len(twse_qfiis.SELECT_TYPES)

    def test_accepts_a_full_market_dataset(self):
        from twflow.httpclient import Response
        from twflow.sources import twse_qfiis

        outer = self

        class FakeFetcher:
            def get(self, url, params=None, **kw):
                return Response(url=url, status=200, text=outer._payload(1360))

        rows = twse_qfiis.fetch(FakeFetcher(), dt.date(2026, 8, 27))
        assert len(rows) == 1360
        assert rows[0]["trade_date"] == "2026-08-27"

    def test_no_selecttype_candidate_was_removed(self):
        """不帶參數會回單一產業卻不報錯，不該留在候選清單裡."""
        from twflow.sources import twse_qfiis

        assert None not in twse_qfiis.SELECT_TYPES
        assert twse_qfiis.SELECT_TYPES[0] == "ALLBUT0999"
