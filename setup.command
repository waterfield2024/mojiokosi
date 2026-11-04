#!/bin/bash
set -e
cd -- "$(dirname -- "$0")"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
  printf 'Apple SiliconのMacで実行してください。Terminalの「Rosettaを使用して開く」はオフにしてください。\n'
  exit 1
fi
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
if ! command -v brew >/dev/null; then
  printf '初回だけHomebrewを導入してください: https://brew.sh/ja/\n導入後、start.commandをもう一度開いてください。手順はdocs/Mac.mdにあります。\n'
  exit 1
fi
# No brew services, system audio changes or permissions are enabled automatically.
for package in python@3.12 python-tk@3.12 ffmpeg node ollama; do
  if ! brew list --versions "$package" >/dev/null 2>&1; then
    brew install "$package"
  fi
done
python="$(brew --prefix python@3.12)/bin/python3.12"
"$python" -X utf8 setup_mac.py "$@"
