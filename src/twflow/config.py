"""設定載入.

預設值全部寫在 ``DEFAULTS``，``config.yaml`` 只需覆寫想改的項目。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "db_path": "data/twflow.db",
    "fixture_dir": "fixtures",
    "sectors_file": "sectors.yaml",
    "mode": "fixture",          # live / fixture / record
    "markets": ["TWSE", "TPEX"],
    "poll": {
        "interval_seconds": 60,   # 全市場掃一輪的目標週期
        "batch_size": 50,         # 單次 MIS 請求帶幾檔（用 | 分隔）
        "universe": "all",        # all | watchlist
    },
    "flow": {
        # 爆量門檻（張）：單一輪詢區間成交量超過此值即計入 burst 統計。
        # 註：這不是「單筆大單」——快照資料無法辨識個別委託，只能辨識爆量區間。
        "burst_lot_threshold": 100,
        # 成交價落在買賣價中間時的分配方式：proportional | midpoint_neutral
        "midpoint_rule": "proportional",
        # 是否套用盤後迴歸得出的每檔校準係數
        "apply_calibration": True,
    },
    "quadrant": {
        # 動能視窗（分鐘）：比較「近 W 分鐘」與「前 W 分鐘」的資金流強度
        "momentum_window_minutes": 30,
        # 板塊至少要有幾檔成分股才進入四象限圖，避免單檔雜訊主導
        "min_constituents": 2,
        # 板塊成交值下限（元），過濾冷門板塊
        "min_turnover": 10_000_000,
    },
    "watchlist": ["2330", "2317", "2454", "2308", "3231"],
    "retention_days": 30,
    "server": {"host": "127.0.0.1", "port": 8000},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))

    @classmethod
    def load(cls, path: str | Path | None = "config.yaml") -> "Config":
        data = dict(DEFAULTS)
        if path:
            p = Path(path)
            if p.exists():
                loaded = yaml.safe_load(p.read_text("utf-8")) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"{p} 的內容必須是一個 mapping")
                data = _deep_merge(data, loaded)
        return cls(data)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def __getitem__(self, key: str) -> Any:
        return self.data[key]
