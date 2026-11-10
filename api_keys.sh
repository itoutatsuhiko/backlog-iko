#!/usr/bin/env bash
# macOS bash / zsh: run `source ./api_keys.sh` in the terminal used for migration.
# Values are read without echo; never put API keys into this file or chat messages.
printf '移行元のAPIキー（入力は表示されません）: '
read -r -s BACKLOG_SRC_API_KEY
printf '\n移行先のAPIキー（入力は表示されません）: '
read -r -s BACKLOG_DST_API_KEY
printf '\n'
export BACKLOG_SRC_API_KEY BACKLOG_DST_API_KEY
if [ -z "$BACKLOG_SRC_API_KEY" ] || [ -z "$BACKLOG_DST_API_KEY" ]; then
  printf 'APIキーが空です。source ./api_keys.sh を再実行してください。\n'
else
  printf 'このターミナルの環境変数に設定しました。ファイルには保存していません。\n'
fi
