"""T86 三大法人買賣超 parser 測試.

這裡的重點是**抗改版**：欄位順序、欄位拆分（外資拆成外陸資/外資自營商、
自營商拆成自行買賣/避險）都曾經變動過。開發環境連不到證交所，無法驗證
真實欄位，所以 parser 一律以標題關鍵字定位，並在這裡用構造的變體驗證。
"""

import json

import pytest

from twflow.errors import ParseError
from twflow.sources.twse_t86 import parse

NEW_FIELDS = [
    "證券代號", "證券名稱",
    "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
    "外陸資買賣超股數(不含外資自營商)",
    "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
    "投信買進股數", "投信賣出股數", "投信買賣超股數",
    "自營商買賣超股數",
    "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)", "自營商買賣超股數(自行買賣)",
    "自營商買進股數(避險)", "自營商賣出股數(避險)", "自營商買賣超股數(避險)",
    "三大法人買賣超股數",
]


def new_row(code="2330", name="台積電", foreign=1000, fdealer=100,
            trust=500, dealer=200, dealer_self=150, dealer_hedge=50, total=1800):
    return [
        code, name,
        "0", "0", f"{foreign:,}",
        "0", "0", f"{fdealer:,}",
        "0", "0", f"{trust:,}",
        f"{dealer:,}",
        "0", "0", f"{dealer_self:,}",
        "0", "0", f"{dealer_hedge:,}",
        f"{total:,}",
    ]


def payload(fields, rows, stat="OK"):
    return json.dumps({"stat": stat, "date": "20260827", "fields": fields, "data": rows})


class TestHappyPath:
    def test_parses_a_single_row(self):
        out = parse(payload(NEW_FIELDS, [new_row()]))
        assert len(out) == 1
        r = out[0]
        assert r["code"] == "2330"
        assert r["name"] == "台積電"
        assert r["market"] == "TWSE"

    def test_foreign_combines_mainland_and_foreign_dealer(self):
        # 改版後外資拆成兩欄，兩者都要算進外資
        out = parse(payload(NEW_FIELDS, [new_row(foreign=1000, fdealer=100)]))
        assert out[0]["foreign_net"] == 1100.0

    def test_dealer_prefers_the_total_column(self):
        # 自營商有總計欄時就用它，不要把分項再加一次造成重複計算
        out = parse(payload(NEW_FIELDS, [new_row(dealer=200, dealer_self=150, dealer_hedge=50)]))
        assert out[0]["dealer_net"] == 200.0

    def test_total_uses_the_official_column(self):
        out = parse(payload(NEW_FIELDS, [new_row(total=9999)]))
        assert out[0]["total_net"] == 9999.0


class TestSchemaResilience:
    def test_survives_column_reordering(self):
        # 把欄位順序整個反過來，依標題定位就不受影響
        order = list(range(len(NEW_FIELDS)))[::-1]
        fields = [NEW_FIELDS[i] for i in order]
        row = new_row()
        reordered = [row[i] for i in order]
        out = parse(payload(fields, [reordered]))
        assert out[0]["code"] == "2330"
        assert out[0]["foreign_net"] == 1100.0

    def test_handles_old_schema_without_foreign_dealer_split(self):
        fields = ["證券代號", "證券名稱", "外資買賣超股數",
                  "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數"]
        rows = [["2330", "台積電", "1,000", "500", "200", "1,700"]]
        out = parse(payload(fields, rows))
        assert out[0]["foreign_net"] == 1000.0
        assert out[0]["dealer_net"] == 200.0

    def test_derives_dealer_from_parts_when_total_absent(self):
        fields = ["證券代號", "證券名稱", "外資買賣超股數", "投信買賣超股數",
                  "自營商買賣超股數(自行買賣)", "自營商買賣超股數(避險)"]
        rows = [["2330", "台積電", "1,000", "500", "150", "50"]]
        out = parse(payload(fields, rows))
        assert out[0]["dealer_net"] == 200.0

    def test_derives_total_when_absent(self):
        fields = ["證券代號", "證券名稱", "外資買賣超股數", "投信買賣超股數", "自營商買賣超股數"]
        rows = [["2330", "台積電", "1,000", "500", "200"]]
        out = parse(payload(fields, rows))
        assert out[0]["total_net"] == 1700.0

    def test_reads_table_under_aaData_key(self):
        body = json.dumps({"stat": "OK", "fields": NEW_FIELDS, "aaData": [new_row()]})
        assert len(parse(body)) == 1

    def test_short_row_does_not_crash(self):
        # 某些列會比標題短，缺的欄位當 0 處理而不是整批失敗
        out = parse(payload(NEW_FIELDS, [["2330", "台積電", "0", "0", "1,000"]]))
        assert out[0]["foreign_net"] == 1000.0
        assert out[0]["trust_net"] == 0.0


class TestNumberParsing:
    @pytest.mark.parametrize("raw,expected", [
        ("1,234,567", 1234567.0),
        ("-1,234", -1234.0),
        ("(1,234)", -1234.0),
        ("-", 0.0),
        ("", 0.0),
        ("0", 0.0),
    ])
    def test_number_formats(self, raw, expected):
        fields = ["證券代號", "證券名稱", "外資買賣超股數", "投信買賣超股數", "自營商買賣超股數"]
        out = parse(payload(fields, [["2330", "台積電", raw, "0", "0"]]))
        assert out[0]["foreign_net"] == expected


class TestFiltering:
    def test_keeps_only_four_digit_numeric_codes(self):
        fields = ["證券代號", "證券名稱", "外資買賣超股數", "投信買賣超股數", "自營商買賣超股數"]
        rows = [
            ["2330", "台積電", "1", "0", "0"],       # 普通股 → 保留
            ["00940", "元大臺灣價值高息", "1", "0", "0"],  # ETF 5 碼 → 濾掉
            ["03019B", "元大美債", "1", "0", "0"],   # 債券 ETF → 濾掉
            ["2330P", "權證", "1", "0", "0"],        # 權證 → 濾掉
        ]
        out = parse(payload(fields, rows))
        assert [r["code"] for r in out] == ["2330"]


class TestErrors:
    def test_non_ok_stat_raises_with_the_reason(self):
        with pytest.raises(ParseError, match="很抱歉"):
            parse(payload(NEW_FIELDS, [], stat="很抱歉，沒有符合條件的資料!"))

    def test_malformed_json_raises(self):
        with pytest.raises(ParseError, match="不是合法 JSON"):
            parse("<html>503 Service Unavailable</html>")

    def test_missing_fields_key_raises_with_observed_keys(self):
        with pytest.raises(ParseError, match="找不到欄位標題"):
            parse(json.dumps({"stat": "OK", "data": []}))

    def test_missing_required_column_reports_actual_headers(self):
        fields = ["證券代號", "證券名稱", "收盤價"]
        with pytest.raises(ParseError) as exc:
            parse(payload(fields, [["2330", "台積電", "1000"]]))
        assert "收盤價" in str(exc.value.observed)

    def test_empty_result_raises_rather_than_returning_nothing(self):
        # 非交易日回空表；靜默回傳空清單會讓上游誤以為當天法人零買賣
        fields = ["證券代號", "證券名稱", "外資買賣超股數", "投信買賣超股數", "自營商買賣超股數"]
        with pytest.raises(ParseError, match="非交易日"):
            parse(payload(fields, []))
