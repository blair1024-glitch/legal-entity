#!/usr/bin/env sh
# 重新產生 lock 檔。
#
# 一定要用 --python-version 指定**最低支援版本**（見 pyproject.toml 的
# requires-python），而不是你手邊那個直譯器的版本。
#
# 原因：uv 預設對當前 Python 解析，在 3.12 上解出來的 lock 可能釘到只支援
# 3.11+ 的套件，於是 3.10 的環境根本裝不起來。這個坑踩過一次了
# （websockets 17.1 requires >=3.11，CI 的 3.10 job 直接掛掉）。
set -eu

MIN_PY=3.10

command -v uv >/dev/null 2>&1 || {
  echo "找不到 uv。安裝方式：pip install uv" >&2
  exit 1
}

cd "$(dirname "$0")/.."

echo "以 Python ${MIN_PY} 解析相依…"
uv pip compile pyproject.toml \
  --python-version "${MIN_PY}" \
  -o requirements.lock
uv pip compile pyproject.toml requirements-dev.txt \
  --python-version "${MIN_PY}" \
  -o requirements-dev.lock

echo
echo "完成。請務必重跑測試確認新版本沒有破壞什麼："
echo "  pip install -r requirements-dev.lock && pip install -e . --no-deps && pytest"
