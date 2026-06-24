#!/usr/bin/env bash
# Orchestrate one load-test run against the app under the PRODUCTION gunicorn
# config (workers=1, threads=8 — the real concurrency ceiling).
#
# Usage: loadtest/run.sh <users> <spawn_rate> <run_time> <tag>
#   e.g. loadtest/run.sh 1000 100 60s u1000
set -uo pipefail
cd "$(dirname "$0")/.."

USERS="${1:-1000}"
SPAWN="${2:-100}"
RUNTIME="${3:-60s}"
TAG="${4:-run}"
PORT="${PORT:-10000}"
HOST="http://127.0.0.1:${PORT}"

source .venv/bin/activate
mkdir -p loadtest/results

echo "=== Starting gunicorn (workers=1, threads=8, prod config) on :${PORT} ==="
# Bare `gunicorn app:app` auto-loads gunicorn.conf.py (workers=1/gthread/timeout=300).
PORT="$PORT" gunicorn app:app > "loadtest/results/gunicorn_${TAG}.log" 2>&1 &
GUNI_PID=$!

# Wait for health.
for _ in $(seq 1 30); do
  if curl -fsS "${HOST}/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done
if ! curl -fsS "${HOST}/api/health" >/dev/null 2>&1; then
  echo "!! gunicorn failed to come up — log:"; tail -30 "loadtest/results/gunicorn_${TAG}.log"
  kill $GUNI_PID 2>/dev/null; exit 1
fi
WORKER_PID=$(pgrep -P "$GUNI_PID" | head -1)
echo "Health OK. gunicorn master=$GUNI_PID worker=$WORKER_PID"

# Sample the WORKER's RSS(KB)/threads/CPU% once per second during the run.
( while kill -0 "$WORKER_PID" 2>/dev/null; do
    ps -o rss=,nlwp=,pcpu= -p "$WORKER_PID" 2>/dev/null | awk -v t="$(date +%s)" '{print t,$1,$2,$3}'
    sleep 1
  done ) > "loadtest/results/proc_${TAG}.log" &
MON_PID=$!

echo "=== Locust: ${USERS} users, spawn ${SPAWN}/s, for ${RUNTIME} (same_ip=${LT_SAME_IP:-0}) ==="
locust -f loadtest/locustfile.py --headless \
  -u "$USERS" -r "$SPAWN" -t "$RUNTIME" \
  --host "$HOST" \
  --csv "loadtest/results/${TAG}" --csv-full-history \
  --only-summary 2>&1 | tee "loadtest/results/locust_${TAG}.log"

kill $MON_PID 2>/dev/null
echo "=== Peak worker RSS (KB) / max threads ==="
awk 'NR==1{maxr=$2;maxt=$3} {if($2>maxr)maxr=$2; if($3>maxt)maxt=$3} END{printf "peak_rss_kb=%d  max_threads=%d  samples=%d\n", maxr, maxt, NR}' "loadtest/results/proc_${TAG}.log"
echo "=== gunicorn errors/warnings (if any) ==="
grep -iE "error|traceback|critical|sigkill|timeout|worker" "loadtest/results/gunicorn_${TAG}.log" | tail -20 || echo "(none)"

kill $GUNI_PID 2>/dev/null
wait $GUNI_PID 2>/dev/null
echo "=== Done: $TAG ==="
