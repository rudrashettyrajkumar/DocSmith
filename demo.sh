#!/usr/bin/env bash
# API-level demo of the two required test inputs (the web UI at :8000 is nicer).
# Usage: PROVIDER=groq MODEL=llama-3.3-70b-versatile API_KEY=gsk_... bash demo.sh

set -euo pipefail
PROVIDER="${PROVIDER:-groq}"
MODEL="${MODEL:-llama-3.3-70b-versatile}"
: "${API_KEY:?Set API_KEY=<your key for $PROVIDER>}"
BASE="http://localhost:8000"

run() {
  echo "=========== $1 ==========="
  JOB=$(curl -s "$BASE/agent" -H "Content-Type: application/json" \
    -d "{\"request\": $2, \"provider\": \"$PROVIDER\", \"model\": \"$MODEL\", \"api_key\": \"$API_KEY\"}")
  ID=$(echo "$JOB" | python3 -c "import json,sys; print(json.load(sys.stdin)['job_id'])")
  echo "job: $ID (polling...)"
  while :; do
    STATE=$(curl -s "$BASE/api/jobs/$ID")
    STATUS=$(echo "$STATE" | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
    [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] && break
    sleep 2
  done
  echo "$STATE" | python3 -m json.tool
  echo
}

run "TEST 1: standard business request" \
  '"Create a business proposal for offering an AI-powered customer support chatbot to a mid-sized online retail company."'

run "TEST 2: complex / ambiguous request" \
  '"We have a leadership meeting next week about our mobile app project which is in trouble - costs are exploding, the client keeps demanding new features, and two developers just quit. Prepare something we can present. Keep it positive but also honest about the risks."'
