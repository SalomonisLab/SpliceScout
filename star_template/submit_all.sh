#!/usr/bin/env bash
# submit_all.sh -- submit one STAR job per sample row in $SAMPLE_LIST.
# Idempotent: skips samples that already have a valid BAM or a live job, so it is
# safe to re-run to mop up failures.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
[ -f "$PIPELINE_ROOT/RESOLVED_INDEX.env" ] && source "$PIPELINE_ROOT/RESOLVED_INDEX.env"  # resolved GENOME_DIR + BUILD_JID
set -u; shopt -s nullglob
star_require_bsub
star_load_modules                  # samtools, for star_bam_ok

[ -f "$SAMPLE_LIST" ] || { echo "no sample list ($SAMPLE_LIST) -- run build_sample_list.sh first" >&2; exit 1; }
mkdir -p "$BAM_OUT" "$LOG_DIR"
LIVE="$(star_live_names)"

n=0; skip=0; ndrop=0
while IFS=$'\t' read -r LABEL F1 F2; do
  [ -n "${LABEL:-}" ] || continue
  if star_bam_ok "$LABEL"; then skip=$((skip+1)); continue; fi
  # dropped after STAR_MAX_FAILS (the watchdog skips these too): a relaunch must not re-submit a sample the stage
  # gave up on (e.g. its FASTQ is gone). To retry one, delete $STAR_ATTEMPTS_DIR/<label>.dropped and .n first.
  if star_is_dropped "$LABEL"; then ndrop=$((ndrop+1)); continue; fi
  if star_has_live "$(star_jobname "$LABEL")" "$LIVE"; then skip=$((skip+1)); continue; fi
  star_submit_sample "$LABEL" "$F1" "${F2:-NA}" >/dev/null
  n=$((n+1))
done < "$SAMPLE_LIST"

echo "submitted $n STAR job(s); skipped $skip (already done or in-flight) + $ndrop dropped earlier (star_dropped.txt); BAMs -> $BAM_OUT"
