#!/bin/zsh
# このフォルダ（app 側の clone）に、origin でコミットされた最新を取り込む。全 app 共通の中身。
# ここでは直さない・コミットしない。直すのは開発側で、コミットしてからこれを開く。
# 利用データは .gitignore で外してあるので、pull で消えたり書き換わったりしない。
set -eu
cd "${0:A:h}"

if [[ ! -d .git ]]; then
  echo "ここは git の clone ではありません: $PWD"
  echo "Return で閉じる"; read; exit 1
fi

changed="$(git status --porcelain --untracked-files=no)"
if [[ -n "$changed" ]]; then
  echo "このフォルダで、追跡中のファイルが直接書き換えられています。取り込みを止めました。"
  echo "$changed"
  echo "開発側で直してコミットし、ここは git checkout -- <ファイル> で戻してください。"
  echo "Return で閉じる"; read; exit 1
fi

git pull --ff-only
git log --oneline -1
