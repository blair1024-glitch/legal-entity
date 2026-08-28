"""板塊分類與彙總.

分類兩層：官方產業別（自動抓、可靠）作為底層，``sectors.yaml`` 定義的
細分板塊覆蓋在上面。沒有被細分板塊涵蓋的個股會沿用官方產業別，
所以每一檔都保證有板塊歸屬，儀表板不會出現「未分類」的黑洞。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class SectorMap:
    """個股 → 板塊的對照表."""

    by_code: dict[str, str] = field(default_factory=dict)
    source: dict[str, str] = field(default_factory=dict)   # code -> custom / official
    state_brokers: tuple[str, ...] = ()

    @classmethod
    def load(
        cls,
        path: str | Path = "sectors.yaml",
        securities: list | None = None,
    ) -> "SectorMap":
        """載入板塊定義.

        Parameters
        ----------
        securities:
            資料庫裡的 ``securities`` 列（需有 ``code`` 與 ``industry``），
            用來為未被自訂板塊涵蓋的個股補上官方產業別。
        """
        by_code: dict[str, str] = {}
        src: dict[str, str] = {}
        brokers: tuple[str, ...] = ()

        p = Path(path)
        if p.exists():
            data = yaml.safe_load(p.read_text("utf-8")) or {}
            for sector, codes in (data.get("sectors") or {}).items():
                for code in codes or []:
                    code = str(code).strip()
                    if not code:
                        continue
                    # 先定義的優先，避免同一檔被兩個板塊搶走時結果不穩定
                    by_code.setdefault(code, str(sector))
                    src.setdefault(code, "custom")
            brokers = tuple(str(b) for b in (data.get("state_brokers") or []))

        for row in securities or []:
            code = row["code"] if not isinstance(row, dict) else row.get("code", "")
            industry = (
                row["industry"] if not isinstance(row, dict) else row.get("industry", "")
            )
            if code and code not in by_code:
                by_code[code] = industry or "未分類"
                src[code] = "official"

        return cls(by_code=by_code, source=src, state_brokers=brokers)

    def sector_of(self, code: str) -> str:
        return self.by_code.get(code, "未分類")

    def is_custom(self, code: str) -> bool:
        return self.source.get(code) == "custom"

    def members(self, sector: str) -> list[str]:
        return [c for c, s in self.by_code.items() if s == sector]

    def sectors(self) -> list[str]:
        return sorted(set(self.by_code.values()))


@dataclass
class SectorFlow:
    """單一板塊在某個時間切片的資金流彙總."""

    sector: str
    net_value: float = 0.0
    burst_net_value: float = 0.0
    turnover_value: float = 0.0
    constituents: int = 0
    custom: bool = False

    @property
    def strength(self) -> float:
        """淨流入佔該板塊成交值的比重，範圍約 [-1, 1].

        用比重而非絕對金額，是為了讓大小板塊可比——否則四象限圖永遠只有
        台積電所屬的板塊在動，其他板塊全部擠在原點。
        """
        if self.turnover_value <= 0:
            return 0.0
        return self.net_value / self.turnover_value


def aggregate_by_sector(
    flow_rows: list,
    sector_map: SectorMap,
    *,
    calibration: dict[str, float] | None = None,
) -> dict[str, SectorFlow]:
    """把逐檔的每分鐘資金流彙總成板塊資金流.

    Parameters
    ----------
    flow_rows:
        ``flow_minute`` 的列（``sqlite3.Row`` 或 dict 皆可）。
    calibration:
        每檔的校準係數（來自盤後迴歸）。有值時套用在推估的淨流上，
        讓歷史上系統性高估／低估的個股被修正。成交值不套用係數——
        成交值是實測值，不是推估值。
    """
    calibration = calibration or {}
    out: dict[str, SectorFlow] = {}
    seen: dict[str, set[str]] = {}

    for row in flow_rows:
        get = row.get if isinstance(row, dict) else row.__getitem__
        code = get("code")
        sector = sector_map.sector_of(code)
        coef = calibration.get(code, 1.0)

        rec = out.setdefault(
            sector, SectorFlow(sector=sector, custom=sector_map.is_custom(code))
        )
        rec.net_value += get("net_value") * coef
        rec.burst_net_value += get("burst_net_value") * coef
        rec.turnover_value += get("turnover_value")

        seen.setdefault(sector, set()).add(code)

    for sector, codes in seen.items():
        out[sector].constituents = len(codes)

    return out
