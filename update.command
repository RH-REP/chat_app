#!/bin/zsh
# このフォルダ（app 側の clone）に、origin でコミットされた最新を取り込む。全 app 共通の中身
# （正本は shared_modules の部品 update_command。各 app には写しを置く）。
# ここでは直さない・コミットしない。直すのは開発側で、コミットしてからこれを開く。
# 利用データは .gitignore で外してあるので、pull で消えたり書き換わったりしない。
#
# 使い方:
#   update.command          手で開く。取り込めなければ理由を出して止まる（Return で閉じる）。
#                           取り込んだあと、app に実行できる after_update.sh があればそれを動かす。
#   update.command --auto   start.command が起動の前に呼ぶ。取り込めなくても（オフライン・早送りできない・
#                           追跡中のファイルが書き換わっている・時間切れ）知らせを出して 0 で終わり、
#                           start.command は今の版のまま起動を続ける。after_update.sh は動かさない（これから起動するので）。
#                           開発側の repo（フォルダの道筋に app_dev を含む）と、APP_NO_UPDATE=1 のときは何もしない
#                           （試験で開発側の start.command を回しても pull しないため）。
set -u
cd "${0:A:h}"
AUTO=0
[[ "${1:-}" == --auto ]] && AUTO=1
# 取り込みを待つ秒数の上限。起動の前は短く（既定 10 秒）、手で開いたときは長く待つ
if (( AUTO )); then LIMIT="${APP_UPDATE_TIMEOUT:-10}"; else LIMIT="${APP_UPDATE_TIMEOUT:-300}"; fi

# 取り込めなかったときの知らせ。手で開いたときは止まり、--auto のときは知らせて続ける
fail() {
  if (( AUTO )); then
    echo "最新を取り込めませんでした（今の版で起動します）: $1"
    [[ $# -gt 1 ]] && print -r -- "$2"
    if [[ "$(uname)" == Darwin ]]; then
      osascript -e 'on run a' -e 'display notification (item 2 of a) with title (item 1 of a)' -e 'end run' \
        "${PWD:t}: 最新を取り込めませんでした" "今の版で起動します。$1" >/dev/null 2>&1 &!
    fi
    exit 0
  fi
  echo "$1"
  [[ $# -gt 1 ]] && print -r -- "$2"
  echo "Return で閉じる"; read; exit 1
}

if (( AUTO )); then
  [[ -n "${APP_NO_UPDATE:-}" ]] && exit 0
  [[ "$PWD/" == */app_dev/* ]] && exit 0      # 開発側の repo では pull しない
fi

[[ -d .git ]] || fail "ここは git の clone ではありません: $PWD"

changed="$(git status --porcelain --untracked-files=no 2>&1)"
if [[ -n "$changed" ]]; then
  fail "このフォルダで、追跡中のファイルが直接書き換えられています。取り込みを止めました。
開発側で直してコミットし、ここは git checkout -- <ファイル> で戻してください。" "$changed"
fi

before="$(git rev-parse HEAD 2>/dev/null)"
out="${TMPDIR:-/tmp}/app_update.$$"
# 認証を尋ねて止まらないようにし、つなぎに時間をかけない
GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=5}" \
  git -c advice.diverging=false -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=5 pull --ff-only -q >"$out" 2>&1 &
pid=$!
# 上限を過ぎたら止める（git は TERM を受けると .git の lock を片付けて終わる）。git が先に終われば見張りもすぐ終わる。
# pull は fetch・ssh などの子を立てるので、子から順に止める（親だけ止めると子が残る）
killtree() { local c; for c in $(pgrep -P $1 2>/dev/null); do killtree $c; done; kill -TERM $1 2>/dev/null; }
( repeat $(( LIMIT * 5 )); do kill -0 $pid 2>/dev/null || exit 0; sleep 0.2; done; killtree $pid ) &!
wait $pid
rc=$?
msg="$(<"$out")"; rm -f "$out"
if (( rc == 143 )); then
  fail "${LIMIT} 秒で終わらなかったので止めました（つながらない・遅いかもしれません）。" "$msg"
elif (( rc != 0 )); then
  fail "git pull --ff-only が失敗しました（オフライン、または早送りできない）。" "$msg"
fi

after="$(git rev-parse HEAD)"
if [[ "$before" == "$after" ]]; then
  echo "最新です: $(git log -1 --format='%h %s')"
else
  echo "取り込みました: $(git log -1 --format='%h %s')"
fi

if (( ! AUTO )) && [[ -x ./after_update.sh ]]; then
  ./after_update.sh
fi
exit 0
