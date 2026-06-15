#!/usr/bin/env bash

devloop_read_config_value() {
    local project_dir="${1:-}" key="${2:-}"
    [ -z "$project_dir" ] || [ -z "$key" ] && return 0
    grep "^${key}=" "$project_dir/.devloop" 2>/dev/null | cut -d= -f2- | tail -1
}

devloop_claude_disabled() {
    [ "${DEVLOOP_DISABLE_CLAUDE:-}" = "1" ] || [ -f "$HOME/.claude/.devloop-disable-claude" ]
}

devloop_ai_provider() {
    local project_dir="${1:-}"
    local provider="${DEVLOOP_AI_PROVIDER:-}"
    [ -z "$provider" ] && [ -n "$project_dir" ] && provider="$(devloop_read_config_value "$project_dir" AI_PROVIDER)"
    if [ -z "$provider" ] && devloop_claude_disabled; then
        provider="codex"
    fi
    echo "${provider:-claude}"
}

devloop_ai_model() {
    local project_dir="${1:-}" role="${2:-impl}" fallback="${3:-sonnet}" provider
    provider="$(devloop_ai_provider "$project_dir")"
    if [ "$provider" = "codex" ]; then
        local key val
        [ "$role" = "pm" ] && key="CODEX_MODEL_PM" || key="CODEX_MODEL_IMPL"
        val="${DEVLOOP_CODEX_MODEL:-}"
        [ -z "$val" ] && [ -n "$project_dir" ] && val="$(devloop_read_config_value "$project_dir" "$key")"
        echo "$val"
    else
        echo "$fallback"
    fi
}

devloop_ai_print() {
    local project_dir="$1" role="$2" model="$3" prompt="$4"
    local provider provider_model
    provider="$(devloop_ai_provider "$project_dir")"
    provider_model="$(devloop_ai_model "$project_dir" "$role" "$model")"
    if [ "$provider" = "claude" ]; then
        if devloop_claude_disabled; then
            echo "Claude disabled locally. Use AI_PROVIDER=codex or remove ~/.claude/.devloop-disable-claude." >&2
            return 127
        fi
        command -v claude >/dev/null 2>&1 || return 127
        claude --model "$model" --dangerously-skip-permissions -p "$prompt"
        return $?
    fi

    command -v codex >/dev/null 2>&1 || return 127
    local args=(codex --ask-for-approval never exec -C "${project_dir:-$(pwd)}" --sandbox workspace-write --color never)
    [ -n "$provider_model" ] && args+=(--model "$provider_model")
    "${args[@]}" "$prompt"
}
