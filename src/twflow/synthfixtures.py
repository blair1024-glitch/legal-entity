"""產生合成 fixture 樣本.

## 為什麼需要這個

本專案的開發環境連不到台股資料源，但整條管線仍然要能離線跑起來、被測試。
這裡依各端點**已知的欄位格式**手工建構出結構正確的假回應，寫進 ``fixtures/``。

**這些是合成資料，欄位結構是依公開文件與社群慣例推斷的，數值全是假的。**
每個檔案旁邊的 ``.meta.json`` 都標記 ``synthetic: true``。

使用者在有外網的機器上執行 ``twflow record`` 後，這些檔案會被**真實回應覆蓋**，
屆時 ``pytest`` 就是在真實結構上驗證 parser——那才是真正的驗收。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .httpclient import fixture_key
from .sources import mis, taifex, tpex_insti, twse_meta, twse_qfiis, twse_t86

# 幾檔具代表性的股票，涵蓋不同產業別
SAMPLE = [
    ("2330", "台積電", "24", 1005.0),
    ("2317", "鴻海", "31", 215.5),
    ("2454", "聯發科", "24", 1420.0),
    ("2881", "富邦金", "17", 92.3),
    ("2603", "長榮", "15", 205.0),
    ("1101", "台泥", "01", 33.4),
]

TPEX_SAMPLE = [("6488", "環球晶", 465.0), ("5483", "中美晶", 178.5)]


def _t86() -> str:
    fields = [
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
    data = []
    for i, (code, name, _, _) in enumerate(SAMPLE):
        sign = 1 if i % 2 == 0 else -1
        foreign = sign * (1_000_000 + i * 137_000)
        trust = sign * (200_000 + i * 31_000)
        dealer_self = sign * 50_000
        dealer_hedge = sign * 30_000
        dealer = dealer_self + dealer_hedge
        data.append([
            code, name,
            f"{abs(foreign) * 3:,}", f"{abs(foreign) * 2:,}", f"{foreign:,}",
            "0", "0", "0",
            f"{abs(trust) * 2:,}", f"{abs(trust):,}", f"{trust:,}",
            f"{dealer:,}",
            "0", "0", f"{dealer_self:,}",
            "0", "0", f"{dealer_hedge:,}",
            f"{foreign + trust + dealer:,}",
        ])
    return json.dumps(
        {"stat": "OK", "date": "20260827", "title": "三大法人買賣超日報",
         "fields": fields, "data": data},
        ensure_ascii=False,
    )


def _qfiis() -> str:
    fields = ["證券代號", "證券名稱", "發行股數", "全體外資及陸資持有股數",
              "外資及陸資持股比率"]
    data = [
        [code, name, f"{25_000_000_000 - i * 1_000_000_000:,}",
         f"{15_000_000_000 - i * 900_000_000:,}", f"{72.5 - i * 6.3:.2f}"]
        for i, (code, name, _, _) in enumerate(SAMPLE)
    ]
    return json.dumps({"stat": "OK", "fields": fields, "data": data}, ensure_ascii=False)


def _meta(market: str) -> str:
    if market == "TWSE":
        rows = [
            {"出表日期": "1150827", "公司代號": code, "公司名稱": f"{name}股份有限公司",
             "公司簡稱": name, "產業別": ind}
            for code, name, ind, _ in SAMPLE
        ]
    else:
        rows = [
            {"SecuritiesCompanyCode": code, "CompanyName": name, "SecuritiesIndustryCode": "24"}
            for code, name, _ in TPEX_SAMPLE
        ]
    return json.dumps(rows, ensure_ascii=False)


def _tpex_insti() -> str:
    rows = []
    for i, (code, name, _) in enumerate(TPEX_SAMPLE):
        sign = 1 if i % 2 == 0 else -1
        rows.append({
            "SecuritiesCompanyCode": code,
            "CompanyName": name,
            "ForeignInvestorsBuySell": f"{sign * 800_000:,}",
            "SecuritiesInvestmentTrustBuySell": f"{sign * 150_000:,}",
            "DealersBuySell": f"{sign * 40_000:,}",
            "TotalInstitutionalInvestorsBuySell": f"{sign * 990_000:,}",
        })
    return json.dumps(rows, ensure_ascii=False)


def _mis() -> str:
    arr = []
    for code, name, _, px in SAMPLE:
        tick = max(round(px * 0.001, 2), 0.01)
        bid, ask = round(px - tick, 2), round(px + tick, 2)
        arr.append({
            "c": code, "n": name, "ch": f"{code}.tw", "ex": "tse",
            "z": f"{px:.2f}", "v": "18452", "o": f"{px * 0.995:.2f}",
            "h": f"{px * 1.01:.2f}", "l": f"{px * 0.99:.2f}", "y": f"{px * 0.998:.2f}",
            "a": "_".join(f"{ask + tick * k:.2f}" for k in range(5)) + "_",
            "b": "_".join(f"{bid - tick * k:.2f}" for k in range(5)) + "_",
            "f": "120_85_63_44_31_", "g": "98_77_55_41_28_",
            "t": "13:29:58", "tlong": str(int(time.time() * 1000)),
        })
    return json.dumps(
        {"msgArray": arr, "userDelay": 5000, "rtmessage": "OK", "rtcode": "0000"},
        ensure_ascii=False,
    )


def _taifex() -> str:
    header = (
        "日期,商品名稱,身份別,多方交易口數,多方契約金額(千元),空方交易口數,"
        "空方契約金額(千元),多空交易口數淨額,多空契約金額淨額(千元),"
        "多方未平倉口數,多方未平倉契約金額(千元),空方未平倉口數,"
        "空方未平倉契約金額(千元),多空未平倉口數淨額,多空未平倉契約金額淨額(千元)"
    )
    rows = [
        "2026/08/27,臺股期貨,自營商,12045,29873,11302,28031,743,1842,8921,22134,10233,25390,-1312,-3256",
        "2026/08/27,臺股期貨,投信,1832,4544,902,2238,930,2306,15420,38251,2103,5216,13317,33035",
        "2026/08/27,臺股期貨,外資,38210,94800,41022,101764,-2812,-6964,52180,129406,38945,96583,13235,32823",
    ]
    return header + "\n" + "\n".join(rows) + "\n"


# (檔名說明, url, params, body, 內容產生器)
SPECS = [
    ("上市三大法人買賣超 T86", twse_t86.URL,
     {"date": "20260827", "selectType": "ALL", "response": "json"}, None, _t86),
    ("外資持股比率 MI_QFIIS", twse_qfiis.URL,
     {"date": "20260827", "selectType": "ALL", "response": "json"}, None, _qfiis),
    ("上市公司基本資料", twse_meta.TWSE_URL, None, None, lambda: _meta("TWSE")),
    ("上櫃公司基本資料", twse_meta.TPEX_URL, None, None, lambda: _meta("TPEX")),
    ("上櫃三大法人買賣超", tpex_insti.URL, None, None, _tpex_insti),
]


def generate(fixture_dir: str | Path = "fixtures") -> list[str]:
    """產生所有合成 fixture，回傳寫出的檔名清單."""
    d = Path(fixture_dir)
    d.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    specs = list(SPECS)

    # MIS 的 ex_ch 依查詢的股票而異，doctor 只查前兩檔，這裡對齊它
    specs.append((
        "MIS 盤中即時報價", mis.URL,
        {"ex_ch": "tse_2330.tw|tse_2317.tw", "json": "1", "delay": "0"}, None, _mis,
    ))

    # 期交所是 POST，每個契約各一次
    stamp = "2026/08/27"
    for contract in taifex.DEFAULT_CONTRACTS:
        specs.append((
            f"期貨三大法人未平倉 {contract}", taifex.URL, None,
            {"firstDate": stamp, "lastDate": stamp, "queryStartDate": stamp,
             "queryEndDate": stamp, "commodityId": contract},
            _taifex,
        ))

    for label, url, params, body, builder in specs:
        key = fixture_key(url, params, body)
        (d / f"{key}.txt").write_text(builder(), encoding="utf-8")
        (d / f"{key}.meta.json").write_text(
            json.dumps(
                {
                    "label": label,
                    "url": url,
                    "params": params or {},
                    "body": body or {},
                    "synthetic": True,
                    "note": (
                        "合成樣本：欄位結構依公開文件與社群慣例推斷，數值為假。"
                        "在有外網的機器上執行 `twflow record` 會以真實回應覆蓋。"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        written.append(f"{label}  →  {key}.txt")

    return written
