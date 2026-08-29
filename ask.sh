#!/usr/bin/env bash
# One-shot Q&A for the user's Omarchy default agent. Prints only the answer.
set -euo pipefail

# Quickshell leaves stdin as an open pipe; Codex (and others) wait on it forever.
exec </dev/null

export PATH="${PATH:-/usr/bin}:${HOME}/.local/share/mise/shims:${HOME}/.local/bin:/usr/local/bin:/usr/bin"

plugin_id="damianpoole.ask"
config_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/omarchy/plugin-settings"
config_file="${OMARCHY_ASK_CONFIG_FILE:-${config_dir}/${plugin_id}.json}"

have() { command -v "$1" >/dev/null 2>&1; }

default_agent() {
  omarchy-default-agent 2>/dev/null || true
}

get_settings() {
  local agent="$1"
  local model=""
  local reasoning=""

  if [[ -f $config_file ]]; then
    model=$(jq -r --arg agent "$agent" '.agents[$agent].model // ""' "$config_file" 2>/dev/null || true)
    reasoning=$(jq -r --arg agent "$agent" '.agents[$agent].reasoning // ""' "$config_file" 2>/dev/null || true)
  fi

  jq -cn \
    --arg agent "$agent" \
    --arg model "$model" \
    --arg reasoning "$reasoning" \
    '{agent: $agent, model: $model, reasoning: $reasoning}'
}

set_settings() {
  local agent="$1"
  local model="$2"
  local reasoning="$3"
  local source_file="$config_file"
  local temporary_source=""
  local next_file

  [[ -n $agent ]] || {
    echo "Cannot save settings without an agent." >&2
    exit 2
  }

  case "$reasoning" in
  "" | minimal | low | medium | high | xhigh | max | ultra) ;;
  *)
    echo "Unsupported reasoning level: $reasoning" >&2
    exit 2
    ;;
  esac

  mkdir -p "$config_dir"
  if [[ ! -f $source_file ]]; then
    temporary_source=$(mktemp)
    source_file="$temporary_source"
    printf '{"schemaVersion":1,"agents":{}}\n' >"$source_file"
  fi

  next_file=$(mktemp "${config_dir}/.${plugin_id}.XXXXXX")
  if ! jq \
    --arg agent "$agent" \
    --arg model "$model" \
    --arg reasoning "$reasoning" \
    '.schemaVersion = 1
      | .agents = (.agents // {})
      | .agents[$agent] = {model: $model, reasoning: $reasoning}' \
    "$source_file" >"$next_file"; then
    rm -f "$next_file" "$temporary_source"
    exit 1
  fi

  chmod 600 "$next_file"
  mv "$next_file" "$config_file"
  [[ -z $temporary_source ]] || rm -f "$temporary_source"
  get_settings "$agent"
}

case "${1:-}" in
--get-settings)
  agent="${2:-$(default_agent)}"
  get_settings "$agent"
  exit 0
  ;;
--set-settings)
  [[ $# -eq 4 ]] || {
    echo "Usage: ask.sh --set-settings <agent> <model-or-empty> <reasoning-or-empty>" >&2
    exit 2
  }
  set_settings "$2" "$3" "$4"
  exit 0
  ;;
--config-path)
  printf '%s\n' "$config_file"
  exit 0
  ;;
esac

if [[ ${PWD} == "${HOME}" && -d ${HOME}/Work ]]; then
  cd "${HOME}/Work"
elif [[ ! -d $PWD ]]; then
  cd "${HOME}/Work" 2>/dev/null || cd "${HOME}"
fi

prompt=${1:-}
if [[ -z $prompt ]]; then
  echo "Type a question first." >&2
  exit 2
fi

agent=$(default_agent)
if [[ -z $agent ]]; then
  echo "No default agent set. Pick one with: omarchy default agent" >&2
  exit 1
fi

settings=$(get_settings "$agent")
model=$(jq -r '.model' <<<"$settings")
reasoning=$(jq -r '.reasoning' <<<"$settings")

out=$(mktemp)
trap 'rm -f "$out"' EXIT

run_codex() {
  local bin="${HOME}/.local/share/mise/installs/codex/latest/bin/codex"
  local log="${XDG_RUNTIME_DIR:-/tmp}/omarchy-ask.log"
  local -a args=(exec)
  have "$bin" || bin=$(command -v codex)
  [[ -z $model ]] || args+=(-m "$model")
  [[ -z $reasoning ]] || args+=(-c "model_reasoning_effort=${reasoning}")
  args+=(--skip-git-repo-check --ephemeral -s read-only --output-last-message "$out" -- "$prompt")

  "$bin" "${args[@]}" >"$log" 2>&1 || {
    tail -n 30 "$log" >&2 || true
    exit 1
  }
  if [[ ! -s $out ]]; then
    echo "Codex returned no answer." >&2
    tail -n 30 "$log" >&2 || true
    exit 1
  fi
  cat "$out"
  echo
}

run_claude() {
  local -a args=(-p --output-format text)
  [[ -z $model ]] || args+=(--model "$model")
  claude "${args[@]}" -- "$prompt"
}

run_gemini() {
  local -a args=(-p "$prompt" --approval-mode plan)
  [[ -z $model ]] || args+=(--model "$model")
  gemini "${args[@]}"
}

run_opencode() {
  local -a args=(run --print-logs=false)
  [[ -z $model ]] || args+=(--model "$model")
  opencode "${args[@]}" -- "$prompt"
}

run_crush() {
  local -a args=(run)
  [[ -z $model ]] || args+=(--model "$model")
  crush "${args[@]}" "$prompt"
}

run_copilot() {
  local -a args=(-p "$prompt")
  [[ -z $model ]] || args+=(--model "$model")
  copilot "${args[@]}"
}

run_grok() {
  local -a args=(--print)
  [[ -z $model ]] || args+=(--model "$model")
  grok "${args[@]}" -- "$prompt"
}

run_pi() {
  local -a args=(--print)
  [[ -z $model ]] || args+=(--model "$model")
  pi "${args[@]}" -- "$prompt"
}

run_omp() {
  local -a args=(--print)
  [[ -z $model ]] || args+=(--model "$model")
  omp "${args[@]}" -- "$prompt"
}

case "$agent" in
codex) run_codex ;;
claude) run_claude ;;
gemini) run_gemini ;;
opencode) run_opencode ;;
crush) run_crush ;;
copilot) run_copilot ;;
grok) run_grok ;;
pi) run_pi ;;
omp) run_omp ;;
*)
  echo "Ask does not yet support one-shot answers from '$agent'." >&2
  echo "Set a supported default with: omarchy default agent" >&2
  exit 1
  ;;
esac
