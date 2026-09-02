#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

python3 -m unittest -v "$repo_dir/tests/test_helper.py"
python3 -m py_compile "$repo_dir/quick_ask_helper.py"

if rg -n 'StdioCollector|ask\.sh|omarchy-ask\.log|XDG_RUNTIME_DIR|execDetached' \
  "$repo_dir/Ask.qml" "$repo_dir/quick_ask_helper.py"; then
  echo "Forbidden legacy process or log pattern remains." >&2
  exit 1
fi

[[ $(rg -c 'Qt\.openUrlExternally' "$repo_dir/Ask.qml") -eq 1 ]]
rg -F 'stdinEnabled: true' "$repo_dir/Ask.qml" >/dev/null
rg -F 'maximumLength: root.maxUserCharacters' "$repo_dir/Ask.qml" >/dev/null
rg -F 'Text.PlainText' "$repo_dir/Ask.qml" >/dev/null
rg -F 'var match = candidate.match(/^(https?):\/\/' "$repo_dir/Ask.qml" >/dev/null

echo "quick-ask security tests passed"
