#!/bin/zsh -f
# chat_app（同じ Wi-Fi の2台で使う1対1チャットとファイル送信）をダブルクリックで起動する。
# このフォルダ（clone）の chat.py を動かす。受け取ったファイルはこのフォルダの received/ に入る（.gitignore で除外）。
# 最新にするときは update.command（git pull --ff-only）。このフォルダでは直さない。
set -u
USE_DIR="${0:A:h}"
if [[ ! -f "$USE_DIR/chat.py" ]]; then echo "コードが見つかりません: ${USE_DIR}/chat.py"; read; exit 1; fi
PYTHON=""
for cand in "$HOME/.pyenv/versions/3.11.8/bin/python3" "$(command -v python3 2>/dev/null)"; do
  if [[ -n "$cand" && -x "$cand" ]]; then PYTHON="$cand"; break; fi
done
if [[ -z "$PYTHON" ]]; then echo "python3 が見つかりませんでした。"; read; exit 1; fi
cd "$USE_DIR" || exit 1
"$PYTHON" chat.py --save-dir "$USE_DIR/received" "$@"
echo; echo "終了しました。このウィンドウは閉じてかまいません。"
