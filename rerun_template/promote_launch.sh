#!/usr/bin/env bash
# =============================================================================
# promote_launch.sh -- part of a re-run / re-score kit built by rerun_deploy.py (armed by rerun_submit.sh or
# rescore_submit.sh). A self-rescheduling LSF launcher: once the NEW concordance stage has finished, it puts the new
# results under the cell line's ORIGINAL folder names (FINAL_CONC_DIR, and FINAL_PSI_DIR for a full re-run -- a
# re-score leaves FINAL_PSI_DIR empty: it read the existing PSI) -- "put the new data in the old folders" (2026-09-22).
#   * the previous contents of an original folder are KEPT as <folder>.old_<date> -- never deleted;
#   * the top-level scripts / configs / reports of a moved folder are re-pointed at its new path, so its status
#     scripts and any later re-run use the new location;
#   * waits while any job of the new PSI / concordance stages is still live; never promotes a STALLED concordance.
# Checks every CHECK_MIN minutes; gives up after MAX_WAIT_HOURS (writes PROMOTE_GAVE_UP.txt next to it).
# =============================================================================
set -u
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$KIT/rerun.env"                       # PSI_DIR CONC_DIR FINAL_PSI_DIR FINAL_CONC_DIR PSI_TAG CONC_TAG
CHECK_MIN=60
MAX_WAIT_HOURS=1344
LOG="$KIT/promote.log"
mkdir -p "$KIT/logs"
say() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
command -v bsub >/dev/null 2>&1 || { say "no bsub on $(hostname) -> stop"; exit 0; }
[ -n "${FINAL_CONC_DIR:-}" ] || { say "rerun.env names no FINAL_CONC_DIR -> nothing to promote"; exit 0; }
PROMOTE_PSI=0; [ -n "${FINAL_PSI_DIR:-}" ] && [ "${FINAL_PSI_DIR:-}" != "${PSI_DIR:-}" ] && PROMOTE_PSI=1
LIVE_RE="^${CONC_TAG}_(job|watchdog|launch)"; [ "$PROMOTE_PSI" = 1 ] && LIVE_RE="^(${PSI_TAG}_|${CONC_TAG}_(job|watchdog|launch))"

# swap_into <new dir> <original dir> [<extra old path> <extra new path>] -- move <new> to <original>'s name; the
# original's current contents go to <original>.old_<date>; top-level text files are re-pointed at the new path.
swap_into() {
  local new="$1" final="$2" x_from="${3:-}" x_to="${4:-}" bak="" stamp f
  [ -d "$new" ] || { say "SKIP: $new is gone (already promoted?)"; return 0; }
  if [ -e "$final" ]; then
    stamp="$(date -r "$final" +%Y%m%d 2>/dev/null || date +%Y%m%d)"
    bak="$final.old_$stamp"
    [ -e "$bak" ] && bak="$bak.$(date +%H%M%S)"
    mv "$final" "$bak" || { say "FAILED to move $final aside -> stop (nothing else changed)"; return 1; }
    say "kept the previous $final as $bak"
  fi
  mkdir -p "$(dirname "$final")"
  if ! mv "$new" "$final"; then
    say "FAILED to move $new -> $final; putting the previous folder back"
    [ -n "$bak" ] && mv "$bak" "$final"
    return 1
  fi
  for f in "$final"/* "$final"/results/*; do
    [ -f "$f" ] || continue
    case "$f" in *.sh|*.txt|*.tsv|*.out|*.log|*.py) ;; *) continue ;; esac
    if grep -qF "$new" "$f" 2>/dev/null || { [ -n "$x_from" ] && grep -qF "$x_from" "$f" 2>/dev/null; }; then
      sed -i "s#${new}#${final}#g" "$f"
      [ -n "$x_from" ] && sed -i "s#${x_from}#${x_to}#g" "$f"
    fi
  done
  echo "$(date '+%F %T'): results moved here from $new by the re-run kit (previous contents: ${bak:-none})" \
    >> "$final/SWAPPED.txt"
  say "promoted $new -> $final"
}

if [ -f "$KIT/PROMOTED.txt" ]; then say "already promoted -> stop"; exit 0; fi
if [ -f "$CONC_DIR/PIPELINE_STALLED.txt" ]; then
  say "the new concordance STALLED -> not promoting; the results stay in $CONC_DIR"; exit 0
fi
if [ -f "$CONC_DIR/PIPELINE_COMPLETE.txt" ]; then
  live="$(timeout 60 bjobs -noheader -o job_name 2>/dev/null)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    say "bjobs failed (rc=$rc) -> retry next pass"
  elif printf '%s\n' "$live" | grep -qE "$LIVE_RE"; then
    say "jobs of the new stages are still live -> retry next pass"
  else
    if [ "$PROMOTE_PSI" = 1 ]; then
      swap_into "$PSI_DIR" "$FINAL_PSI_DIR" || exit 1
      swap_into "$CONC_DIR" "$FINAL_CONC_DIR" "$PSI_DIR" "$FINAL_PSI_DIR" || exit 1
    else
      swap_into "$CONC_DIR" "$FINAL_CONC_DIR" || exit 1
    fi
    { echo "Promoted $(date '+%F %T')"; [ "$PROMOTE_PSI" = 1 ] && echo "  PSI         -> $FINAL_PSI_DIR"
      echo "  concordance -> $FINAL_CONC_DIR"; } > "$KIT/PROMOTED.txt"
    say "done: the new results are in $FINAL_CONC_DIR$([ "$PROMOTE_PSI" = 1 ] && echo " and $FINAL_PSI_DIR")"
    exit 0
  fi
fi

# not finished yet -> bounded wait, then reschedule this launcher
STAMP="$KIT/.promote_first_seen"
[ -f "$STAMP" ] || date +%s > "$STAMP"
now=$(date +%s); first=$(cat "$STAMP" 2>/dev/null || echo "$now")
if [ "$(( now - first ))" -gt "$(( MAX_WAIT_HOURS * 3600 ))" ]; then
  echo "promote_launch gave up at $(date): $CONC_DIR never finished within ${MAX_WAIT_HOURS}h" > "$KIT/PROMOTE_GAVE_UP.txt"
  say "gave up after ${MAX_WAIT_HOURS}h"; exit 0
fi
when=$(date -d "+$CHECK_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null)
bsub -L /bin/bash -n 1 -M 500 -W 60 -b "$when" -J "${CONC_TAG}_promote" \
     -o "$KIT/logs/promote.out" -e "$KIT/logs/promote.err" "$KIT/promote_launch.sh" >/dev/null 2>&1 \
  || say "WARNING: could not reschedule (bsub failed) -- re-arm: bsub -L /bin/bash -J ${CONC_TAG}_promote $KIT/promote_launch.sh"
exit 0
