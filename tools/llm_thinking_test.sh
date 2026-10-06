#!/usr/bin/env bash
# =============================================================================
# mpiio_evolve -- probe the LLM endpoint for the thinking/reasoning switch
#
# Answers ONE question: does the OpenAI-compatible server on ccdev0022
# honor a "disable thinking" request, and in which spelling?  Iterations
# fail with 'No valid code found' + completion_tokens pinned at max_tokens
# because the Qwen reasoning burst eats the whole budget; three probes:
#
#   A) baseline                       (no switch -- expected: rambles/cap)
#   B) chat_template_kwargs.enable_thinking=false   (Qwen template kwarg)
#   C) chat_template_kwargs.thinking=false          (llama.cpp alias)
#
# A switch WORKS when that probe's completion_tokens collapses (tens of
# tokens) and finish_reason is 'stop' instead of 'length'.
#
# Usage:
#   ./tools/llm_thinking_test.sh
#   LLM_URL=http://host:8781/v1 LLM_MODEL=qwen3 ./tools/llm_thinking_test.sh
#
# Submits NOTHING to Slurm; talks to the LLM endpoint only.
# =============================================================================
set -uo pipefail

LLM_URL="${LLM_URL:-http://ccdev0022.hpc.gsi.de:8781/v1}"
LLM_MODEL="${LLM_MODEL:-/tmp/Qwen_Qwen3.8-Flash-Next_UD-Q4_K_XL-00001-of-00004.gguf}"
LLM_KEY="${OPENAI_API_KEY:-unused}"
MAXTOK="${LLM_MAX_TOKENS:-512}"
PROMPT="What is 17*23? Answer with just the number."

# The Squid on the login nodes must be bypassed (same rule as the
# controller): without this, probes can be refused with Squid 403 HTML
# pages instead of reaching the LLM endpoint.
_hn="${LLM_URL#*://}"; _hn="${_hn%%/*}"
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$_hn,localhost,127.0.0.1"
export no_proxy="$NO_PROXY"

command -v curl >/dev/null || { echo "ERROR: curl not found" >&2; exit 2; }
PY=python3

probe() {  # probe NAME EXTRA_JSON   (EXTRA_JSON = body fragment or empty)
    local name="$1" extra="${2:-}" body t0 t1 http out
    body=$(cat <<EOF
{"model": "$LLM_MODEL",
 "messages": [{"role": "user", "content": "$PROMPT"}],
 "max_tokens": $MAXTOK$( [[ -n "$extra" ]] && echo ", $extra")}
EOF
)
    t0=$(date +%s)
    out=$(curl -sS -w '\n%{http_code}' --max-time 300 \
              -H "Content-Type: application/json" -H "Authorization: Bearer $LLM_KEY" \
              -X POST "$LLM_URL/chat/completions" -d "$body" 2>&1)
    local rc=$?; t1=$(date +%s)
    if [[ $rc -ne 0 ]]; then
        printf '%-16s CONNECT ERROR (curl rc=%s): %s\n' "$name" "$rc" "$(tail -c 200 <<<"$out")"
        return 1
    fi
    http="${out##*$'\n'}"
    out="${out%$'\n'*}"
    if [[ "$http" != "200" ]]; then
        printf '%-16s HTTP %s: %s\n' "$name" "$http" "$(head -c 300 <<<"$out")"
        return 1
    fi
    # Parse: completion_tokens, finish_reason, first 160 chars of content
    # (response travels via env: stdin is occupied by the heredoc script)
    LLM_JSON="$out" "$PY" - "$name" "$((t1-t0))" <<'PYEOF'
import json, os, sys
name, dt = sys.argv[1], sys.argv[2]
d = json.loads(os.environ["LLM_JSON"])
ch = (d.get("choices") or [{}])[0]
msg = ch.get("message") or {}
tok = (d.get("usage") or {}).get("completion_tokens", "?")
fin = ch.get("finish_reason", "?")
content = (msg.get("content") or "").strip().replace("\n", " ")
# llama.cpp may put the reasoning in a separate field when --reasoning-parser is on
rc_ = (msg.get("reasoning_content") or "")[:60].replace("\n", " ")
verdict = "ANSWER-like" if isinstance(tok, int) and tok < 200 and fin == "stop" \
          else "still capped/rambling"
print(f"{name:<16} {dt:>4}s  completion_tokens={tok}  finish={fin}  -> {verdict}")
print(f"{'':<16} content[:160]: {content[:160]}")
if rc_:
    print(f"{'':<16} reasoning[:60]: {rc_}")
PYEOF
}

echo "endpoint: $LLM_URL"
echo "model   : $LLM_MODEL"
echo "max_tokens per probe: $MAXTOK"
echo "----------------------------------------------------------------"
probe "A-baseline"
probe "B-enable_thinking-false" '"chat_template_kwargs": {"enable_thinking": false}'
probe "C-thinking-false"        '"chat_template_kwargs": {"thinking": false}'
echo "----------------------------------------------------------------"
cat <<'EOF'
Reading the result:
  * B or C collapses to completion_tokens ~ tens + finish=stop
        -> the switch WORKS end-to-end (our launcher already sends B).
  * A rambles AND B/C behave exactly like A
        -> the proxy/server strips unknown fields: the switch must be
           injected IN THE PROXY (uvicorn) or forced via llama.cpp
           chat template / --reasoning-parser on the GPU node.
  * 'reasoning_content' shown separately
        -> llama.cpp runs with a reasoning parser already: content may be
           clean even when tokens look high; judge by content[:160].
EOF
