#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

python3 -m unittest discover -v -s "$repo_dir/tests" -p 'test_*.py'
python3 -m py_compile "$repo_dir/quick_ask_helper.py" "$repo_dir/scripts/bindings.py"

if rg -n 'StdioCollector|ask\.sh|omarchy-ask\.log|XDG_RUNTIME_DIR|execDetached' \
  "$repo_dir/Ask.qml" "$repo_dir/quick_ask_helper.py" "$repo_dir/scripts/bindings.py"; then
  echo "Forbidden legacy process or log pattern remains." >&2
  exit 1
fi

[[ $(rg -c 'Qt\.openUrlExternally' "$repo_dir/Ask.qml") -eq 1 ]]
rg -F 'stdinEnabled: true' "$repo_dir/Ask.qml" >/dev/null
rg -F 'maximumLength: root.maxUserCharacters' "$repo_dir/Ask.qml" >/dev/null
rg -F 'Text.PlainText' "$repo_dir/Ask.qml" >/dev/null
rg -F 'var match = candidate.match(/^(https?):\/\/' "$repo_dir/Ask.qml" >/dev/null
rg -F 'os.O_NOFOLLOW' "$repo_dir/scripts/bindings.py" >/dev/null
rg -F 'RENAME_EXCHANGE' "$repo_dir/scripts/bindings.py" >/dev/null
rg -F 'hyprctl", "configerrors' "$repo_dir/scripts/bindings.py" >/dev/null
rg -F -- '--ignore-user-config' "$repo_dir/quick_ask_helper.py" >/dev/null
rg -F -- '--restricted' "$repo_dir/quick_ask_helper.py" >/dev/null
rg -F 'environment=_agent_environment' "$repo_dir/quick_ask_helper.py" >/dev/null

if rg -n 'subprocess\.run|capture_output|\.read_text\(|\.write_text\(' \
  "$repo_dir/scripts/bindings.py"; then
  echo "Binding helper contains an unbounded subprocess or path-based file operation." >&2
  exit 1
fi

echo "quick-ask security tests passed"
