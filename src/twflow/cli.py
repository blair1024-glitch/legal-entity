"""twflow 命令列介面.

指令一覽::

    twflow doctor    檢查每個資料來源的可達性與欄位結構（本機外網環境必跑）
    twflow record    抓取真實回應並存進 fixtures/，供離線測試使用
    twflow sync      匯入上市／上櫃證券清單與產業別
    twflow poll      盤中輪詢，把即時報價轉成資金流
    twflow eod       盤後：抓官方三大法人數據並校準推估值
                     加 --since/--until 可回補一段日期區間
    twflow auto      一個指令跑完整天：盤中輪詢、收盤後自動跑盤後流程
    twflow serve     啟動網頁儀表板
    twflow demo      產生合成資料，讓儀表板在沒有外網時也能完整展示
    twflow fixtures  產生合成 fixture 樣本，讓離線也能跑完整的 doctor
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

from .config import Config
from .errors import TwflowError
from .httpclient import Fetcher
from .store import Store
from .tradingcal import previous_trading_day, today_taipei


def _log_setup(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _fetcher(config: Config, mode_override: str | None = None) -> Fetcher:
    return Fetcher(
        mode=mode_override or str(config.get("mode", "fixture")),
        fixture_dir=config.get("fixture_dir", "fixtures"),
    )


def _parse_date(value: str | None) -> dt.date:
    if not value:
        # 預設抓「上一個交易日」——當日盤後資料要 15:00 後才有，
        # 直接預設今天在盤中執行只會拿到空資料。
        return previous_trading_day(today_taipei() + dt.timedelta(days=1))
    return dt.date.fromisoformat(value)


# ---------- doctor ----------

def cmd_doctor(args, config: Config) -> int:
    """逐一檢查每個資料來源，印出可達性與欄位結構診斷.

    這個指令存在的理由：本專案的開發環境連不到台股資料源，所有端點與欄位
    結構都是依公開文件與社群慣例實作的。在有外網的機器上跑一次 doctor，
    就能立刻看出哪個來源的實際結構和實作假設不符。
    """
    from .sources import bsr, mis, taifex, tpex_insti, twse_meta, twse_qfiis, twse_t86

    day = _parse_date(args.date)
    fetcher = _fetcher(config, args.mode)

    checks = [
        ("MIS 盤中即時報價", lambda: mis.fetch_batch(fetcher, [("2330", "TWSE"), ("2317", "TWSE")])),
        ("上市證券清單與產業別", lambda: twse_meta.fetch(fetcher, "TWSE")),
        ("上櫃證券清單與產業別", lambda: twse_meta.fetch(fetcher, "TPEX")),
        ("上市三大法人買賣超 (T86)", lambda: twse_t86.fetch(fetcher, day)),
        ("上櫃三大法人買賣超", lambda: tpex_insti.fetch(fetcher, day)),
        ("外資持股比率 (MI_QFIIS)", lambda: twse_qfiis.fetch(fetcher, day)),
        ("期貨三大法人未平倉", lambda: taifex.fetch(fetcher, day)),
        ("券商分點（本機檔案）", lambda: bsr.load_directory()),
    ]

    banner = {
        "live": "live —— 實際連線到證交所",
        "fixture": "fixture —— 只讀本機樣本，不連網",
        "record": "record —— 實際連線並錄製樣本",
    }.get(fetcher.mode, fetcher.mode)

    print(f"\n資料來源診斷  日期={day}")
    print(f"模式：{banner}")
    if fetcher.mode == "fixture":
        # 這一段很重要：fixture 模式下全綠代表「樣本解析得動」，
        # 完全沒有驗證到「連得上證交所」。不講清楚會給出假的安心感。
        print(
            "\n  ⚠  這不是連線測試。以下結果只證明 parser 讀得懂本機樣本，\n"
            "     沒有碰到證交所。要驗證真實連線請改用：\n"
            "         twflow --mode live doctor\n"
            "     （或在 config.yaml 設 mode: live）"
        )
    print("─" * 68)
    failures = 0
    for name, fn in checks:
        try:
            rows = fn()
        except TwflowError as exc:
            failures += 1
            print(f"✗ {name}")
            print(f"    {exc}")
            observed = getattr(exc, "observed", None)
            if observed:
                print(f"    實際觀察到: {str(observed)[:400]}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"✗ {name}\n    未預期的錯誤: {exc!r}")
        else:
            n = len(rows)
            sample = ""
            if n:
                first = rows[0]
                sample = str(first if isinstance(first, dict) else vars(first))[:220]
            status = "✓" if n else "△"
            print(f"{status} {name}: {n} 筆")
            if sample:
                print(f"    範例: {sample}")
    print("─" * 68)
    if failures:
        print(
            f"{failures} 個來源有問題。若欄位結構與預期不符，請對照上面的"
            f"「實際觀察到」修正 src/twflow/sources/ 底下對應的 parser。"
        )
    elif fetcher.mode == "fixture":
        print(
            "樣本全部解析成功——但**這不代表連得上證交所**。\n"
            "請改跑 `twflow --mode live doctor` 才算真正驗證過。"
        )
    else:
        print("全部通過。建議接著執行 `twflow record` 錄下真實樣本，再跑 pytest。")
    print()
    return 1 if failures else 0


# ---------- record ----------

def cmd_record(args, config: Config) -> int:
    """把真實回應錄進 fixtures/，讓離線測試跑在真實結構上."""
    args.mode = "record"
    print("以 record 模式抓取，回應會寫入 fixtures/ …")
    rc = cmd_doctor(args, config)
    print("錄製完成。接著執行 `pytest` ——這一步會用真實樣本檢驗 parser。")
    return rc


# ---------- sync ----------

def cmd_sync(args, config: Config) -> int:
    from .pipeline import sync_securities

    with Store(config.get("db_path")) as store:
        report = sync_securities(
            store, _fetcher(config, args.mode), [str(m) for m in config.get("markets", ["TWSE"])]
        )
        print("證券清單同步:")
        print(report.render())
        return 0 if report.ok else 1


# ---------- poll ----------

def cmd_poll(args, config: Config) -> int:
    from .poller import Poller

    with Store(config.get("db_path")) as store:
        poller = Poller(store, _fetcher(config, args.mode), config)
        if not poller.universe():
            print("universe 是空的。請先執行 `twflow sync` 匯入證券清單。", file=sys.stderr)
            return 1
        poller.run(once=args.once, ignore_session=args.ignore_session)
    return 0


# ---------- eod ----------

def cmd_eod(args, config: Config) -> int:
    from .pipeline import run_eod, run_eod_range

    if args.since:
        return _eod_range(args, config)

    day = _parse_date(args.date)
    with Store(config.get("db_path")) as store:
        report = run_eod(
            store,
            _fetcher(config, args.mode),
            day,
            markets=[str(m) for m in config.get("markets", ["TWSE"])],
        )
        print(f"盤後流程 {day}:")
        print(report.render())

        removed = store.purge_old(int(config.get("retention_days", 30)))
        if removed:
            print(f"  · 清理過期盤中資料: {removed} 筆")

        from .calibrate import accuracy_summary

        acc = accuracy_summary(store)
        if acc.get("available"):
            latest = acc["latest"]
            print(
                f"\n推估準確度（{latest['trade_date']}，{latest['n_stocks']} 檔）:\n"
                f"  等級相關 Spearman : {latest['spearman']:+.3f}\n"
                f"  方向一致比例      : {latest['sign_match']:.1%}\n"
                f"  近 {acc['days']} 日平均 Spearman: {acc['mean_spearman']:+.3f}"
            )
        else:
            print("\n（尚無盤中推估資料可供校準——盤中先跑過 `twflow poll` 才會有。）")
        return 0 if report.ok else 1


def _eod_range(args, config: Config) -> int:
    """回補一段日期區間."""
    from .pipeline import run_eod_range

    start = dt.date.fromisoformat(args.since)
    end = dt.date.fromisoformat(args.until) if args.until else today_taipei()
    if start > end:
        print(f"起始日 {start} 晚於結束日 {end}", file=sys.stderr)
        return 1

    fetcher = _fetcher(config, args.mode)
    markets = [str(m) for m in config.get("markets", ["TWSE"])]

    print(f"回補 {start} → {end}\n")
    failed: list[str] = []

    def on_day(day, report):
        ok = sum(1 for s in report.steps if s.ok)
        mark = "✓" if report.ok else "✗"
        print(f"  {mark} {day}  {ok}/{len(report.steps)} 個步驟成功")
        if not report.ok:
            failed.append(day.isoformat())
            for step in report.steps:
                if not step.ok:
                    print(f"        {step.name}: {step.detail[:110]}")

    with Store(config.get("db_path")) as store:
        results = run_eod_range(
            store, fetcher, start, end, markets=markets, on_day=on_day
        )
        n = update_coefficients_after_backfill(store)

    print(f"\n完成 {len(results)} 個交易日，{len(failed)} 天有問題。")
    if failed:
        print(f"  有問題的日期: {', '.join(failed[:10])}"
              + (" …" if len(failed) > 10 else ""))
        print("  非交易日失敗是正常的；若是欄位結構問題，請跑 `twflow doctor`。")
    print(f"更新 {n} 檔的校準係數。")
    print(
        "\n提醒：盤中推估資料**無法回補**（證交所沒有歷史分時報價），"
        "\n所以校準要等你實際盤中跑過 `twflow poll` 之後才會有樣本可比。"
    )
    return 0


def update_coefficients_after_backfill(store) -> int:
    from .calibrate import update_coefficients

    return update_coefficients(store)


# ---------- auto ----------

def cmd_auto(args, config: Config) -> int:
    from .scheduler import Scheduler

    with Store(config.get("db_path")) as store:
        poller_universe = Scheduler(store, _fetcher(config, args.mode), config)
        if not store.securities():
            print("證券清單是空的。請先執行 `twflow sync`。", file=sys.stderr)
            return 1
        poller_universe.run()
    return 0


# ---------- serve ----------

def cmd_serve(args, config: Config) -> int:
    import uvicorn

    from .api import create_app

    store = Store(config.get("db_path"))
    app = create_app(store, config)
    host = args.host or str(config.get("server.host", "127.0.0.1"))
    port = args.port or int(config.get("server.port", 8000))
    print(f"儀表板啟動於 http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


# ---------- demo ----------

def cmd_demo(args, config: Config) -> int:
    from .demo import generate

    with Store(config.get("db_path")) as store:
        summary = generate(store, days=args.days, seed=args.seed)
    print("已產生合成示範資料：")
    for line in summary:
        print(f"  · {line}")
    print("\n注意：這是**合成資料**，不是真實市場行情，僅供離線檢視介面與管線。")
    print("執行 `twflow serve` 開啟儀表板。")
    return 0


# ---------- fixtures ----------

def cmd_fixtures(args, config: Config) -> int:
    from .synthfixtures import generate

    written = generate(config.get("fixture_dir", "fixtures"))
    print(f"已產生 {len(written)} 組合成 fixture：")
    for line in written:
        print(f"  · {line}")
    print("\n這些是**合成樣本**——欄位結構依公開文件推斷，數值為假。")
    print("在有外網的機器上執行 `twflow record` 會以真實回應覆蓋它們。")
    return 0


# ---------- entry ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="twflow", description="台股法人資金流向儀表板")
    p.add_argument("-c", "--config", default="config.yaml", help="設定檔路徑")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--mode", choices=["live", "fixture", "record"], help="覆寫抓取模式")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="檢查資料來源可達性與欄位結構")
    d.add_argument("--date", help="檢查用的交易日 YYYY-MM-DD")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("record", help="錄製真實回應到 fixtures/")
    r.add_argument("--date", help="交易日 YYYY-MM-DD")
    r.set_defaults(func=cmd_record)

    s = sub.add_parser("sync", help="匯入證券清單與產業別")
    s.set_defaults(func=cmd_sync)

    po = sub.add_parser("poll", help="盤中輪詢即時報價")
    po.add_argument("--once", action="store_true", help="只跑一輪就結束")
    po.add_argument("--ignore-session", action="store_true", help="忽略交易時段判斷")
    po.set_defaults(func=cmd_poll)

    e = sub.add_parser("eod", help="盤後抓官方數據並校準")
    e.add_argument("--date", help="交易日 YYYY-MM-DD，預設為上一個交易日")
    e.add_argument("--since", help="回補起始日 YYYY-MM-DD（改為區間模式）")
    e.add_argument("--until", help="回補結束日 YYYY-MM-DD，預設今天")
    e.set_defaults(func=cmd_eod)

    au = sub.add_parser("auto", help="盤中輪詢 + 收盤後自動跑盤後流程")
    au.set_defaults(func=cmd_auto)

    sv = sub.add_parser("serve", help="啟動網頁儀表板")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    sv.set_defaults(func=cmd_serve)

    fx = sub.add_parser("fixtures", help="產生合成 fixture 樣本")
    fx.set_defaults(func=cmd_fixtures)

    dm = sub.add_parser("demo", help="產生合成示範資料（離線檢視用）")
    dm.add_argument("--days", type=int, default=3, help="產生幾個交易日")
    dm.add_argument("--seed", type=int, default=20260827)
    dm.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _log_setup(args.verbose)
    config = Config.load(args.config)
    try:
        return args.func(args, config)
    except KeyboardInterrupt:
        print("\n已中斷。")
        return 130
    except TwflowError as exc:
        print(f"錯誤: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
