#!/usr/bin/env bash
# Synthetic end-to-end demo for the offline agent observatory.
#
# Uses only the CLI, a synthetic fixture, and a temporary SQLite file. Replays
# the import and the response to show that counts and classifications stay
# stable. No network calls and no real transcripts are involved.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/var/tmp}/agent-observatory-demo.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
DB="$WORK/projection.db"
FIXTURE="$ROOT/examples/synthetic_events.jsonl"
STATE="$ROOT/examples/state.json"
RESPONSE="$ROOT/examples/response.json"

echo "== import (first pass) =="
python3 -m agent_observatory import-jsonl --db "$DB" "$FIXTURE" | tee "$WORK/import1.json"

echo "== import (replay) =="
python3 -m agent_observatory import-jsonl --db "$DB" "$FIXTURE" | tee "$WORK/import2.json"

python3 - "$WORK/import2.json" <<'PY'
import json, sys
summary = json.load(open(sys.argv[1]))["imports"][0]
assert summary["events"] == 7, summary
assert summary["skipped_identical_file"], summary
print(f"replay preserved {summary['events']} events across {summary['sessions']} sessions")
PY

echo "== report =="
python3 -m agent_observatory report --db "$DB" > "$WORK/report.json"
python3 - "$WORK/report.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
assert report["coverage"]["events"] == 7, report["coverage"]
assert report["test"]["invocations"] == 2, report["test"]
assert report["test"]["results"] == {"passed": 1, "failed": 1, "unknown": 0}, report["test"]
print("coverage:", json.dumps(report["coverage"]["by_provider"], sort_keys=True))
print("test:", json.dumps(report["test"], sort_keys=True))
PY

echo "== build request =="
python3 -m agent_observatory build-request \
  --db "$DB" \
  --state "$STATE" \
  --subject-kind session \
  --session 'city-a|host-a|codex|session-1' \
  > "$WORK/request.json" 2> "$WORK/request.meta"
cat "$WORK/request.meta"
REQUEST_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["request_hash"])' "$WORK/request.meta")"

echo "== import saved response =="
python3 -m agent_observatory import-response \
  --db "$DB" --response "$RESPONSE" --request-hash "$REQUEST_HASH" > "$WORK/classification1.json"
python3 -m agent_observatory import-response \
  --db "$DB" --response "$RESPONSE" --request-hash "$REQUEST_HASH" > "$WORK/classification2.json"
python3 - "$WORK/classification1.json" "$WORK/classification2.json" <<'PY'
import json, sys
first = json.load(open(sys.argv[1]))
second = json.load(open(sys.argv[2]))
assert first["classification_id"] == second["classification_id"], (first, second)
assert second["deduplicated"] is True, second
print(f"classification {first['classification_id']} deduplicated on replay")
PY

echo "DEMO OK"
