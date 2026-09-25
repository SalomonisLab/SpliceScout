#!/usr/bin/env bash
# =============================================================================
# watchdog.sh -- the self-driving controller for the single AltAnalyze (PSI) job.
# Each pass it:
#   1. (re)schedules the NEXT pass FIRST (survives a mid-pass walltime kill),
#   2. if the PSI table is present and no work job is live -> COMPLETE,
#   3. if the job died with no output -> resubmit it (up to MAX_RESUBMITS) else STALLED,
#   4. else (job still RUNNING) just waits for the next pass.
# Counts only the WORK job (^${JOB_TAG}_job), never itself, so nlive can reach 0.
# This is the LAST stage in the chain (download->STAR->BED->PSI); finalize() kicks nothing downstream.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_psi.sh"
# shared error-log + email (cluster jobs email the user directly). Fallback to no-ops if missing (old deploy).
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
  psi_qopt
  # GUARD: if both `date` variants failed/returned empty, never pass an empty value to `bsub -b`
  # (empty -b is a hard bsub error that kills the chain) -- omit -b and let LSF dispatch ASAP.
  local _bopt=()
  [ -n "$when" ] && _bopt=(-b "$when")
  # bounded, as in the download stage: a bsub blocked at the pending-job cap must not hang the pass
  out=$(timeout "${WATCHDOG_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M 1000 -W 20 "${_bopt[@]+"${_bopt[@]}"}" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>&1)
  RESCHED_RC=$?
  [ -n "$when" ] || when="ASAP (date unavailable -> -b omitted)"
  WATCHDOG_NEXT_JID=$(printf '%s' "$out" | psi_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?}, rc=$RESCHED_RC)"
}

finalize() {                            # $1 = COMPLETE | STALLED
  local status="$1"
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"   # SEPARATE line (bash 4.2 + set -u)
  psi_finalize_once || { say "finalize already claimed by a concurrent pass -> skip"; return 0; }
  rm -f "$STATE.firstpass" "$STATE.passes" "$STATE.lastpass" 2>/dev/null   # a later re-arm starts a fresh backstop window
  # cancel ALL queued watchdog successors except THIS job. GUARD bjobs with `timeout 60` so a hung/wedged
  # bjobs cannot block finalize; an empty result just means "nothing to cancel" (NOT "all done").
  local _self _wj _bjout
  _self="${LSB_JOBID:-}"
  _bjout=$(timeout 60 bjobs -noheader -o jobid -J "${JOB_TAG}_watchdog" 2>/dev/null) || _bjout=""
  for _wj in $_bjout; do
    [ "$_wj" = "$_self" ] && continue
    bkill "$_wj" >/dev/null 2>&1
  done
  # PARTIAL if the upstream BED run was incomplete (launcher dropped the marker). PSI still ran on whatever
  # BEDs were present; we just report it honestly and skip the optional uploaded-toolkit cleanup.
  local partial=0
  { [ "$status" = "STALLED" ] || [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ]; } && partial=1
  local ng=0
  [ -s "$GROUPS_FILE" ] && while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) ng=$((ng+1)) ;; esac; done < "$GROUPS_FILE"
  {
    echo "AltAnalyze splicing (PSI) stage $status at $(ts)"
    [ "$partial" = "1" ] && echo "*** PARTIAL: upstream BED incomplete and/or this stage STALLED ***"
    if psi_done; then echo "PSI table: PRESENT -> $PSI_OUT/AltResults/AlternativeOutput"
    else echo "PSI table: MISSING or INCOMPLETE (no clean AltAnalyze exit and not every comparison written)"; fi
    if [ -s "$GROUPS_FILE" ] && [ -s "$COMPS_FILE" ]; then
      echo "Comparisons: $(psi_comparisons_produced) of $(psi_comparisons_expected) requested dPSI files written ($ng grouped samples); per-comparison detail: $PIPELINE_ROOT/PSI_COMPARISONS.tsv"
    else
      echo "Comparisons: NONE (no usable 2-group split -- AltAnalyze's RNASeq workflow cannot run groupless)"
    fi
    echo "BED input: $BED_INPUT_DIR"
    if [ "$status" = "STALLED" ]; then
      echo
      echo "AltAnalyze did not finish after $(cat "$STATE.resub" 2>/dev/null || echo 0) resubmit(s)."
      echo "Inspect $LOG_DIR/psi_job.err (likely a missing species DB, a bad BED, or an R/python problem)."
    fi
  } > "$rep"
  # optional cleanup: ONLY remove an UPLOADED altanalyze_home (one that lives UNDER PIPELINE_ROOT), and only
  # on a clean COMPLETE. NEVER touches a found-on-cluster ALTANALYZE_HOME (e.g. the lab install).
  if [ "$status" = "COMPLETE" ] && [ "$partial" = "0" ] && [ "${CLEANUP_TOOLS_WHEN_DONE:-0}" = "1" ]; then
    case "$ALTANALYZE_HOME" in
      "$PIPELINE_ROOT"/*) [ "$ALTANALYZE_HOME" != "/" ] && rm -rf "$ALTANALYZE_HOME" 2>/dev/null \
                            && say "cleanup: removed uploaded AltAnalyze home $ALTANALYZE_HOME (kept PSI results)" ;;
      *) say "cleanup: ALTANALYZE_HOME is external ($ALTANALYZE_HOME) -> left untouched" ;;
    esac
  fi
  # After a clean COMPLETE, optionally compress the kept uncompressed data (BEDs + PSI text outputs) to
  # reclaim space. Runs as its OWN LSF job (can be long) so it never blocks finalize; "off" => skip.
  if [ "$status" = "COMPLETE" ] && [ "$partial" = "0" ] \
     && [ "${COMPRESS_WHEN_DONE:-off}" != "off" ] && [ ! -f "$PIPELINE_ROOT/COMPRESSION_COMPLETE.txt" ]; then
    psi_qopt
    if bsub -L /bin/bash -n "${COMPRESS_THREADS:-8}" -W 1108:00 -M 8000 -R "span[hosts=1]" \
            -J "${JOB_TAG}_compress" -o "$LOG_DIR/compress.out" -e "$LOG_DIR/compress.err" \
            ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/compress_done.sh" >/dev/null 2>&1; then
      say "submitted post-completion compression job (${JOB_TAG}_compress, mode=${COMPRESS_WHEN_DONE})"
    else
      say "WARNING: failed to submit the compression job (bsub error) -- run is COMPLETE but data left uncompressed"
    fi
  fi
  # EMAIL the user: error on STALLED (needs attention), a progress note on COMPLETE.
  if [ "$status" = "STALLED" ]; then
    notify_error "PSI stage STALLED" "$(head -20 "$rep" 2>/dev/null)" "psi-stalled"
    notify_diagnose "$JOB_TAG" "$PIPELINE_ROOT" "$SCRIPTS_DIR" "$LOG_DIR"
  else
    notify_update "PSI stage COMPLETE" "$(head -10 "$rep" 2>/dev/null)"
  fi
  say "FINALIZED ($status; partial=$partial) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then
  say "already finalized -> stop"; exit 0
fi
# Reclaim a STALE finalize lock (no marker exists => a prior finalize died before writing one).
[ -d "$PIPELINE_ROOT/.finalized.lock" ] && rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null

# SINGLE-FLIGHT: only ONE pass at a time (a nudged + a timed successor would otherwise double-submit).
exec 8>"$PIPELINE_ROOT/.watchdog.run.lock" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  flock -n 8 2>/dev/null || { say "another watchdog pass holds the run lock -> exit"; exit 0; }
fi

reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)

# ABSOLUTE BACKSTOP (bjobs-independent): cap by pass-count AND wall-clock so a permanently-PENDING job or a
# persistently-broken bjobs can never loop forever with no human signal.
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

# SNAPSHOT WITH RC: bjobs FAILED or EMPTY -> unreliable -> skip the pass. (This running watchdog always
# appears in bjobs, so a truly empty list means bjobs failed.)
LIVE="$(psi_snapshot)"; PSI_SNAP_RC=$?
if [ "$PSI_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$PSI_SNAP_RC) -- skipping this pass"
  exit 0
fi
nlive=$(psi_count_work "$LIVE")     # 0 or 1 (pure-bash; grep -c is empty on compute nodes)
done=0; psi_done && done=1
say "progress: psi_done=$done ; nlive=$nlive ; resubmits=$(cat "$STATE.resub" 2>/dev/null || echo 0)"

# 1) completion: PSI table present AND no live work job
if [ "$done" -eq 1 ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

# 2) the job died with no output -> resubmit (bounded), else STALLED
if [ "$done" -eq 0 ] && [ "$nlive" -eq 0 ] && ! psi_job_is_live; then
  nresub=$(cat "$STATE.resub" 2>/dev/null || echo 0)
  if [ "$nresub" -ge "${MAX_RESUBMITS:-2}" ]; then
    say "AltAnalyze not done and resubmit budget (${MAX_RESUBMITS:-2}) exhausted -> STALLED"
    finalize "STALLED"; exit 0
  fi
  beds=("$BED_INPUT_DIR"/*__junction.bed)
  if [ "${#beds[@]}" -eq 0 ]; then
    say "no junction BEDs present yet -- waiting (no resubmit)"
  else
    # WALLTIME / MEM SELF-HEAL: if the dead job hit an LSF limit, escalate BEFORE resubmitting so the retry
    # can actually finish (else the same -W/-M just kills it again). Reads the LAST termination reason from
    # the job's accumulated -o log (awk, not grep -c -> reliable on compute nodes). WALL -> queue max on a
    # walltime kill; MEM -> +50% per memory kill. (config.sh is re-sourced each pass, so this re-derives
    # from the persistent log every time and converges.)
    _jo="$LOG_DIR/psi_job.out"
    _term=$(awk 'match($0,/TERM_(RUNLIMIT|MEMLIMIT)/){m=substr($0,RSTART,RLENGTH)} END{print m}' "$_jo" 2>/dev/null)
    _nmem=$(awk '/TERM_MEMLIMIT/{n++} END{print n+0}' "$_jo" 2>/dev/null); _nmem=${_nmem:-0}
    [ "$_term" = "TERM_RUNLIMIT" ] && { WALL="1108:00"; export WALL; say "previous job hit the WALLTIME limit -> resubmitting at the queue max (-W $WALL)"; }
    [ "${_nmem:-0}" -gt 0 ] && { MEM_MB=$(( MEM_MB + MEM_MB * _nmem / 2 )); export MEM_MB; say "previous job hit the MEM limit x$_nmem -> bumped -M to $MEM_MB"; }
    if psi_submit_job >/dev/null; then
      nresub=$((nresub+1)); echo "$nresub" > "$STATE.resub"
      say "resubmitted the AltAnalyze job (attempt $nresub/${MAX_RESUBMITS:-2})"
    fi
  fi
fi

# 2b) RUN-but-FROZEN deadlock self-heal. The checks above only act when nlive==0; a job that LSF still calls
# RUN but is DEADLOCKED (e.g. AltAnalyze's import parent hung on a dead multiprocessing worker -- the 14h A549
# freeze) is otherwise invisible until the 14-day backstop, and writes NO marker so the PC poller never
# emails. Detect it by cpu_used NOT advancing: a busy job (even a legitimately long, quiet AltAnalyze phase)
# always burns cpu, so frozen cpu_used over IDLE_STALL_PASSES passes == a true deadlock -> bkill + resubmit
# (counts toward MAX_RESUBMITS; the BED preflight quarantines the usual corrupt-bed cause) + EMAIL.
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
        notify_error "PSI job DEADLOCKED (frozen)" "The AltAnalyze PSI job ran but FROZE (cpu_used stuck at $_cpu) for $_idle watchdog passes; resubmit budget (${MAX_RESUBMITS:-2}) exhausted -> STALLED. Inspect $LOG_DIR/psi_job.err and the BED inputs." "psi-frozen-stall"
        finalize "STALLED"; exit 0
      fi
      say "FROZEN: cpu_used stuck at '$_cpu' for $_idle passes -> killing + resubmitting (self-heal)"
      notify_error "PSI job DEADLOCKED -> auto-restarting" "The AltAnalyze PSI job was RUNNING but FROZEN (cpu_used stuck at $_cpu) for $_idle passes. Killing it and resubmitting (attempt $((nresub+1))/${MAX_RESUBMITS:-2}); the BED preflight quarantines the usual cause (a corrupt intron bed)." "psi-frozen-kill"
      for _j in $(timeout 30 bjobs -noheader -o jobid -J "${JOB_TAG}_job" 2>/dev/null); do bkill "$_j" >/dev/null 2>&1; done
      echo 0 > "$STATE.idle"; rm -f "$STATE.cpu" 2>/dev/null
      if psi_submit_job >/dev/null; then nresub=$((nresub+1)); echo "$nresub" > "$STATE.resub"; say "resubmitted after freeze (attempt $nresub/${MAX_RESUBMITS:-2})"; fi
    fi
  else
    echo 0 > "$STATE.idle"   # PEND/UNKNOWN -> not a freeze; reset the idle counter
  fi
fi

# 2c) every requested comparison is WRITTEN but the job is still RUN and FROZEN -- stuck in a post-analysis tail
# (e.g. a visualization step blocking on a dead web service, which ate the end of a 107 h run). The analysis is
# complete, so KILL it (no resubmit): the next pass sees nlive==0 and finalizes COMPLETE instead of waiting weeks.
if [ "$done" -eq 1 ] && [ "$nlive" -ge 1 ]; then
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
    say "liveness (outputs complete): job RUN cpu_used='$_cpu' idle_passes=$_idle/${IDLE_STALL_PASSES:-3}"
    if [ "$_idle" -ge "${IDLE_STALL_PASSES:-3}" ]; then
      say "all $(psi_comparisons_expected) comparisons written but the job is FROZEN -> killing its post-analysis tail"
      for _j in $(timeout 30 bjobs -noheader -o jobid -J "${JOB_TAG}_job" 2>/dev/null); do bkill "$_j" >/dev/null 2>&1; done
      echo 0 > "$STATE.idle"; rm -f "$STATE.cpu" 2>/dev/null
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
    { echo "PSI watchdog could not reschedule at $(ts): bsub failed twice (likely the LSF pending-job cap)."
      echo "Re-arm manually:  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh"
    } > "$PIPELINE_ROOT/PIPELINE_ORPHANED.txt"
    notify_error "PSI watchdog ORPHANED" "The PSI watchdog could not queue a successor twice (likely the LSF pending-job cap), so the self-driving chain may stop. Re-arm: bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh" "psi-orphaned"
  fi
fi
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
