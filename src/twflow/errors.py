"""共用例外型別."""


class TwflowError(Exception):
    """本專案所有例外的基底."""


class FetchError(TwflowError):
    """抓取資料失敗（網路、HTTP 狀態碼、逾時）."""


class FixtureMissing(TwflowError):
    """fixture 模式下找不到對應的樣本檔."""


class ParseError(TwflowError):
    """回應內容無法解析成預期結構.

    帶上來源名稱與觀察到的實際結構，方便 `twflow doctor` 印出可讀的診斷。
    """

    def __init__(self, source: str, message: str, observed: object = None):
        self.source = source
        self.observed = observed
        super().__init__(f"[{source}] {message}")
