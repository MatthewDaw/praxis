#!/usr/bin/env bash
# Validates ticket 4bde46205675437b925ffd00d60928e3: a lightweight React
# charting dependency supporting a dual y-axis is declared in package.json,
# a chart component exposing two independent y-axis bindings renders, and
# the gzipped production bundle grows by no more than 150 KB over the
# pre-change baseline.
#
# Run from anywhere; paths are resolved relative to this script.
#
# Baseline handling: the very first run against a tree that does NOT yet
# declare the charting dependency (i.e. the RED run, before this ticket's
# implementation lands) builds the pre-change bundle and caches its gzip
# byte count at BASELINE_CACHE (gitignored). Every later run — once the
# dependency and chart are wired in — diffs its own build against that
# cached number. This avoids fragile git-stash/checkout gymnastics while
# still comparing against a real, once-measured pre-change build.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> frontend-react/

BUDGET_BYTES=$((150 * 1024))
BASELINE_CACHE="scripts/.chart-bundle-baseline-bytes"

gzip_bytes_of_main_chunk() {
  local f
  f=$(ls dist/assets/index-*.js 2>/dev/null | head -1)
  if [ -z "$f" ]; then
    echo "FAIL: no dist/assets/index-*.js produced by the build" >&2
    exit 1
  fi
  gzip -c "$f" | wc -c
}

dep_declared=0
if grep -qE '"(recharts|victory|chart\.js|react-chartjs-2|@nivo/[a-z-]+|@visx/[a-z-]+|lightweight-charts)"[[:space:]]*:' package.json; then
  dep_declared=1
fi

if [ "$dep_declared" -eq 0 ]; then
  npm run build >/dev/null 2>&1
  bytes=$(gzip_bytes_of_main_chunk)
  echo "$bytes" > "$BASELINE_CACHE"
  echo "FAIL: no charting dependency declared in frontend-react/package.json (captured pre-change baseline: $bytes gzip bytes)"
  exit 1
fi

if [ ! -f "$BASELINE_CACHE" ]; then
  echo "FAIL: no cached pre-change baseline at $BASELINE_CACHE — run this check once against the tree before the charting dependency was added"
  exit 1
fi
baseline_bytes=$(cat "$BASELINE_CACHE")

# A chart component under src/components/viz must declare two distinct
# yAxisId bindings (two independent y-axis scales).
chart_src=""
for f in src/components/viz/*.tsx; do
  [ -e "$f" ] || continue
  if grep -q "yAxisId" "$f"; then
    chart_src="$f"
    break
  fi
done
if [ -z "$chart_src" ]; then
  echo "FAIL: no component under src/components/viz declares yAxisId bindings"
  exit 1
fi
axis_id_count=$(grep -oE 'yAxisId="[^"]+"' "$chart_src" | sort -u | wc -l)
if [ "$axis_id_count" -lt 2 ]; then
  echo "FAIL: $chart_src declares fewer than two independent yAxisId bindings (found $axis_id_count)"
  exit 1
fi

# The chart must actually render — proven by its unit test suite.
npx vitest run src/components/viz --reporter=dot

# Bundle-cost budget.
npm run build >/dev/null 2>&1
current_bytes=$(gzip_bytes_of_main_chunk)
growth=$((current_bytes - baseline_bytes))

echo "baseline gzip bytes: $baseline_bytes"
echo "current  gzip bytes: $current_bytes"
echo "growth:              $growth bytes (budget: $BUDGET_BYTES bytes / 150 KB)"

if [ "$growth" -gt "$BUDGET_BYTES" ]; then
  echo "FAIL: gzip bundle grew by $growth bytes, exceeding the 150 KB budget"
  exit 1
fi

echo "PASS"
