#!/usr/bin/env bash
# build_sample_list.sh -- generate $SAMPLE_LIST from $FASTQ_INPUT_DIR.
# BUILD-ONCE: if the list already exists it is NOT rebuilt, so the watchdog's
# denominator can never drift mid-run. Delete $SAMPLE_LIST to force a rebuild.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"   # for star_expected_count (pure-bash row count; grep -c is unreliable on compute nodes)
set -u

mkdir -p "$PIPELINE_ROOT" "$LOG_DIR"

if [ -s "$SAMPLE_LIST" ]; then
  n=$(star_expected_count)
  echo "sample list already exists ($n rows): $SAMPLE_LIST"
  echo "  (delete it to force a rebuild)"
  exit 0
fi

PY="$(command -v python3 || command -v python)"
[ -n "$PY" ] || { echo "ERROR: python3 not found" >&2; exit 1; }

rt=()
[ -n "$RUNTABLE" ] && rt=(--runtable "$RUNTABLE")

"$PY" "$HERE/make_sample_list.py" \
    --input-dir "$FASTQ_INPUT_DIR" \
    ${rt[@]+"${rt[@]}"} \
    --out "$SAMPLE_LIST"
rc=$?

n=$(star_expected_count)
# A TARGETED RE-RUN (rerun_submit.sh writes RERUN_TARGETED.txt) may legitimately find no new FASTQ -- every sample it
# asked for was undeliverable. Then an EMPTY list is the answer: the stage completes at once and hands on to BED,
# instead of failing the launch and leaving the chain to retry forever.
if [ "$rc" -eq 0 ] && [ "$n" -eq 0 ] && [ -f "$PIPELINE_ROOT/RERUN_TARGETED.txt" ]; then
  echo "targeted re-run: no new FASTQ under $FASTQ_INPUT_DIR -> empty sample list (nothing to align)"
  exit 0
fi
if [ "$rc" -ne 0 ] || [ "$n" -eq 0 ]; then
  echo "ERROR: sample-list build failed or produced 0 rows." >&2
  echo "  check $FASTQ_INPUT_DIR and the .orphans/.unmapped/.mixed reports beside $SAMPLE_LIST" >&2
  exit 1
fi
echo "built $n-sample list -> $SAMPLE_LIST"
