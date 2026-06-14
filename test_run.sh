#!/usr/bin/env bash
set -e

KEY="${1:-}"
BASE="http://localhost:8080"

echo "==> upserting skill"
curl -s -X POST $BASE/skills \
  -H "X-OmniAgent-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"name":"weather","version":"v1","tool_names":["test-service.get_weather","test-service.get_uv_index","test-service.get_clothing_recommendation"],"instructions":"Use get_weather first to get temperature and condition, then pass those values to get_clothing_recommendation. Use get_uv_index for UV info.","system_prompt":"You have access to weather, UV, and clothing recommendation tools."}' \
  | jq . || true

echo "==> fetching agent (create if missing)"
AGENT=$(curl -s $BASE/agents \
  -H "X-OmniAgent-Key: $KEY" | jq -r '.[] | select(.name=="weather-bot") | .id')

if [ -z "$AGENT" ]; then
  AGENT=$(curl -s -X POST $BASE/agents \
    -H "X-OmniAgent-Key: $KEY" -H "Content-Type: application/json" \
    -d '{"name":"weather-bot","version":"v1","harness":"antigravity","skill_refs":{"weather":"v1"},"system_prompt":"You are a helpful weather assistant."}' \
    | jq -r .id)
fi
echo "agent: $AGENT"

echo "==> creating session"
SESSION=$(curl -s -X POST $BASE/sessions \
  -H "X-OmniAgent-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"agent_name":"weather-bot"}' | jq -r .id)
echo "session: $SESSION"

echo "==> running"
curl -s -X POST $BASE/sessions/$SESSION/run \
  -H "X-OmniAgent-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"Whats the weather in Tokyo and what should I wear?"}' | jq .

echo "==> streaming events (ctrl+c to stop)"
curl -N $BASE/sessions/$SESSION/stream \
  -H "X-OmniAgent-Key: $KEY"
