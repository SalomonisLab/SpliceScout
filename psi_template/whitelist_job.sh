#!/usr/bin/env bash
# =============================================================================
# whitelist_job.sh -- JUNCTION PREVALENCE FILTER for the PSI stage (manuscript Methods, "Junction prevalence
# filtering"). Called by run_psi_job.sh AFTER the BED quarantine preflight and BEFORE AltAnalyze:
#   bash whitelist_job.sh <junction_bed_dir> <filtered_out_dir>
# Keeps junction j only if it is detected in >= round(tau*N) of the N libraries (tau = JUNCTION_PREVALENCE_TAU,
# default 0.01; 0 disables). Writes FILTERED COPIES + summary into <filtered_out_dir> (originals untouched) and
# prints the dir AltAnalyze should read on the LAST line of stdout:
#   * the filtered dir  -- filter applied (or reused from an identical earlier build)
#   * the input dir     -- filter disabled, or the cohort is too small for tau*N to exclude anything, or it failed
# Heavy for big cohorts (A549 N~6,300: ~34 GB, one in-memory pass) -- it runs inside the PSI job's allocation.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u

IN="${1:?usage: whitelist_job.sh <junction_bed_dir> <filtered_out_dir>}"
OUT="${2:?usage: whitelist_job.sh <junction_bed_dir> <filtered_out_dir>}"
TAU="${JUNCTION_PREVALENCE_TAU:-0.01}"

case "$TAU" in
  ""|0|0.0|0.00|off|OFF|no) echo "[whitelist] JUNCTION_PREVALENCE_TAU=$TAU -> prevalence filter DISABLED" >&2; echo "$IN"; exit 0 ;;
esac

PY="$(command -v python || command -v python3)"
[ -n "$PY" ] || { echo "[whitelist] no python on PATH -> unfiltered" >&2; echo "$IN"; exit 0; }

"$PY" "$HERE/junction_whitelist.py" --beddir "$IN" --outdir "$OUT" --tau "$TAU" >&2
rc=$?
case "$rc" in
  0) echo "$OUT" ;;
  3) echo "$IN" ;;                                     # too small to filter: every junction passes anyway
  *) echo "[whitelist] junction_whitelist.py FAILED (rc=$rc) -> AltAnalyze runs on the UNFILTERED BEDs" >&2
     echo "$IN" ;;
esac
exit 0
