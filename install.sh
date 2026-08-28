#!/usr/bin/env bash
# 一鍵安裝。在專案資料夾裡執行：  ./install.sh
#
# 做的事：檢查 Python 版本 → 建立虛擬環境 → 安裝套件 → 跑測試 → 建立設定檔。
# 每一步都會說明現在在做什麼，失敗時直接講怎麼修，不留下半殘狀態。

set -e

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'

say()  { printf "\n%s▸ %s%s\n" "$BOLD" "$1" "$OFF"; }
ok()   { printf "  %s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
warn() { printf "  %s!%s %s\n" "$YELLOW" "$OFF" "$1"; }
die()  { printf "\n%s✗ %s%s\n\n%s\n\n" "$RED" "$1" "$OFF" "$2"; exit 1; }

cd "$(dirname "$0")"

# --- 確認位置對 ---
[ -f pyproject.toml ] || die "找不到 pyproject.toml" \
  "請確認你在專案資料夾裡執行這個腳本。"

# --- 1. Python ---
say "檢查 Python"

command -v python3 >/dev/null 2>&1 || die "沒有找到 python3" \
"請到 https://www.python.org/downloads/ 下載安裝（點兩下 .pkg 即可），
裝完把終端機關掉重開，再跑一次 ./install.sh"

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info[:2] >= (3,10) else 0)')

if [ "$PY_OK" != "1" ]; then
  die "Python 版本太舊：$PY_VER（需要 3.10 以上）" \
"macOS 內建的通常是 3.9，要另外安裝新版：
  1. 到 https://www.python.org/downloads/ 下載並安裝
  2. 把終端機關掉重開（很重要，否則還是舊版）
  3. 如果已經有 .venv 資料夾，先刪掉：rm -rf .venv
  4. 再跑一次 ./install.sh"
fi
ok "Python $PY_VER"

# --- 2. 虛擬環境 ---
say "建立獨立環境"
if [ -d .venv ]; then
  warn "已存在 .venv，沿用它（想重來就先 rm -rf .venv）"
else
  python3 -m venv .venv
  ok "建立完成"
fi

# 這個腳本自己啟用虛擬環境即可；使用者的終端機要另外 source（最後會提示）
# shellcheck disable=SC1091
. .venv/bin/activate

# --- 3. 套件 ---
say "安裝套件（第一次約需 1–2 分鐘）"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-dev.lock
python -m pip install --quiet -e . --no-deps
ok "安裝完成"

# --- 4. 測試 ---
say "驗證安裝"
if python -m pytest -q 2>&1 | tail -3; then
  ok "測試通過"
else
  die "測試沒過" "把上面的訊息複製下來，這通常代表安裝過程有東西沒裝好。"
fi

# --- 5. 設定檔 ---
say "設定檔"
if [ -f config.yaml ]; then
  warn "config.yaml 已存在，保留你原本的設定"
else
  cp config.example.yaml config.yaml
  ok "已建立 config.yaml（mode 預設為 live，會實際連線證交所）"
fi

# --- 完成 ---
cat <<DONE

${GREEN}${BOLD}安裝完成。${OFF}

${BOLD}怎麼用：在這個資料夾裡下 ./twflow 指令即可${OFF}
${DIM}（不需要啟用虛擬環境，./twflow 會自己找到對的 Python）${OFF}

${BOLD}第一件該做的事——確認連得上證交所：${OFF}

    ./twflow --mode live doctor

${BOLD}想先看畫面（用假資料，不連網）：${OFF}

    ./twflow demo --days 5
    ./twflow serve      ${DIM}# 然後瀏覽器開 127.0.0.1:8000${OFF}

${DIM}下次開終端機記得先切回這個資料夾：
    cd $(pwd)${OFF}

DONE
