#!/bin/bash
cd -- "$(dirname -- "$0")" || exit 1
bash ./setup.command --ensure --launch "$@"
status=$?
if [ "$status" -ne 0 ]; then
  printf '\n準備または起動に失敗しました。上の説明を確認してください。Enterで閉じます。\n'
  read -r _
fi
exit "$status"
