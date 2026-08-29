#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT

export HOME="$test_root/home"
export XDG_CONFIG_HOME="$test_root/config"
export XDG_RUNTIME_DIR="$test_root/runtime"
export OMARCHY_ASK_CONFIG_FILE="$test_root/config/quick-ask-test.json"
export CAPTURE_FILE="$test_root/args.txt"
export PATH="$test_root/bin:/usr/bin"

mkdir -p \
  "$HOME/.local/share/mise/installs/codex/latest/bin" \
  "$XDG_CONFIG_HOME" \
  "$XDG_RUNTIME_DIR" \
  "$test_root/bin"

cat >"$test_root/bin/omarchy-default-agent" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "${MOCK_AGENT:-codex}"
MOCK

cat >"$HOME/.local/share/mise/installs/codex/latest/bin/codex" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$CAPTURE_FILE"
while (( $# > 0 )); do
  if [[ $1 == --output-last-message ]]; then
    printf 'MOCK_ANSWER\n' >"$2"
    break
  fi
  shift
done
MOCK

cat >"$test_root/bin/gemini" <<'MOCK'
#!/usr/bin/env bash
printf '%s\n' "$@" >"$CAPTURE_FILE"
printf 'GEMINI_ANSWER\n'
MOCK

chmod +x \
  "$test_root/bin/omarchy-default-agent" \
  "$HOME/.local/share/mise/installs/codex/latest/bin/codex" \
  "$test_root/bin/gemini"

settings=$(bash "$repo_dir/ask.sh" --get-settings codex)
[[ $(jq -r '.model' <<<"$settings") == "" ]]
[[ $(jq -r '.reasoning' <<<"$settings") == "" ]]

bash "$repo_dir/ask.sh" --set-settings codex gpt-5.6-luna low >/dev/null
settings=$(bash "$repo_dir/ask.sh" --get-settings codex)
[[ $(jq -r '.model' <<<"$settings") == gpt-5.6-luna ]]
[[ $(jq -r '.reasoning' <<<"$settings") == low ]]

answer=$(bash "$repo_dir/ask.sh" "test question")
[[ $answer == MOCK_ANSWER ]]
grep -Fx -- -m "$CAPTURE_FILE" >/dev/null
grep -Fx -- gpt-5.6-luna "$CAPTURE_FILE" >/dev/null
grep -Fx -- model_reasoning_effort=low "$CAPTURE_FILE" >/dev/null

bash "$repo_dir/ask.sh" --set-settings codex "" "" >/dev/null
bash "$repo_dir/ask.sh" "agent default question" >/dev/null
if grep -Fx -- -m "$CAPTURE_FILE" >/dev/null; then
  echo "Agent-default mode unexpectedly passed a model." >&2
  exit 1
fi
if grep -F -- model_reasoning_effort= "$CAPTURE_FILE" >/dev/null; then
  echo "Agent-default mode unexpectedly passed a reasoning level." >&2
  exit 1
fi

export MOCK_AGENT=gemini
bash "$repo_dir/ask.sh" --set-settings gemini gemini-2.5-flash "" >/dev/null
answer=$(bash "$repo_dir/ask.sh" "gemini question")
[[ $answer == GEMINI_ANSWER ]]
grep -Fx -- --model "$CAPTURE_FILE" >/dev/null
grep -Fx -- gemini-2.5-flash "$CAPTURE_FILE" >/dev/null

echo "quick-ask tests passed"
