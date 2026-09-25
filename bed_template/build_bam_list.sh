#!/usr/bin/env bash
# build_bam_list.sh -- generate $BAM_LIST (one row per *.bam that still needs its BEDs) from $BAM_INPUT_DIR.
# BUILD-ONCE: if the list already exists it is NOT rebuilt, so the watchdog's
# denominator can never drift mid-run. Delete $BAM_LIST to force a rebuild (e.g.
# after STAR publishes more BAMs).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"   # for bed_expected_count (pure-bash row count; grep -c is unreliable on compute nodes)
set -u; shopt -s nullglob

mkdir -p "$PIPELINE_ROOT" "$LOG_DIR"

if [ -s "$BAM_LIST" ]; then
  n=$(bed_expected_count)
  echo "BAM list already exists ($n rows): $BAM_LIST"
  echo "  (delete it to force a rebuild)"
  exit 0
fi

# Scan TOP-LEVEL *.bam (not the bed/ subdir). label = basename without .bam. Only BAMs that still NEED their BEDs are
# listed: a BAM whose junction BED is already there -- complete (bed_done), or gzipped by the PSI stage's
# compress-when-done pass -- is finished. A first run lists every BAM (none has BEDs yet); a re-run after new samples
# lists just the new ones instead of re-converting thousands (their BEDs were .gz, so the plain done-test failed).
BED_DIR="${BED_OUT_DIR:-$BAM_INPUT_DIR}"
{
  for b in "$BAM_INPUT_DIR"/*.bam; do
    [ -e "$b" ] || continue
    label="$(basename "$b" .bam)"
    [ -n "$label" ] || continue
    [ -e "$BED_DIR/${label}__junction.bed.gz" ] && continue
    bed_done "$label" && continue
    printf '%s\n' "$label"
  done | sort -u
} > "$BAM_LIST.tmp"
mv -f "$BAM_LIST.tmp" "$BAM_LIST"     # atomic publish (never a half-written list)

n=$(bed_expected_count)
if [ "$n" -eq 0 ]; then
  # a TARGETED RE-RUN (rerun_submit.sh writes RERUN_TARGETED.txt) with no new BAM: nothing to convert -> the stage
  # completes at once and the PSI launcher proceeds
  if [ -f "$PIPELINE_ROOT/RERUN_TARGETED.txt" ]; then
    echo "targeted re-run: every BAM under $BAM_INPUT_DIR already has its BEDs -> empty list (nothing to convert)"
    exit 0
  fi
  echo "ERROR: no *.bam needing BEDs under $BAM_INPUT_DIR (0 BAMs, or every BAM already converted)" >&2
  exit 1
fi
echo "built $n-BAM list -> $BAM_LIST"
