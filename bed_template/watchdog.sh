#!/usr/bin/env bash
# =============================================================================
# watchdog.sh -- the self-driving controller for BAM->BED. Each pass it:
#   1. resubmits any BAM with no valid BED pair and no live job,
#   2. counts progress (done / expected) over the FIXED BAM list,
#   3. declares COMPLETE (all done, no live work jobs) or STALLED (no forward
#      progress for MAX_STALL_PASSES idle passes), else
#   4. reschedules itself WATCHDOG_INTERVAL_MIN into the future via `bsub -b`.
# Counts only WORK jobs (^${JOB_TAG}_bed_), never itself, so nlive can reach 0.
# Started by run_bed_pipeline.sh; needs no babysitting. This is the LAST stage in
# the chain (download -> STAR -> BED), so finalize() kicks nothing downstream.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"
if [ -f "$HERE/lib_notify.sh" ]; then source "$HERE/lib_notify.sh"; else
  log_event(){ :; }; notify_error(){ :; }; notify_update(){ :; }; fi
set -u; shopt -s nullglob
: "${BED_OUT_DIR:=$(dirname "$BAM_INPUT_DIR")/STAR_beds}"   # robust if an older deployed config.sh predates BED_OUT_DIR
bed_load_modules                        # harmless (bed_done is pure file checks); kept for parity

LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

# Reschedule-first: the NEXT pass is queued at the START of each pass, so a mid-pass walltime kill
# (e.g. bsub blocked on the LSF pending-job threshold) can NEVER break the self-driving chain.
# finalize() cancels the successor once the stage is actually done.
WATCHDOG_NEXT_JID=""
RESCHED_RC=0
reschedule() {
  local when out
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  bed_qopt
  # capture bsub's OWN rc (not star_jobid's pipeline-tail rc) so the safety-net re-arms only on a genuine
  # submit FAILURE, never on a successful-but-unparsed submit (which would double-arm two chains).
  out=$(bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>&1)
  RESCHED_RC=$?
  WATCHDOG_NEXT_JID=$(printf '%s' "$out" | bed_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?}, rc=$RESCHED_RC)"
}

# IDEMPOTENT tool cleanup (free disk on a clean COMPLETE). Extracted from finalize() and gated on a SEPARATE
# TOOLS_CLEANUP_COMPLETE.txt marker so it SELF-HEALS: a run that finalized COMPLETE before this code existed,
# or whose finalize skipped cleanup (bjobs unreliable that pass), is cleaned the next time the watchdog runs
# (e.g. a manual re-arm) instead of staying uncleaned forever (the MDSL symptom: tools never removed). Runs
# at most once; NEVER on a partial run; never out from under a live conversion; rm targets are path-guarded.
bed_cleanup_tools() {
  [ "${CLEANUP_TOOLS_WHEN_DONE:-1}" = "1" ] || return 0
  [ -f "$PIPELINE_ROOT/TOOLS_CLEANUP_COMPLETE.txt" ] && return 0
  { [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE_PARTIAL.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ]; } && return 0
  local dlroot freshlive _frc
  dlroot="$(dirname "$BAM_INPUT_DIR")"
  freshlive="$(bed_snapshot)"; _frc=$?
  if [ "$_frc" -ne 0 ] || [ -z "$(printf '%s' "$freshlive" | tr -d '[:space:]')" ] \
     || printf '%s\n' "$freshlive" | grep -qE "^${JOB_TAG}_bed_"; then
    say "cleanup: SKIPPED -- a work job is still live or bjobs is unreliable (kept tools, no risky rm)"
    return 0
  fi
  [ -n "${ALTANALYZE_DIR:-}" ] && [ "$ALTANALYZE_DIR" != "/" ] && rm -rf "$ALTANALYZE_DIR" 2>/dev/null
  # LOG a cleanup that didn't actually free the space (rm errors were swallowed with 2>/dev/null) instead of
  # falsely reporting "removed" -- the user can then chase a permission/NFS problem.
  [ -n "${ALTANALYZE_DIR:-}" ] && [ -e "$ALTANALYZE_DIR" ] && say "cleanup: WARNING -- could not remove $ALTANALYZE_DIR (still present; check perms/NFS)"
  if [ -n "$dlroot" ] && [ "$dlroot" != "/" ] && [ -d "$dlroot/STAR_bams" ] \
     && [ "$(printf '%s' "$dlroot" | awk -F/ '{print NF-1}')" -ge 3 ]; then
    rm -rf "$dlroot/star" "$dlroot/by_study" 2>/dev/null
    rm -f "$dlroot"/*.sh "$dlroot"/*.py 2>/dev/null
    say "cleanup: removed AltAnalyze toolkit/ref + STAR bundle + download scripts (kept BAMs, $BED_OUT_DIR, markers, logs)"
  else
    say "cleanup: removed AltAnalyze toolkit; SKIPPED $dlroot rm -- failed the run-root safety guard"
  fi
  : > "$PIPELINE_ROOT/TOOLS_CLEANUP_COMPLETE.txt"
}

finalize() {                            # $1 = COMPLETE | STALLED
  local status="$1" label rest
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"   # SEPARATE line: bash 4.2 + set -u can't see $status declared in the SAME `local`
  # EXACTLY-ONCE (T2.3): atomic mkdir claim (NFS-safe) -- only the first racer does the destructive cleanup.
  bed_finalize_once || { say "finalize already claimed by a concurrent pass -> skip"; return 0; }
  # Cancel ALL queued/duplicate watchdog successors except THIS job (a double-armed/nudged one would
  # otherwise re-spawn the chain after we stop).
  local _self _wj
  _self="${LSB_JOBID:-}"
  for _wj in $(bjobs -noheader -o jobid -J "${JOB_TAG}_watchdog" 2>/dev/null); do
    [ "$_wj" = "$_self" ] && continue
    bkill "$_wj" >/dev/null 2>&1
  done
  # PARTIAL run (T1.1): disable destructive cleanup + per-job BAM deletion + write an HONEST marker whenever
  # the dataset may be incomplete. Detected WITHOUT relying on the launcher-written
  # PIPELINE_INCOMPLETE_UPSTREAM marker (a script-only hot-patch does NOT replace the deploy-generated
  # launcher) -- the robust signals are the upstream POSITIVE completion markers: STAR's
  # ($BAM_INPUT_DIR/PIPELINE_COMPLETE.txt) AND the download's ($dlroot/PIPELINE_COMPLETE.txt). partial=1
  # unless BOTH are present (and this stage itself COMPLETEd), so the toolkit/STAR-bundle/by_study purge
  # below is gated on the whole chain having positively succeeded.
  local partial=0 dlroot
  dlroot="$(cd "$BAM_INPUT_DIR/.." 2>/dev/null && pwd)"
  if [ "$status" = "STALLED" ] || [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] \
     || [ ! -f "$BAM_INPUT_DIR/PIPELINE_COMPLETE.txt" ] \
     || { [ -n "${dlroot:-}" ] && [ ! -f "$dlroot/PIPELINE_COMPLETE.txt" ]; }; then
    partial=1
  fi
  {
    echo "BAM->BED (AltAnalyze junction/exon) stage $status at $(ts)"
    [ "$partial" = "1" ] && echo "*** PARTIAL: upstream incomplete and/or this stage STALLED -- NOT all BAMs converted; cleanup/BAM-deletion DISABLED ***"
    echo "Converted: $done_n / $exp_n BAMs"
    echo "BED outputs: $BED_OUT_DIR  (<sample>__junction.bed + __exon.bed/__intronJunction.bed per BED_MODE)"
    echo
    echo "Per-sample:"
    while IFS=$'\t' read -r label rest; do
      [ -n "$label" ] || continue
      if bed_done "$label"; then printf '  %-44s OK\n' "$label"
      else printf '  %-44s MISSING/INVALID\n' "$label"; fi
    done < "$BAM_LIST"
    if [ "$status" = "STALLED" ]; then
      echo
      echo "BAMs with no valid BED pair after repeated resubmits -- inspect"
      echo "$LOG_DIR/bed_<label>.err (likely a bad/truncated BAM, a missing exon"
      echo "reference, or a python/pysam problem):"
      while IFS=$'\t' read -r label rest; do
        [ -n "$label" ] || continue
        bed_done "$label" || echo "  $label"
      done < "$BAM_LIST"
    fi
  } > "$rep"
  [ "$partial" = "1" ] && echo "PARTIAL run at $(ts): $done_n/$exp_n converted. Upstream incomplete and/or STALLED; destructive cleanup + BAM deletion were DISABLED. Inspect before deleting anything." \
      > "$PIPELINE_ROOT/PIPELINE_COMPLETE_PARTIAL.txt"
  # LAST stage in the chain -- no downstream launcher to kick. Tool cleanup (free disk) only on a clean
  # COMPLETE; the idempotent, self-healing logic lives in bed_cleanup_tools (also run at pass-top so a
  # COMPLETE-but-uncleaned run is reconciled on a re-arm).
  [ "$status" = "COMPLETE" ] && [ "$partial" = "0" ] && bed_cleanup_tools
  if [ "$status" = "STALLED" ]; then
    notify_error "BAM->BED stage STALLED" "$(head -20 "$rep" 2>/dev/null)" "bed-stalled"
    notify_diagnose "$JOB_TAG" "$PIPELINE_ROOT" "$SCRIPTS_DIR" "$LOG_DIR"
  else notify_update "BAM->BED stage COMPLETE" "$(head -10 "$rep" 2>/dev/null)"; fi
  say "FINALIZED ($status; partial=$partial) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ]; then
  bed_cleanup_tools   # SELF-HEAL: reconcile a COMPLETE-but-uncleaned run (tools never removed) on a re-arm
  say "already finalized (COMPLETE) -> stop"; exit 0
fi
if [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then say "already finalized (STALLED) -> stop"; exit 0; fi
# Reclaim a STALE finalize lock (no marker exists here => a prior finalize died before writing one).
[ -d "$PIPELINE_ROOT/.finalized.lock" ] && rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null
[ -f "$BAM_LIST" ] || { say "no BAM list at $BAM_LIST -- stopping"; exit 1; }

# SINGLE-FLIGHT (T2.3): only ONE pass at a time (a nudged + a timed successor would otherwise resubmit
# the same BAM and race its BED-file writes). The loser exits; the winner already reschedule-first'd.
exec 8>"$PIPELINE_ROOT/.watchdog.run.lock" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  flock -n 8 2>/dev/null || { say "another watchdog pass holds the run lock -> exit"; exit 0; }
fi

reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)

# ABSOLUTE BACKSTOP (T2.2), bjobs-INDEPENDENT: cap by pass-count AND wall-clock so a permanently-PENDING
# job or a persistently-broken bjobs can never loop/skip forever with no human signal.
_now=$(date +%s)
[ -f "$STATE.firstpass" ] || echo "$_now" > "$STATE.firstpass"
_first=$(cat "$STATE.firstpass" 2>/dev/null || echo "$_now")
_passes=$(( $(cat "$STATE.passes" 2>/dev/null || echo 0) + 1 )); echo "$_passes" > "$STATE.passes"
if [ "$_passes" -ge "${ABSOLUTE_MAX_PASSES:-960}" ] || [ "$(( _now - _first ))" -ge "$(( ${MAX_WALL_HOURS:-336} * 3600 ))" ]; then
  exp_n=$(bed_expected_count); done_n=$(bed_done_count); nlive=0
  say "BACKSTOP: passes=$_passes wall=$(( _now - _first ))s exceeded the cap -> STALLED (inspect stuck/PENDING jobs)"
  finalize "STALLED"; exit 0
fi

# SNAPSHOT WITH RC (T2.1): bjobs FAILED or EMPTY -> UNKNOWN -> never treat as "no jobs -> COMPLETE".
# An empty/failed bjobs is UNRELIABLE, not a positive "queue is empty" signal: finalizing on it would
# delete the toolkit out from under still-running conversions. Wrap bjobs in `timeout 60` (a hung LSF
# daemon must not wedge the pass) and RE-POLL once on UNKNOWN before giving up, then skip the whole pass.
bed_snap_timed() { timeout 60 bjobs -noheader -o job_name 2>/dev/null; }
LIVE="$(bed_snap_timed)"; BED_SNAP_RC=$?      # capture bjobs rc in the PARENT (command-subst is a subshell)
if [ "$BED_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$BED_SNAP_RC) UNKNOWN -- re-polling before skipping (never finalize on an empty snapshot)"
  LIVE="$(bed_snap_timed)"; BED_SNAP_RC=$?
  if [ "$BED_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
    say "WARNING: bjobs still failed/empty (rc=$BED_SNAP_RC) -- treating as UNKNOWN, skipping resubmit + completion this pass"
    exit 0
  fi
fi
nlive=$(bed_count_work "$LIVE")      # pure-bash count (grep -c returns empty on compute nodes -> wedged the gate)
exp_n=$(bed_expected_count)
done_n=$(bed_done_count)

# SANITY FLOOR (T2.1): live-work collapse >50% vs last pass with no new BEDs -> suspect partial bjobs -> skip.
_prev_nlive=$(cat "$STATE.nlive" 2>/dev/null || echo -1)
_prev_done=$(cat "$STATE.donen" 2>/dev/null || echo -1)
echo "$nlive" > "$STATE.nlive"; echo "$done_n" > "$STATE.donen"
if [ "$_prev_nlive" -gt 1 ] && [ "$nlive" -lt "$(( _prev_nlive / 2 ))" ] && [ "$done_n" -le "$_prev_done" ]; then
  say "WARNING: live work jobs collapsed $_prev_nlive->$nlive with no new BEDs (suspect partial bjobs) -- skipping this pass"
  exit 0
fi

# 1) resubmit BAMs with no valid BED pair that have no live job -- bulk snapshot AND targeted re-check,
#    plus a missing-BAM guard (if DELETE_BAM_AFTER_BED removed the BAM, a resubmit can't succeed).
resub=0
while IFS=$'\t' read -r label rest; do
  [ -n "$label" ] || continue
  bed_done "$label" && continue
  bed_is_dropped "$label" && continue        # already gave up on this sample (drop-after-N)
  jn="$(bed_jobname "$label")"
  bed_has_live "$jn" "$LIVE" && continue
  bed_job_is_live "$jn" && continue          # targeted re-verify (T2.1): fail-closed against partial snapshots
  if [ ! -s "$BAM_INPUT_DIR/$label.bam" ]; then
    bed_drop_sample "$label" "bam-gone"
    say "UNRECOVERABLE: $label has no valid BEDs and its BAM is gone -- DROPPED (re-align needed to recover)"
    continue
  fi
  # not done + not seen live. ONLY count a failure when the last attempt ACTUALLY ENDED (its -o log was
  # written after we submitted it). BED conversions on big BAMs run LONGER than the poll interval, and a
  # loaded/flaky bjobs can momentarily report a running job not-live -- counting that as a "failed
  # conversion" is exactly what false-dropped 15 of 17 samples and tripped the meltdown guard to mark a
  # SUCCESSFUL MDS_L run STALLED (2026-06-22). A genuinely-stuck job is caught by the no-churn STALL guard
  # below, not by this counter.
  if bed_job_submitted "$label" && ! bed_job_ended "$label"; then
    continue                                  # latest attempt still running -> wait (no bump, no duplicate)
  fi
  if bed_job_ended "$label"; then             # the attempt terminated with no valid BED -> a REAL failure
    _n=$(bed_bump_attempt "$label")
    if [ "$_n" -gt "${BED_MAX_FAILS:-3}" ]; then
      bed_drop_sample "$label" "conversion"
      say "DROPPED $label after $((_n - 1)) failed conversions -> logged to bed_dropped.txt"
      continue
    fi
  fi
  bed_submit_sample "$label" >/dev/null && resub=$((resub+1))
done < "$BAM_LIST"
[ "$resub" -gt 0 ] && say "resubmitted $resub missing/invalid BAM(s)"
dropped_n=$(bed_dropped_count)
say "progress: $done_n/$exp_n converted ($dropped_n dropped after ${BED_MAX_FAILS:-3} fails) ; $nlive live work job(s) ; $resub resubmitted this pass"

# MELTDOWN GUARD (mirrors the download stage): if a LARGE fraction had to be dropped, the converter /
# exon-ref / toolkit is likely globally broken (not a few bad BAMs) -> STALL with an HONEST marker rather
# than a false COMPLETE with most BEDs missing (which would silently feed an almost-empty PSI).
_ceiling=$(( exp_n / 10 )); [ "$_ceiling" -lt 5 ] && _ceiling=5
if [ "$dropped_n" -gt "$_ceiling" ] && [ "$nlive" -eq 0 ]; then
  say "MELTDOWN: $dropped_n/$exp_n dropped (> $_ceiling = >10%) -- converter likely broken, not a few bad BAMs -> STALLED (inspect logs/bed_dropped.txt, fix, re-run)"
  finalize "STALLED"; exit 0
fi

# 2) completion: every BAM converted OR dropped, AND no live work job
if [ "$(( done_n + dropped_n ))" -ge "$exp_n" ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

# 3) stall: no live jobs AND no progress for MAX_STALL_PASSES; OR no-churn (done_n AND live set unchanged)
#    for 4x MAX_STALL_PASSES (catches a permanently-PENDING/looping job).
_hash=$(bed_live_work_hash "$LIVE")
_prev_hash=$(cat "$STATE.hash" 2>/dev/null || echo "")
echo "$_hash" > "$STATE.hash"
if [ "$nlive" -eq 0 ]; then
  prev=$(cat "$STATE" 2>/dev/null || echo -1)
  stall=0
  [ "$done_n" = "$prev" ] && stall=$(( $(cat "$STATE.stall" 2>/dev/null || echo 0) + 1 ))
  echo "$done_n" > "$STATE"; echo "$stall" > "$STATE.stall"; echo 0 > "$STATE.nochurn"
  if [ "$stall" -ge "${MAX_STALL_PASSES:-2}" ]; then finalize "STALLED"; exit 0; fi
else
  echo "$done_n" > "$STATE"; echo 0 > "$STATE.stall"
  if [ "$done_n" = "$(cat "$STATE.ncdone" 2>/dev/null || echo -1)" ] && [ "$_hash" = "$_prev_hash" ]; then
    nc=$(( $(cat "$STATE.nochurn" 2>/dev/null || echo 0) + 1 )); echo "$nc" > "$STATE.nochurn"
    if [ "$nc" -ge "$(( ${MAX_STALL_PASSES:-2} * 4 ))" ]; then
      say "no-churn: done_n=$done_n and the live job set unchanged for $nc passes -> STALLED (stuck/PENDING jobs)"
      finalize "STALLED"; exit 0
    fi
  else
    echo 0 > "$STATE.nochurn"
  fi
  echo "$done_n" > "$STATE.ncdone"
fi

# Safety net (T2.4): re-arm ONLY if the pass-start reschedule actually FAILED with no successor id; on a
# second failure write an ORPHAN marker so the dead chain is detectable.
if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
  reschedule
  if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
    say "ORPHAN: could not queue a successor twice (PEND cap?) -- chain may stop"
    { echo "BED watchdog could not reschedule at $(ts): bsub failed twice (likely the LSF pending-job cap)."
      echo "Re-arm manually:  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh"
    } > "$PIPELINE_ROOT/PIPELINE_ORPHANED.txt"
  fi
fi
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
