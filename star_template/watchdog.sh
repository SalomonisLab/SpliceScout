#!/usr/bin/env bash
# =============================================================================
# watchdog.sh -- the self-driving controller. Each pass it:
#   1. resubmits any sample with no valid BAM and no live job,
#   2. counts progress (done / expected) over the FIXED sample list,
#   3. declares COMPLETE (all done, no live work jobs) or STALLED (no forward
#      progress for MAX_STALL_PASSES idle passes), else
#   4. reschedules itself WATCHDOG_INTERVAL_MIN into the future via `bsub -b`.
# Counts only WORK jobs (^${JOB_TAG}_star_), never itself, so nlive can reach 0.
# Started by run_star_pipeline.sh; needs no babysitting.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
if [ -f "$HERE/lib_notify.sh" ]; then source "$HERE/lib_notify.sh"; else
  log_event(){ :; }; notify_error(){ :; }; notify_update(){ :; }; fi
[ -f "$PIPELINE_ROOT/RESOLVED_INDEX.env" ] && source "$PIPELINE_ROOT/RESOLVED_INDEX.env"  # resolved GENOME_DIR + BUILD_JID
set -u; shopt -s nullglob
star_load_modules                       # samtools, for quickcheck

LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }

# Reschedule-first: the NEXT pass is queued at the START of each pass, so a mid-pass walltime kill
# (e.g. bsub blocked on the LSF pending-job threshold) can NEVER break the self-driving chain.
# finalize() cancels the successor once the pipeline is actually done.
WATCHDOG_NEXT_JID=""
RESCHED_RC=0
reschedule() {
  local when out
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)
  star_qopt
  # capture bsub's OWN exit status (not the pipeline-tail rc of star_jobid) so the safety-net can tell a
  # genuine submit FAILURE (re-arm) from a successful submit whose id just failed to parse (do NOT re-arm,
  # else two parallel watchdog chains). RESCHED_RC drives both decisions.
  # bounded, as in the download stage: a bsub blocked at the pending-job cap must not hang the pass
  out=$(timeout "${WATCHDOG_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M 1000 -W 20 -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>&1)
  RESCHED_RC=$?
  WATCHDOG_NEXT_JID=$(printf '%s' "$out" | star_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?}, rc=$RESCHED_RC)"
}

finalize() {                            # $1 = COMPLETE | STALLED
  local status="$1" label rest
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"   # SEPARATE line: bash 4.2 + set -u can't see $status declared in the SAME `local`
  # EXACTLY-ONCE (T2.3): an atomic mkdir claim survives NFS flock degradation -- only the first racer
  # past this does the destructive cleanup + chain kick; a concurrent pass returns harmlessly.
  star_finalize_once || { say "finalize already claimed by a concurrent pass -> skip"; return 0; }
  rm -f "$STATE.firstpass" "$STATE.passes" "$STATE.lastpass" 2>/dev/null   # a later re-arm starts a fresh backstop window
  # Cancel ALL queued/duplicate watchdog successors except THIS job -- a double-armed or nudged successor
  # would otherwise re-spawn the chain after we stop (T2.4/T6).
  local _self _wj
  _self="${LSB_JOBID:-}"
  for _wj in $(timeout 60 bjobs -noheader -o jobid -J "${JOB_TAG}_watchdog" 2>/dev/null); do
    [ "$_wj" = "$_self" ] && continue
    bkill "$_wj" >/dev/null 2>&1
  done
  # PARTIAL run (T1.1): disable destructive cleanup/FASTQ purge + write an HONEST marker whenever the
  # dataset may be incomplete. Detected WITHOUT relying on the launcher-written PIPELINE_INCOMPLETE_UPSTREAM
  # marker (that marker's writer is the deploy-generated launcher, which a SCRIPT-ONLY hot-patch does NOT
  # replace) -- the robust signal is the upstream DOWNLOAD's own POSITIVE completion marker. partial=1 if
  # this stage STALLED, OR the incomplete-upstream marker exists, OR the download did NOT write
  # $dlroot/PIPELINE_COMPLETE.txt. The by_study purge below is thus gated on the download's POSITIVE success.
  local partial=0 dlroot
  dlroot="$(cd "$BAM_OUT/.." 2>/dev/null && pwd)"
  if [ "$status" = "STALLED" ] || [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] \
     || { [ -n "${dlroot:-}" ] && [ ! -f "$dlroot/PIPELINE_COMPLETE.txt" ]; }; then
    partial=1
  fi
  {
    echo "STAR alignment pipeline $status at $(ts)"
    [ "$partial" = "1" ] && echo "*** PARTIAL: upstream incomplete and/or this stage STALLED -- NOT all samples aligned; cleanup/deletion DISABLED ***"
    echo "Aligned: $done_n / $exp_n samples"
    echo "BAM dir: $BAM_OUT  ($(du -sh "$BAM_OUT" 2>/dev/null | cut -f1) used)"
    echo
    echo "Per-sample:"
    while IFS=$'\t' read -r label rest; do
      [ -n "$label" ] || continue
      if star_bam_ok "$label"; then printf '  %-44s OK\n' "$label"
      else printf '  %-44s MISSING/INVALID\n' "$label"; fi
    done < "$SAMPLE_LIST"
    if [ "$status" = "STALLED" ]; then
      echo
      echo "Samples with no valid BAM after repeated resubmits -- inspect"
      echo "$LOG_DIR/star_<label>.err (likely a bad/truncated FASTQ, OOM, or"
      echo "persistent scratch shortage):"
      while IFS=$'\t' read -r label rest; do
        [ -n "$label" ] || continue
        star_bam_ok "$label" || echo "  $label"
      done < "$SAMPLE_LIST"
    fi
  } > "$rep"
  # honest partial marker (surfaced by the SpliceScout status probe)
  [ "$partial" = "1" ] && echo "PARTIAL run at $(ts): $done_n/$exp_n aligned. Upstream incomplete and/or STALLED; destructive cleanup + FASTQ purge were DISABLED. Inspect before deleting inputs." \
      > "$PIPELINE_ROOT/PIPELINE_COMPLETE_PARTIAL.txt"
  # SpliceScout BED auto-chain: kick the BED launcher (it self-marks partial if WE stalled). Kick on
  # COMPLETE or STALLED so partial data still flows; bed_launch + BED finalize enforce the partial safety.
  if [ -f "$PIPELINE_ROOT/bed/bed_launch.sh" ]; then
    star_qopt
    bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J "${JOB_TAG}_bed_launch" \
         -o "$PIPELINE_ROOT/bed/launch.out" -e "$PIPELINE_ROOT/bed/launch.err" \
         ${QOPT[@]+"${QOPT[@]}"} "$PIPELINE_ROOT/bed/bed_launch.sh" >/dev/null 2>&1
    say "kicked BED launcher -> $PIPELINE_ROOT/bed/bed_launch.sh"
  fi
  # Tool cleanup (free disk): ONLY on a clean (non-partial) COMPLETE, and ONLY when a FRESH bjobs snapshot
  # confirms no work job is still live (T1.3 -- never rm out from under a still-running resubmit). If the
  # snapshot is unreliable, KEEP the tools (a missed disk-free is harmless; deleting a live job's inputs is not).
  local freshlive _frc
  if [ "$status" = "COMPLETE" ] && [ "$partial" = "0" ] && [ "${CLEANUP_TOOLS_WHEN_DONE:-1}" = "1" ]; then
    freshlive="$(star_snapshot)"; _frc=$?
    if [ "$_frc" -eq 0 ] && [ -n "$(printf '%s' "$freshlive" | tr -d '[:space:]')" ] \
       && ! printf '%s\n' "$freshlive" | grep -qE "^${JOB_TAG}_star_"; then
      # dlroot was resolved above (partial gate). tighter path guard (T1.3): a genuine download root
      # contains STAR_bams AND is >=3 path components
      # deep -- so a misconfigured/shallow BAM_OUT can't widen the rm target to a shared parent.
      if [ -n "${dlroot:-}" ] && [ "$dlroot" != "/" ] && [ -d "$dlroot/STAR_bams" ] \
         && [ "$(printf '%s' "$dlroot" | awk -F/ '{print NF-1}')" -ge 3 ]; then
        # by_study is removed ONLY once it holds no FASTQ at all. With DELETE_FASTQ_AFTER_BAM=1 every aligned
        # sample's FASTQ is already gone, so what remains is the per-study scaffolding. Any FASTQ still here
        # is either (a) the user's "keep FASTQ" choice (DELETE_FASTQ_AFTER_BAM=0) or (b) a sample that was
        # never aligned (dropped / orphan mate / mixed layout) -- deleting it would destroy data nothing else
        # holds. (Before, this rm ran unconditionally and silently overrode the GUI keep-FASTQ toggle.)
        if [ -d "$dlroot/by_study" ] && [ -n "$(find "$dlroot/by_study" -type f \( -name '*.fastq.gz' \
             -o -name '*.fq.gz' -o -name '*.fastq' -o -name '*.fq' \) -print -quit 2>/dev/null)" ]; then
          say "cleanup: KEPT $dlroot/by_study -- FASTQ files remain (DELETE_FASTQ_AFTER_BAM=${DELETE_FASTQ_AFTER_BAM:-1}, or unaligned samples)"
        else
          rm -rf "$dlroot/by_study" 2>/dev/null
        fi
        rm -f "$dlroot"/*.sh "$dlroot"/*.py 2>/dev/null
        say "cleanup: removed download bundle scripts (+ by_study if it held no FASTQ) under $dlroot"
      else
        say "cleanup: SKIPPED -- dlroot '${dlroot:-}' failed the run-root safety guard (kept tools)"
      fi
    else
      say "cleanup: SKIPPED -- a work job is still live or bjobs is unreliable (kept tools, no risky rm)"
    fi
  fi
  if [ "$status" = "STALLED" ]; then
    notify_error "STAR stage STALLED" "$(head -20 "$rep" 2>/dev/null)" "star-stalled"
    notify_diagnose "$JOB_TAG" "$PIPELINE_ROOT" "$SCRIPTS_DIR" "$LOG_DIR"
  else notify_update "STAR stage COMPLETE" "$(head -10 "$rep" 2>/dev/null)"; fi
  say "FINALIZED ($status; partial=$partial) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then
  say "already finalized -> stop"; exit 0
fi
# Reclaim a STALE finalize lock: we only reach here when NO PIPELINE_*.txt marker exists, so if
# .finalized.lock is present a prior finalize was killed BEFORE writing its marker -- otherwise it would
# wedge every future finalize (the lock is never otherwise removed). Safe to clear now.
[ -d "$PIPELINE_ROOT/.finalized.lock" ] && rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null
[ -f "$SAMPLE_LIST" ] || { say "no sample list at $SAMPLE_LIST -- stopping"; exit 1; }

# SINGLE-FLIGHT (T2.3): only ONE watchdog pass runs at a time. A nudged successor colliding with a timed
# one (or a double-arm) would otherwise resubmit the same sample twice and double-kick the chain. Hold a
# dedicated lock for the whole pass; a loser just exits (the winner already reschedule-first'd a successor).
exec 8>"$PIPELINE_ROOT/.watchdog.run.lock" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  flock -n 8 2>/dev/null || { say "another watchdog pass holds the run lock -> exit"; exit 0; }
fi

reschedule                       # queue the NEXT pass FIRST (survives a mid-pass walltime kill)

# ABSOLUTE BACKSTOP (T2.2), bjobs-INDEPENDENT: a permanently-PENDING job keeps nlive>=1 so the normal
# STALL never fires, and an overloaded cluster can break bjobs every pass (skip-forever). Either way, cap
# the run by pass-count AND wall-clock so it can never loop/skip silently with no human signal.
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
  exp_n=$(star_expected_count); done_n=$(star_done_count); nlive=0
  say "BACKSTOP: passes=$_passes wall=$(( _now - _first ))s exceeded the cap -> STALLED (inspect stuck/PENDING jobs)"
  finalize "STALLED"; exit 0
fi

# SNAPSHOT WITH RC (T2.1): bjobs FAILED (rc!=0) or returned EMPTY -> unreliable this instant. Acting on it
# would resubmit running jobs and/or falsely finalize, so skip the WHOLE pass (the queued successor retries).
LIVE="$(star_snapshot)"; STAR_SNAP_RC=$?     # capture bjobs rc in the PARENT (command-subst is a subshell)
if [ "$STAR_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$STAR_SNAP_RC) -- skipping resubmit + completion this pass"
  exit 0
fi
# reuse the SAME snapshot for the live-work count (a 2nd bjobs could disagree). PURE-BASH count -- grep -c
# returns empty on this cluster's compute nodes, which wedged the gate (hit LIVE on the BED stage).
nlive=$(star_count_work "$LIVE")
exp_n=$(star_expected_count)
done_n=$(star_done_count)

# SANITY FLOOR (T2.1): if live-work COLLAPSED >50% vs last pass with NO new BAMs, the snapshot is a
# suspected partial bjobs -> skip resubmit+finalize this pass (don't resubmit jobs that are really alive).
_prev_nlive=$(cat "$STATE.nlive" 2>/dev/null || echo -1)
_prev_done=$(cat "$STATE.donen" 2>/dev/null || echo -1)
echo "$nlive" > "$STATE.nlive"; echo "$done_n" > "$STATE.donen"
if [ "$_prev_nlive" -gt 1 ] && [ "$nlive" -lt "$(( _prev_nlive / 2 ))" ] && [ "$done_n" -le "$_prev_done" ]; then
  say "WARNING: live work jobs collapsed $_prev_nlive->$nlive with no new BAMs (suspect partial bjobs) -- skipping this pass"
  exit 0
fi

# 1) resubmit missing/invalid samples that have no live job -- gated by the bulk snapshot AND a targeted
#    per-name re-check (a partial bulk snapshot can't fool the targeted bjobs), plus a missing-FASTQ guard.
resub=0
while IFS=$'\t' read -r label f1 f2; do
  [ -n "$label" ] || continue
  star_bam_ok "$label" && continue
  star_is_dropped "$label" && continue        # already gave up on this sample (drop-after-N)
  jn="$(star_jobname "$label")"
  star_has_live "$jn" "$LIVE" && continue
  star_job_is_live "$jn" && continue        # targeted re-verify (T2.1): fail-closed against partial snapshots
  if [ "${f1:-NA}" != "NA" ] && [ -n "${f1:-}" ] && ! star_fastq_exists "$f1"; then
    star_drop_sample "$label" "fastq-gone"
    say "UNRECOVERABLE: $label has no valid BAM and its source FASTQ is gone -- DROPPED (re-download needed to recover)"
    continue
  fi
  # not done + not live = the last alignment FAILED -> count it; DROP after STAR_MAX_FAILS so one bad
  # sample can't STALL the stage (mirrors download/BED).
  _n=$(star_bump_attempt "$label")
  if [ "$_n" -gt "${STAR_MAX_FAILS:-3}" ]; then
    star_drop_sample "$label" "alignment"
    say "DROPPED $label after $((_n - 1)) failed alignments -> logged to star_dropped.txt"
    continue
  fi
  star_submit_sample "$label" "$f1" "${f2:-NA}" >/dev/null && resub=$((resub+1))
done < "$SAMPLE_LIST"
[ "$resub" -gt 0 ] && say "resubmitted $resub missing/invalid sample(s)"
dropped_n=$(star_dropped_count)
say "progress: $done_n/$exp_n aligned ($dropped_n dropped after ${STAR_MAX_FAILS:-3} fails) ; $nlive live work job(s) ; $resub resubmitted this pass"

# MELTDOWN GUARD: a large fraction dropped = STAR/index/FASTQ globally broken -> STALL, not a false
# COMPLETE with most BAMs missing (which would silently feed an almost-empty BED+PSI).
_ceiling=$(( exp_n / 10 )); [ "$_ceiling" -lt 5 ] && _ceiling=5
if [ "$dropped_n" -gt "$_ceiling" ] && [ "$nlive" -eq 0 ]; then
  say "MELTDOWN: $dropped_n/$exp_n dropped (> $_ceiling = >10%) -> STALLED (inspect star_dropped.txt + logs, fix, re-run)"
  finalize "STALLED"; exit 0
fi

# 2) completion: every sample done OR dropped, AND no live work job
if [ "$(( done_n + dropped_n ))" -ge "$exp_n" ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

# 3) stall: no live jobs AND no forward progress for MAX_STALL_PASSES passes; OR no-churn (done_n AND the
#    live-work-name SET both unchanged) for 4x MAX_STALL_PASSES (catches a permanently-PENDING/looping job
#    that keeps nlive>=1 forever).
_hash=$(star_live_work_hash "$LIVE")
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

# Safety net (T2.4): re-arm ONLY if the pass-start reschedule actually FAILED (rc!=0) with no successor id
# -- never on a successful-but-unparsed submit (that would double-arm two chains). If it fails twice, write
# an ORPHAN marker so the dead chain is detectable instead of silently stopping with work pending.
if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
  reschedule
  if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
    say "ORPHAN: could not queue a successor twice (bsub failing at the PEND cap?) -- chain may stop"
    { echo "STAR watchdog could not reschedule at $(ts): bsub failed twice (likely the LSF pending-job cap)."
      echo "Re-arm manually:  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh"
    } > "$PIPELINE_ROOT/PIPELINE_ORPHANED.txt"
  fi
fi
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
