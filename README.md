# chat_app

TCP ソケットの最小チャットサーバ（`server.py`、標準ライブラリのみ）と Hello World の `index.html`。データ無し。
起動: `python3 server.py --port <空きポート>`。クライアントは `nc 127.0.0.1 <ポート>` で接続し、1行目に名前、以降はメッセージ（`/quit` で退出）。
起動口は `app/dev_tools/chat_app/start.command`。利用データは無いので `app/dev_tools/chat_app/` には起動口と README しか置かない。
