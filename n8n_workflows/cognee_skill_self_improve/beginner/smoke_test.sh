#!/usr/bin/env bash
# End-to-end smoke test for the no-code skill loop against a Cognee /api/v1 server.
# Exercises the exact endpoints the Verified Node's Skill resource calls.
#
# Requires a running Cognee server with an LLM + embedding provider configured
# (Review/Propose call the LLM; ingest needs an embedding model).
#
# Usage:
#   BASE_URL=http://localhost:8000 API_KEY=ck_xxx ./smoke_test.sh
set -euo pipefail

BASE_URL="${BASE_URL:?set BASE_URL, e.g. http://localhost:8000}"
API_KEY="${API_KEY:?set API_KEY}"
DATASET="${DATASET:-n8n-skill-self-improvement}"
SKILL="${SKILL:-code-review}"
H=(-H "X-Api-Key: ${API_KEY}" -H "Content-Type: application/json")
api="${BASE_URL%/}/api/v1"

SKILL_MD="$(cat ../advanced/my_skills/code-review/SKILL.md)"
TASK="Review this change: an endpoint now does return get_dataset(requested_dataset). Flag missing ownership checks, missing-record handling, and missing tests."

echo "== 1. Ingest Skill =="
INGEST=$(jq -n --arg t "$SKILL_MD" --arg n "$SKILL" --arg d "$DATASET" \
  '{skills_text:$t, skill_name:$n, dataset_name:$d}' \
  | curl -s "${H[@]}" -d @- "${api}/skills")
echo "$INGEST" | jq .
DATASET_ID=$(echo "$INGEST" | jq -r '.dataset_id')
echo "dataset_id=$DATASET_ID"

echo "== 2. Review Skill (AGENTIC_COMPLETION) =="
jq -n --arg q "$TASK" --arg d "$DATASET" --arg s "$SKILL" \
  '{search_type:"AGENTIC_COMPLETION", query:$q, datasets:[$d], skills:[$s], max_iter:6, top_k:15}' \
  | curl -s "${H[@]}" -d @- "${api}/search" | jq .

echo "== 3. Propose Improvement =="
PROPOSE=$(jq -n --arg s "$SKILL" --arg d "$DATASET" --arg t "$TASK" \
  '{entry:{type:"skill_run", selected_skill_id:$s, task_text:$t, result_summary:"Missed ownership check / 404 handling / tests.", success_score:0.3, feedback:-1, candidate_skill_ids:[$s]}, dataset_name:$d, skill_improvement:{skill_name:$s, apply:false, score_threshold:0.9}}' \
  | curl -s "${H[@]}" -d @- "${api}/remember/entry")
echo "$PROPOSE" | jq .
PROPOSAL_ID=$(echo "$PROPOSE" | jq -r '.items[] | select(.kind=="skill_improvement_proposal") | .proposal_id')
echo "proposal_id=$PROPOSAL_ID"

echo "== 4. Get Proposal (before/after diff) =="
curl -s "${H[@]}" "${api}/proposals/${PROPOSAL_ID}?dataset_id=${DATASET_ID}" | jq '{proposal_id, skill_id, confidence, rationale, old_procedure, proposed_procedure}'

echo "== 5. Apply Improvement =="
jq -n --arg s "$SKILL" --arg d "$DATASET" --arg p "$PROPOSAL_ID" \
  '{entry:{type:"skill_run", selected_skill_id:$s, success_score:0.3, feedback:-1}, dataset_name:$d, skill_improvement:{skill_name:$s, apply:true, proposal_id:$p}}' \
  | curl -s "${H[@]}" -d @- "${api}/remember/entry" | jq .

echo "== 6. Get Skill (updated procedure) =="
SKILL_ID=$(curl -s "${H[@]}" "${api}/skills/?dataset_id=${DATASET_ID}" | jq -r ".[] | select(.name==\"${SKILL}\") | .id")
curl -s "${H[@]}" "${api}/skills/${SKILL_ID}?dataset_id=${DATASET_ID}" | jq '{id, name, procedure}'

echo "== done =="
