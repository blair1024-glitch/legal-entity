"""共用 HTTP client：速率限制、重試、以及 live/fixture/record 三種模式.

本專案的一個核心限制是：開發環境連不到台股資料源（組織 egress policy 擋掉了
twse/tpex/taifex）。因此每個資料來源都透過這裡取得原始回應，並支援三種模式：

* ``live``    —— 真的打網路（使用者本機）
* ``fixture`` —— 從 ``fixtures/`` 讀取樣本，完全離線（CI 與本開發環境）
* ``record``  —— 打網路並把回應寫進 ``fixtures/``（使用者本機第一次跑）

這讓 parser 的測試可以離線進行，也讓使用者能用 ``twflow record`` 把真實回應
錄下來、重跑測試，快速抓出樣本結構與實作假設不符的地方。
"""

from __future__ import annotations

import hashlib
import json
import logging
import ssl
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .errors import FetchError, FixtureMissing

log = logging.getLogger(__name__)

# Python 3.13 起 ssl.create_default_context() 預設開啟 VERIFY_X509_STRICT，
# 嚴格要求憑證符合 RFC 5280。有些政府網站的憑證鏈裡，中繼憑證缺少
# Subject Key Identifier 之類的欄位，就會被擋下來——實測櫃買中心
# (www.tpex.org.tw) 就是這種情況，錯誤訊息是：
#
#     certificate verify failed: Missing Subject Key Identifier
#
# curl 與瀏覽器不做這項額外檢查，所以它們連得上、Python 連不上。
#
# 這裡的處理是：預設維持嚴格；只有在真的撞到這類「規範瑕疵」錯誤時，
# 才對該次請求改用放寬 STRICT 的連線並留下警告。
#
# 重要：放寬的只有 RFC 格式檢查。**憑證鏈驗證與主機名稱比對完全保留**，
# 等同 curl 與瀏覽器的驗證強度。這不是關閉 TLS 驗證。
_STRICTNESS_MARKERS = (
    "Subject Key Identifier",
    "Authority Key Identifier",
    "invalid CA certificate",
    "x509 strict",
)


def _is_strictness_failure(exc: Exception) -> bool:
    text = str(exc)
    return any(m.lower() in text.lower() for m in _STRICTNESS_MARKERS)


def _relaxed_ssl_context() -> ssl.SSLContext:
    """保留完整憑證驗證，只關掉 RFC 格式的嚴格檢查."""
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    # 明確保留：仍然驗證憑證鏈，仍然比對主機名稱
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class _RelaxedStrictnessAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = _relaxed_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 證交所會擋掉沒有 Referer 的請求，特別是 MIS 即時報價。
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}


class RateLimiter:
    """滑動視窗速率限制器：``max_calls`` 次 / ``period`` 秒.

    MIS 即時報價的實測限制大約是 3 requests / 5 秒，超過會被暫時擋掉，
    所以這裡預設對每個 host 分開限流。
    """

    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0])
            time.sleep(max(sleep_for, 0.01))


@dataclass
class Response:
    """統一的回應物件，不論來自網路或 fixture."""

    url: str
    status: int
    text: str
    from_fixture: bool = False

    def json(self) -> object:
        return json.loads(self.text)


# 這些參數不列入 fixture 檔名的雜湊。fixture 代表的是「這個端點的回應長什麼
# 樣子」，不是「它在某月某日、對某幾檔股票回傳了什麼」。
#
# 沒有這層處理的話：昨天錄的樣本今天就找不到（日期變了），而 MIS 每換一批
# 股票就要一個新樣本（36 批就是 36 份檔案）——兩者都讓離線模式難以使用。
VOLATILE_PARAM_KEYS = frozenset(
    {
        # 日期
        "date", "dayDate", "queryDate", "queryStartDate", "queryEndDate",
        "firstDate", "lastDate", "yy", "mm", "dd",
        # MIS 的查詢標的清單
        "ex_ch",
    }
)


def fixture_key(url: str, params: dict | None = None, body: dict | None = None) -> str:
    """把一個請求映射成穩定的 fixture 檔名.

    用 host + 路徑末段做人類可讀的前綴，再接請求內容的短雜湊確保唯一，
    這樣 ``fixtures/`` 目錄裡的檔案光看檔名就大致知道是哪個來源。

    雜湊會略過易變參數（見 :data:`VOLATILE_PARAM_KEYS`），讓同一個端點不論查哪一天、
    查哪幾檔股票，都對應同一個 fixture。
    """
    parsed = urllib.parse.urlparse(url)
    tail = (parsed.path.rstrip("/").rsplit("/", 1) or [""])[-1] or "root"
    host = parsed.netloc.replace(".", "_")

    def stable(d: dict | None) -> dict:
        return {k: v for k, v in (d or {}).items() if k not in VOLATILE_PARAM_KEYS}

    payload = json.dumps(
        {"url": url, "params": stable(params), "body": stable(body)},
        sort_keys=True,
        ensure_ascii=False,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    safe_tail = "".join(c if c.isalnum() or c in "-_" else "_" for c in tail)
    return f"{host}__{safe_tail}__{digest}"


@dataclass
class Fetcher:
    """所有資料來源共用的取得器.

    Parameters
    ----------
    mode:
        ``live`` / ``fixture`` / ``record``。
    fixture_dir:
        fixture 檔案存放目錄。
    rate_limits:
        每個 host 的速率限制，形如 ``{"mis.twse.com.tw": (3, 5.0)}``。
    """

    mode: str = "fixture"
    fixture_dir: Path = field(default_factory=lambda: Path("fixtures"))
    timeout: float = 20.0
    max_retries: int = 3
    rate_limits: dict[str, tuple[int, float]] = field(default_factory=dict)
    _limiters: dict[str, RateLimiter] = field(default_factory=dict, init=False)
    _session: requests.Session | None = field(default=None, init=False)
    # 已知憑證有規範瑕疵、需要放寬 STRICT 的 host
    _lenient_hosts: set[str] = field(default_factory=set, init=False)
    # 保留最近幾次的原始回應，讓 `doctor --dump` 能把伺服器實際吐回來的東西
    # 印出來。解析失敗時，這通常比錯誤訊息本身更能說明問題。
    recent: list[tuple[str, str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.mode not in {"live", "fixture", "record"}:
            raise ValueError(f"未知的 Fetcher mode: {self.mode!r}")
        self.fixture_dir = Path(self.fixture_dir)
        # 預設限流：MIS 最嚴，其餘盤後端點只是禮貌性節流。
        defaults = {
            "mis.twse.com.tw": (3, 5.0),
            "www.twse.com.tw": (5, 1.0),
            "openapi.twse.com.tw": (5, 1.0),
            "www.tpex.org.tw": (5, 1.0),
            "www.taifex.com.tw": (5, 1.0),
        }
        for host, limit in defaults.items():
            self.rate_limits.setdefault(host, limit)

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update(DEFAULT_HEADERS)
        return self._session

    def _limiter_for(self, url: str) -> RateLimiter | None:
        host = urllib.parse.urlparse(url).netloc
        if host not in self.rate_limits:
            return None
        if host not in self._limiters:
            max_calls, period = self.rate_limits[host]
            self._limiters[host] = RateLimiter(max_calls, period)
        return self._limiters[host]

    def _fixture_path(self, key: str) -> Path:
        return self.fixture_dir / f"{key}.txt"

    def get(
        self,
        url: str,
        params: dict | None = None,
        *,
        headers: dict | None = None,
        method: str = "GET",
        data: dict | None = None,
    ) -> Response:
        key = fixture_key(url, params, data)
        path = self._fixture_path(key)

        if self.mode == "fixture":
            if not path.exists():
                raise FixtureMissing(
                    f"缺少 fixture: {path}\n"
                    f"  url={url} params={params}\n"
                    f"  請在有外網的機器上執行 `twflow record` 產生樣本。"
                )
            text = path.read_text("utf-8")
            self._remember(url, text)
            return Response(url=url, status=200, text=text, from_fixture=True)

        resp = self._fetch_live(url, params, headers, method, data)
        self._remember(resp.url, resp.text)

        if self.mode == "record":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(resp.text, encoding="utf-8")
            meta = self.fixture_dir / f"{key}.meta.json"
            meta.write_text(
                json.dumps(
                    {
                        "url": url,
                        "params": params or {},
                        "method": method,
                        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "status": resp.status,
                        "synthetic": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return resp

    def _remember(self, url: str, text: str, keep: int = 12) -> None:
        self.recent.append((url, text))
        del self.recent[:-keep]

    def _fetch_live(
        self,
        url: str,
        params: dict | None,
        headers: dict | None,
        method: str,
        data: dict | None,
    ) -> Response:
        limiter = self._limiter_for(url)
        last_err: Exception | None = None

        for attempt in range(self.max_retries):
            if limiter is not None:
                limiter.acquire()
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_err = FetchError(f"連線逾時（{self.timeout} 秒）")
                last_err.__cause__ = exc
            except requests.exceptions.SSLError as exc:
                host = urllib.parse.urlparse(url).netloc
                if _is_strictness_failure(exc) and host not in self._lenient_hosts:
                    # 憑證本身沒問題，只是不合 RFC 的格式要求。記下這個 host
                    # 並重試——下一圈迴圈會用放寬 STRICT 的連線。
                    log.warning(
                        "%s 的憑證鏈有規範瑕疵（%s），改用放寬 RFC 檢查的連線重試。"
                        "憑證鏈與主機名稱仍然完整驗證。",
                        host,
                        str(exc).split("certificate verify failed:")[-1].strip()[:60],
                    )
                    self._lenient_hosts.add(host)
                    self.session.mount(f"https://{host}/", _RelaxedStrictnessAdapter())
                    continue
                # 必須擋在 ConnectionError 前面——SSLError 是它的子類，
                # 順序寫反會把憑證問題誤報成「網路不通」，而兩者的處理
                # 方式完全不同（一個要裝憑證，一個是等網路恢復）。
                last_err = FetchError(
                    f"TLS 憑證驗證失敗：{exc}\n"
                    f"    curl 能連但這裡不行，通常是 Python 的憑證庫沒有該站的根憑證。\n"
                    f"    macOS 解法：執行 /Applications/Python\\ 3.x/Install\\ Certificates.command\n"
                    f"    或 pip install --upgrade certifi"
                )
                last_err.__cause__ = exc
            except requests.ConnectionError as exc:
                last_err = FetchError(f"連不上伺服器：{exc}")
                last_err.__cause__ = exc
            except requests.RequestException as exc:
                last_err = exc
            else:
                # 429/5xx 值得重試；4xx 其餘直接失敗，重試也不會變好。
                if resp.status_code == 200:
                    return Response(url=resp.url, status=resp.status_code, text=resp.text)
                # 把狀態碼的意思講出來——429 和 503 的處理方式完全不同
                meaning = {
                    403: "被拒絕（可能是缺 Referer 或被封鎖）",
                    404: "端點不存在（網址可能改版了）",
                    429: "請求太密集被限流",
                    500: "伺服器內部錯誤",
                    503: "伺服器暫時無法服務（維護中或過載）",
                }.get(resp.status_code, "")
                last_err = FetchError(
                    f"HTTP {resp.status_code}" + (f"（{meaning}）" if meaning else "")
                )
                if resp.status_code < 500 and resp.status_code != 429:
                    raise last_err

            if attempt < self.max_retries - 1:
                time.sleep(2**attempt)

        # 訊息要帶上最後一次失敗的原因。只說「重試 3 次仍失敗」會讓人分不清
        # 是自己網路的問題、對方限流、還是端點根本不存在——三者處理方式不同。
        raise FetchError(
            f"{last_err}　（重試 {self.max_retries} 次後放棄）\n"
            f"    {url}"
        ) from last_err
