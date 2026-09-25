#!/usr/bin/env bash
# =============================================================================
# watchdog.sh -- the self-driving controller for the single concordance job.
# Each pass it:
#   1. (re)schedules the NEXT pass FIRST (survives a mid-pass walltime kill),
#   2. if the concordance results are present and no work job is live -> COMPLETE,
#   3. if the job died with no output -> resubmit it (up to MAX_RESUBMITS) else STALLED,
#   4. else (job still RUNNING) just waits for the next pass.
# Counts only the WORK job (^${JOB_TAG}_job), never itself, so nlive can reach 0.
# This is the LAST stage in the chain (download->STAR->BED->PSI->concordance); finalize() kicks nothing.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_concordance.sh"
if [ -f "$HERE/lib_notify.sh" ]; then source "$HERE/lib_notify.sh"; else
  log_event(){ :; }; notify_error(){ :; }; notify_update(){ :; }; fi
set -u; shopt -s nullglob

LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

# Reschedule-first: the NEXT pass is queued at the START of each pass, so a mid-pass walltime kill can
# NEVER break the self-driving chain. finalize() cancels the successor once the stage is actually done.
WATCHDOG_NEXT_JID=""
RESCHED_RC=0
reschedule() {
  local when out
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  concord_qopt
  local _bopt=()
  [ -n "$when" ] && _bopt=(-b "$when")
  # bounded, as in the download stage: a bsub blocked at the pending-job cap must not hang the pass
  out=$(timeout "${WATCHDOG_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M 1000 -W 20 "${_bopt[@]+"${_bopt[@]}"}" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>&1)
  RESCHED_RC=$?
  [ -n "$when" ] || when="ASAP (date unavailable -> -b omitted)"
  WATCHDOG_NEXT_JID=$(printf '%s' "$out" | concord_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?}, rc=$RESCHED_RC)"
}

finalize() {                            # $1 = COMPLETE | STALLED
  local status="$1"
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"
  concord_finalize_once || { say "finalize already claimed by a concurrent pass -> skip"; return 0; }
  rm -f "$STATE.firstpass" "$STATE.passes" "$STATE.lastpass" 2>/dev/null   # a later re-arm starts a fresh backstop window
  local _self _wj _bjout
  _self="${LSB_JOBID:-}"
  _bjout=$(timeout 60 bjobs -noheader -o jobid -J "${JOB_TAG}_watchdog" 2>/dev/null) || _bjout=""
  for _wj in $_bjout; do
    [ "$_wj" = "$_self" ] && continue
    bkill "$_wj" >/dev/null 2>&1
  done
  local partial=0
  { [ "$status" = "STALLED" ] || [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ]; } && partial=1
  local natlas=0
  for _d in "$RESULTS_DIR"/*/concordance.txt; do [ -e "$_d" ] && natlas=$((natlas+1)); done
  {
    echo "Splicing concordance stage $status at $(ts)"
    [ "$partial" = "1" ] && echo "*** PARTIAL: upstream PSI incomplete and/or this stage STALLED ***"
    echo "Scored atlases with results: $natlas"
    for _d in "$RESULTS_DIR"/*/; do
      [ -d "$_d" ] || continue
      _nm="$(basename "$_d")"
      if [ -s "$_d/ranked_concordance_summary.txt" ]; then echo "  $_nm: $_d/ranked_concordance_summary.txt"
      elif [ -s "$_d/concordance.txt" ]; then echo "  $_nm: $_d/concordance.txt (unranked)"; fi
    done
    echo "Drug signatures scored from: $PSI_EVENTS_DIR"
    if [ "$status" = "STALLED" ]; then
      echo
      echo "Concordance did not finish after $(cat "$STATE.resub" 2>/dev/null || echo 0) resubmit(s)."
      echo "Inspect $LOG_DIR/concord_job.err and $LOG_DIR/scorer_*.log (likely missing PSI signatures,"
      echo "an unreadable query atlas, or AltAnalyze export/UI/unique not on PYTHONPATH)."
    fi
  } > "$rep"
  if [ "$status" = "STALLED" ]; then
    notify_error "Concordance stage STALLED" "$(head -20 "$rep" 2>/dev/null)" "concord-stalled"
    notify_diagnose "$JOB_TAG" "$PIPELINE_ROOT" "$SCRIPTS_DIR" "$LOG_DIR"
  else
    notify_update "Concordance stage COMPLETE" "$(head -12 "$rep" 2>/dev/null)"
  fi
  say "FINALIZED ($status; partial=$partial) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then
  say "already finalized -> stop"; exit 0
fi
[ -d "$PIPELINE_ROOT/.finalized.lock" ] && rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null

# SINGLE-FLIGHT: only ONE pass at a time.
exec 8>"$PIPELINE_ROOT/.watchdog.run.lock" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  flock -n 8 2>/dev/null || { say "another watchdog pass holds the run lock -> exit"; exit 0; }
fi

reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)

# ABSOLUTE BACKSTOP (bjobs-independent): cap by pass-count AND wall-clock.
_now=$(date +%s)
# A RE-ARMED chain gets a NEW window. The caps bound ONE continuous chain, but the window state survives a finalize,
# so a stage re-armed later (a targeted re-run, a manual or AI re-arm) inherited its old firstpass and hit the wall
# cap on its very first pass (the 2026-09-22 K562/A549 re-runs). A gap of BACKSTOP_RESET_GAP_HOURS since the last
# pass means the chain had stopped -> start over. finalize() clears the window as well. (A state from before this
# fix has no .lastpass: the .passes file's age stands in for it.)
_lastp=$(cat "$STATE.lastpass" 2>/dev/null || stat -c %Y "$STATE.passes" 2>/dev/null || echo "$_now")
if [ "$(( _now - _lastp ))" -ge "$(( ${BACKSTOP_RESET_GAP_HOURS:-12} * 3600 ))" ]; then
  rm -f "$STATE.firstpass" "$STATE.passes"
  say "backstop: last pass was $(( (_now - _lastp) / 3600 ))h ago -> re-armed chain, new pass/wall-clock window"
fi
echo "$_now" > "$STATE.lastpass"
[ -f "$STATE.firstpass" ] || echo "$_now" > "$STATE.firstpass"
_first=$(cat "$STATE.firstpass" 2>/dev/null || echo "$_now")
_passes=$(( $(cat "$STATE.passes" 2>/dev/null || echo 0) + 1 )); echo "$_passes" > "$STATE.passes"
if [ "$_passes" -ge "${ABSOLUTE_MAX_PASSES:-960}" ] || [ "$(( _now - _first ))" -ge "$(( ${MAX_WALL_HOURS:-336} * 3600 ))" ]; then
  say "BACKSTOP: passes=$_passes wall=$(( _now - _first ))s exceeded the cap -> STALLED"
  finalize "STALLED"; exit 0
fi

# SNAPSHOT WITH RC: bjobs FAILED or EMPTY -> unreliable -> skip the pass.
LIVE="$(concord_snapshot)"; CONCORD_SNAP_RC=$?
if [ "$CONCORD_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$CONCORD_SNAP_RC) -- skipping this pass"
  exit 0
fi
nlive=$(concord_count_work "$LIVE")
done=0; concord_done && done=1
say "progress: concord_done=$done ; nlive=$nlive ; resubmits=$(cat "$STATE.resub" 2>/dev/null || echo 0)"

# 1) completion: results present AND no live work job
if [ "$done" -eq 1 ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

# 2) the job died with no output -> resubmit (bounded), else STALLED
if [ "$done" -eq 0 ] && [ "$nlive" -eq 0 ] && ! concord_job_is_live; then
  nresub=$(cat "$STATE.resub" 2>/dev/null || echo 0)
  if [ "$nresub" -ge "${MAX_RESUBMITS:-2}" ]; then
    say "concordance not done and resubmit budget (${MAX_RESUBMITS:-2}) exhausted -> STALLED"
    finalize "STALLED"; exit 0
  fi
  sigs=("$PSI_EVENTS_DIR"/PSI.*_vs_*.txt "$PSI_EVENTS_DIR"/PSI.*_vs_*.txt.gz)   # PSI compression may gzip them
  if [ "${#sigs[@]}" -eq 0 ]; then
    say "no drug signatures present yet (PSI not finished) -- waiting (no resubmit)"
  else
    # WALLTIME / MEM SELF-HEAL (see psi_template/watchdog.sh): escalate before resubmitting if the dead job
    # hit an LSF limit. WALL -> queue max on a walltime kill; MEM -> +50% per memory kill (awk, compute-safe).
    _jo="$LOG_DIR/concord_job.out"
    _term=$(awk 'match($0,/TERM_(RUNLIMIT|MEMLIMIT)/){m=substr($0,RSTART,RLENGTH)} END{print m}' "$_jo" 2>/dev/null)
    _nmem=$(awk '/TERM_MEMLIMIT/{n++} END{print n+0}' "$_jo" 2>/dev/null); _nmem=${_nmem:-0}
    [ "$_term" = "TERM_RUNLIMIT" ] && { WALL="1108:00"; export WALL; say "previous job hit the WALLTIME limit -> resubmitting at the queue max (-W $WALL)"; }
    [ "${_nmem:-0}" -gt 0 ] && { MEM_MB=$(( MEM_MB + MEM_MB * _nmem / 2 )); export MEM_MB; say "previous job hit the MEM limit x$_nmem -> bumped -M to $MEM_MB"; }
    if concord_submit_job >/dev/null; then
      nresub=$((nresub+1)); echo "$nresub" > "$STATE.resub"
      say "resubmitted the concordance job (attempt $nresub/${MAX_RESUBMITS:-2})"
    fi
  fi
fi

# 2b) RUN-but-FROZEN deadlock self-heal (see psi_template/watchdog.sh). A job LSF still calls RUN but is
# deadlocked is invisible to the nlive==0 checks and writes no marker. Detect frozen cpu_used over
# IDLE_STALL_PASSES passes (a busy job always burns cpu) -> bkill + resubmit (counts toward MAX_RESUBMITS)
# + EMAIL. Only RUN (not PEND) jobs are evaluated.
if [ "$done" -eq 0 ] && [ "$nlive" -ge 1 ]; then
  _ji=$(timeout 30 bjobs -noheader -o 'stat cpu_used' -J "${JOB_TAG}_job" 2>/dev/null | head -1)
  _jstat=$(printf '%s' "$_ji" | awk '{print $1}')
  _cpu=$(printf '%s' "$_ji" | awk '{print $2}')
  if [ "$_jstat" = "RUN" ] && [ -n "$_cpu" ]; then
    if [ "$_cpu" = "$(cat "$STATE.cpu" 2>/dev/null || echo '')" ]; then
      _idle=$(( $(cat "$STATE.idle" 2>/dev/null || echo 0) + 1 ))
    else
      _idle=0
    fi
    echo "$_idle" > "$STATE.idle"; echo "$_cpu" > "$STATE.cpu"
    say "liveness: job RUN cpu_used='$_cpu' idle_passes=$_idle/${IDLE_STALL_PASSES:-3}"
    if [ "$_idle" -ge "${IDLE_STALL_PASSES:-3}" ]; then
      nresub=$(cat "$STATE.resub" 2>/dev/null || echo 0)
      if [ "$nresub" -ge "${MAX_RESUBMITS:-2}" ]; then
        say "FROZEN: cpu_used stuck at '$_cpu' for $_idle passes AND resubmit budget exhausted -> STALLED"
        notify_error "Concordance job DEADLOCKED (frozen)" "The concordance job ran but FROZE (cpu_used stuck at $_cpu) for $_idle passes; resubmit budget exhausted -> STALLED. Inspect $LOG_DIR/concord_job.err." "concord-frozen-stall"
        finalize "STALLED"; exit 0
      fi
      say "FROZEN: cpu_used stuck at '$_cpu' for $_idle passes -> killing + resubmitting (self-heal)"
      notify_error "Concordance job DEADLOCKED -> auto-restarting" "The concordance job was RUNNING but FROZEN (cpu_used stuck at $_cpu) for $_idle passes. Killing + resubmitting (attempt $((nresub+1))/${MAX_RESUBMITS:-2})." "concord-frozen-kill"
      for _j in $(timeout 30 bjobs -noheader -o jobid -J "${JOB_TAG}_job" 2>/dev/null); do bkill "$_j" >/dev/null 2>&1; done
      echo 0 > "$STATE.idle"; rm -f "$STATE.cpu" 2>/dev/null
      if concord_submit_job >/dev/null; then nresub=$((nresub+1)); echo "$nresub" > "$STATE.resub"; say "resubmitted after freeze (attempt $nresub/${MAX_RESUBMITS:-2})"; fi
    fi
  else
    echo 0 > "$STATE.idle"
  fi
fi

# Safety net: re-arm ONLY if the pass-start reschedule actually FAILED with no successor id.
if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
  reschedule
  if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
    say "ORPHAN: could not queue a successor twice (PEND cap?) -- chain may stop"
    { echo "Concordance watchdog could not reschedule at $(ts): bsub failed twice (likely the LSF pending-job cap)."
      echo "Re-arm manually:  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh"
    } > "$PIPELINE_ROOT/PIPELINE_ORPHANED.txt"
    notify_error "Concordance watchdog ORPHANED" "The concordance watchdog could not queue a successor twice (likely the LSF pending-job cap); the chain may stop. Re-arm: bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh" "concord-orphaned"
  fi
fi
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
