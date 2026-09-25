#!/usr/bin/env bash
# watchdog.sh — the self-driving controller. Each pass it:
#   1. resubmits orphaned/failed conversions (.sra on disk, no .fastq.gz, no live job)
#   2. re-fetches any still-missing accessions (idempotent fetch_missing)
#   3. accounts progress; if everything is converted -> writes PIPELINE_COMPLETE.txt
#      and STOPS. If it has made no progress for several idle passes (accessions
#      that SRA can't deliver) -> writes PIPELINE_STALLED.txt and STOPS.
#   4. otherwise re-submits ITSELF to run again in WATCHDOG_INTERVAL_MIN minutes.
# Start it once (run_pipeline.sh does). After that it needs no human attention.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
if [ -f "$HERE/lib_notify.sh" ]; then source "$HERE/lib_notify.sh"; else
  log_event(){ :; }; notify_error(){ :; }; notify_update(){ :; }; fi
set -u
cd "$STUDIES_DIR" || { echo "[watchdog] FATAL: cannot cd to STUDIES_DIR=$STUDIES_DIR (NFS down / dir gone) -- exiting" >&2; exit 1; }
LOG="$PIPELINE_ROOT/watchdog.log"
STATE="$PIPELINE_ROOT/.watchdog.state"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] $*" >> "$LOG"; }
shopt -s nullglob

# HEAL-ONLY mode (launcher temp-watchdog): while launch_all.sh is still SUBMITTING studies the real watchdog
# isn't armed yet — a window that can last >1h under the pending-job threshold. The launcher calls this script
# with SRA_WATCHDOG_HEAL_ONLY=1 to do JUST the safe healing (resubmit conversions whose .sra is stranded) and
# SKIP everything stateful: no successor reschedule, no refetch (run_all is still submitting the prefetches),
# no completion/stall decision, no finalize, no cleanup. Closes the no-watchdog gap during a long submit.
HEAL_ONLY="${SRA_WATCHDOG_HEAL_ONLY:-0}"

# The NEXT pass is queued at the START of each pass (reschedule-first), so a mid-pass walltime kill
# (e.g. bsub blocked on the LSF pending-job threshold) can NEVER break the self-driving chain. We keep
# the successor's job id so finalize() can cancel it once the pipeline is actually done.
WATCHDOG_NEXT_JID=""
RESCHED_RC=0
reschedule() {
  local when out
  when=$(date -d "+$WATCHDOG_INTERVAL_MIN min" '+%Y:%m:%d:%H:%M' 2>/dev/null) ||
  when=$(date -v+"${WATCHDOG_INTERVAL_MIN}"M '+%Y:%m:%d:%H:%M' 2>/dev/null)   # BSD fallback
  sra_qopt
  # DEAD-MAN'S-SWITCH WALLTIME (structural invariant): the watchdog walltime is DERIVED as
  # WATCHDOG_INTERVAL_MIN-5, so it is ALWAYS < the reschedule interval. A pass that hangs is killed
  # ~5 min BEFORE its successor starts -> the flock frees and the self-driving chain can never overlap
  # (a successor that collided would fail the flock and exit WITHOUT rescheduling = a broken chain).
  # To give passes MORE wall-time on a big run, RAISE WATCHDOG_INTERVAL_MIN in config.sh; the walltime
  # follows automatically. NEVER hardcode a walltime >= the interval -- that re-breaks the switch.
  # capture bsub's OWN rc so the safety-net re-arms only on a genuine submit failure (never double-arm)
  out=$(timeout "${SRA_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M 1000 -W "$(( ${WATCHDOG_INTERVAL_MIN:-30} - 5 ))" -b "$when" -J "${JOB_TAG}_watchdog" \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" 2>&1)
  RESCHED_RC=$?   # timeout -> 124 -> the bottom safety-net re-arms; never blocks the pass
  WATCHDOG_NEXT_JID=$(printf '%s' "$out" | sra_jobid)
  say "next pass scheduled for $when (job ${WATCHDOG_NEXT_JID:-?}, rc=$RESCHED_RC)"
}

# Delete transient clutter after a SUCCESSFUL run. KEEPS .fastq.gz, SraAccList.txt,
# the PIPELINE_COMPLETE.txt report, all scripts, and watchdog.log. Uses find so an
# empty match is harmless (never touches outputs/inputs/scripts).
cleanup_run() {
  [ "${CLEANUP_ON_COMPLETE:-yes}" = "yes" ] || return 0
  [ -f "$PIPELINE_ROOT/CLEANUP_COMPLETE.txt" ] && return 0   # idempotent: already cleaned -> safe to re-run/self-heal
  local S
  for S in "$STUDIES_DIR"/*/; do
    [ -d "$S" ] || continue
    find "$S" -maxdepth 1 -type f \( -name '*.err' -o -name '*.out' -o -name '*.lsf' \
         -o -name '*.sra.vdbcache' -o -name 'SraAccList_missing.txt' \) -delete 2>/dev/null
    find "$S" -mindepth 1 -maxdepth 1 -type d -empty -delete 2>/dev/null   # empty acc subdirs
  done
  rm -f "$PIPELINE_ROOT/watchdog.out" "$PIPELINE_ROOT/watchdog.err" \
        "$PIPELINE_ROOT/.watchdog.state" "$PIPELINE_ROOT/.watchdog.state.stall" \
        "$PIPELINE_ROOT/job_map.tsv" 2>/dev/null
  say "cleanup: removed transient logs/.lsf/temp files (CLEANUP_ON_COMPLETE=yes)"

  # Optionally remove the pipeline's OWN scripts too, leaving a data-only folder.
  # Guard: only if the scripts live inside PIPELINE_ROOT (never a shared tools dir).
  # Safe to delete the running watchdog.sh — bash keeps executing the open file.
  if [ "${CLEANUP_SCRIPTS_ON_COMPLETE:-yes}" = "yes" ] && [ -n "${SCRIPTS_DIR:-}" ]; then
    case "$SCRIPTS_DIR" in
      "$PIPELINE_ROOT"|"$PIPELINE_ROOT"/*)
        rm -f "$SCRIPTS_DIR"/config.sh "$SCRIPTS_DIR"/lib.sh "$SCRIPTS_DIR"/run_pipeline.sh \
              "$SCRIPTS_DIR"/setup.sh "$SCRIPTS_DIR"/run_all.sh "$SCRIPTS_DIR"/prefetch_job.sh \
              "$SCRIPTS_DIR"/fasterqdump_job.sh "$SCRIPTS_DIR"/convert_study.sh \
              "$SCRIPTS_DIR"/fetch_missing.sh "$SCRIPTS_DIR"/status.sh "$SCRIPTS_DIR"/DOWNLOAD_PIPELINE_GUIDE.md \
              "$SCRIPTS_DIR"/watchdog.sh 2>/dev/null
        say "cleanup: removed pipeline scripts from $SCRIPTS_DIR (re-copy the template to re-run)" ;;
      *)
        say "cleanup: kept scripts ($SCRIPTS_DIR is outside PIPELINE_ROOT - shared install)" ;;
    esac
  fi
  : > "$PIPELINE_ROOT/CLEANUP_COMPLETE.txt" 2>/dev/null   # mark done so a later re-arm does not re-clean
}

finalize() {  # $1 = COMPLETE | STALLED
  local status="$1"
  # EXACTLY-ONCE (T2.3): atomic mkdir claim (NFS-safe) -- only the first racer runs cleanup + the STAR kick.
  sra_finalize_once || { say "finalize already claimed by a concurrent pass -> skip"; return 0; }
  rm -f "$STATE.firstpass" "$STATE.passes" "$STATE.lastpass" 2>/dev/null   # a later re-arm starts a fresh backstop window
  # Done -> cancel ALL queued/duplicate watchdog successors except THIS job (a double-armed/nudged one
  # would re-spawn the chain). Best-effort.
  local _self _wj
  _self="${LSB_JOBID:-}"
  for _wj in $(timeout 60 bjobs -noheader -o jobid -J "${JOB_TAG}_watchdog" 2>/dev/null); do
    [ "$_wj" = "$_self" ] && continue
    bkill "$_wj" >/dev/null 2>&1
  done
  local rep="$PIPELINE_ROOT/PIPELINE_${status}.txt"
  {
    echo "Pipeline $status at $(ts)"
    echo "Converted: $total_done / $total_exp runs"
    echo "Dataset size: $(du -sh "$STUDIES_DIR" 2>/dev/null | cut -f1)"
    echo
    echo "Per-study (converted/expected):"
    for S in "$STUDIES_DIR"/*/; do
      [ -f "$S/SraAccList.txt" ] || continue
      local a g; a=$(sra_count_nonblank "$S/SraAccList.txt"); g=$(sra_done_count "$S")
      local mark=""; [ "$g" -lt "$a" ] && mark="   <-- INCOMPLETE"
      printf "  %-22s %s/%s%s\n" "$(basename "$S")" "$g" "$a" "$mark"
    done
    if [ "$status" = "STALLED" ]; then
      echo; echo "Accessions with NO .fastq.gz after repeated re-fetch (likely withdrawn/"
      echo "restricted on SRA — verify manually):"
      for S in "$STUDIES_DIR"/*/; do
        [ -f "$S/SraAccList.txt" ] || continue
        while read -r acc; do acc=$(echo "$acc"|tr -d '\r'); [ -z "$acc" ] && continue
          sra_delivered "${S%/}" "$acc" || echo "  $(basename "$S")  $acc"
        done < "$S/SraAccList.txt"
      done
    fi
  } > "$rep"
  # SpliceScout STAR auto-chain: if a STAR launcher is bundled here, kick it NOW so STAR starts the
  # moment the download finishes (COMPLETE or STALLED) instead of waiting for the launcher's own poll
  # cycle. Harmless no-op for plain download runs (no star/ dir). The launcher keeps its self-poll too.
  if [ -f "$PIPELINE_ROOT/star/star_launch.sh" ]; then
    sra_qopt
    bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J "${JOB_TAG}_star_launch" \
         -o "$PIPELINE_ROOT/star/launch.out" -e "$PIPELINE_ROOT/star/launch.err" \
         ${QOPT[@]+"${QOPT[@]}"} "$PIPELINE_ROOT/star/star_launch.sh" >/dev/null 2>&1
    say "kicked STAR launcher -> $PIPELINE_ROOT/star/star_launch.sh"
  fi
  [ "$status" = "COMPLETE" ] && cleanup_run   # tidy up only on success, never on STALLED
  if [ "$status" = "STALLED" ]; then
    notify_error "Download stage STALLED" "$(head -20 "$rep" 2>/dev/null)" "download-stalled"
    notify_diagnose "$JOB_TAG" "$PIPELINE_ROOT" "$SCRIPTS_DIR" "$PIPELINE_ROOT"
  else notify_update "Download stage COMPLETE" "$(head -10 "$rep" 2>/dev/null)"; fi
  say "FINALIZED ($status) -> $rep  (watchdog stopping)"
}

say "=== watchdog pass start ==="
# If a prior pass already finalized, this (already-queued) successor just stops — no work, no reschedule.
if [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ]; then
  [ "$HEAL_ONLY" = "1" ] || cleanup_run   # SELF-HEAL on a real pass; heal-only must NOT delete scripts mid-submission
  say "already finalized (COMPLETE) -> stop"; exit 0
fi
if [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt" ]; then say "already finalized (STALLED) -> stop"; exit 0; fi
# Reclaim a STALE finalize lock (no marker exists here => a prior finalize died before writing one).
[ -d "$PIPELINE_ROOT/.finalized.lock" ] && rmdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null
# SINGLE-FLIGHT (T2.3): one pass at a time (a nudged + a timed successor would double-submit + double-kick).
exec 8>"$PIPELINE_ROOT/.watchdog.run.lock" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
  flock -n 8 2>/dev/null || { say "another watchdog pass holds the run lock -> exit"; exit 0; }
fi

[ "$HEAL_ONLY" = "1" ] || reschedule   # queue the NEXT pass FIRST (survives a mid-pass walltime kill); heal-only: launcher drives cadence

# ABSOLUTE BACKSTOP (T2.2), bjobs-INDEPENDENT: cap by pass-count AND wall-clock so a stuck job or a
# persistently-broken bjobs can never loop/skip forever with no human signal. (Skipped in heal-only: it must
# never finalize, and its passes are launcher-driven, not the self-driving chain the cap is meant to bound.)
if [ "$HEAL_ONLY" != "1" ]; then
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
  total_done=0; total_exp=0
  for S in */; do
    [ -f "$S/SraAccList.txt" ] || continue
    total_exp=$((total_exp + $(sra_count_nonblank "$S/SraAccList.txt")))
    total_done=$((total_done + $(sra_done_count "$STUDIES_DIR/$(basename "$S")")))
  done
  say "BACKSTOP: passes=$_passes wall=$(( _now - _first ))s exceeded the cap -> STALLED (inspect stuck jobs)"
  finalize "STALLED"; exit 0
fi
fi

# SNAPSHOT WITH RC (T2.1): bjobs FAILED (rc!=0) or EMPTY -> unreliable -> skip the WHOLE pass (the
# queued successor retries). Acting on a bad snapshot resubmits running jobs and/or falsely finalizes.
LIVE="$(sra_snapshot)"; SRA_SNAP_RC=$?       # capture bjobs rc in the PARENT (command-subst is a subshell)
if [ "$SRA_SNAP_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$SRA_SNAP_RC) -- skipping resubmit + completion this pass"
  exit 0
fi

# SECOND INDEPENDENT SNAPSHOT (T2.1 efficient re-verify): the per-accession liveness re-check in the loop is
# an in-memory match against TWO full snapshots, NOT a `bjobs -J <name>` per stranded .sra. WHY: `bjobs -J`
# re-scans the WHOLE job table (no name index) once per accession -> ~15s each under a big pending backlog ->
# a ~12-min resubmit loop. One snapshot already lists EVERY live job name in ONE scan; a second independent
# snapshot gives the same fail-closed-vs-a-partial-snapshot guard (live in EITHER -> skip) for ONE more scan,
# not N. If the 2nd query fails, LIVE_B="" -> fall back to the already-validated LIVE (the pass is gated on it).
LIVE_B="$(sra_snapshot)"; SRA_SNAPB_RC=$?
[ "$SRA_SNAPB_RC" -ne 0 ] && LIVE_B=""

# 1) resubmit orphaned / failed conversions
resub=0; blocked=0
for S in */; do
  sdir="$STUDIES_DIR/$(basename "$S")"
  # If the study's BULK converter (cs) is still queued/running it WILL convert these .sra -> skip per-accession
  # resubmission so we never double-convert. Essential while the launcher is still submitting (cs jobs sit
  # PENDING behind the flood for a long time); harmless on a normal pass (by then cs has run, so it's not live).
  sra_has_live "${JOB_TAG}_cs_$(basename "$S")" "$LIVE" && continue
  # A download still sitting in its per-accession subdir (<acc>/<acc>.sra) is a DOWNLOADED run whose converter
  # never flattened it (convert_study bails out early on a full queue). Flatten it here so the loop below
  # submits its conversion. Before, only flat *.sra were seen: fetch_missing counted these as FAILED downloads,
  # re-prefetched them (prefetch finds them valid and exits 0) and dropped them after MAX_FAILS -- K562
  # (2026-07) dropped 4,081 fully downloaded runs this way.
  for _n in "$sdir"/*/*.sra "$sdir"/*/*.sralite "$sdir"/*/*.sra.vdbcache; do
    [ -e "$_n" ] || continue
    _a="$(basename "$(dirname "$_n")")"
    case "$_n" in
      *.sralite) _t="$sdir/$_a.sra" ;;
      *)         _t="$sdir/$(basename "$_n")" ;;
    esac
    # a flat copy of the same run already exists (both are complete prefetch outputs): drop the nested duplicate,
    # else it would sit in <acc>/ forever and hold the "zero .sra left" completion gate open
    if [ -e "$_t" ]; then rm -f "$_n"; else mv -n "$_n" "$_t" 2>/dev/null; fi
    rmdir "$sdir/$_a" 2>/dev/null
  done
  for sra in "$sdir"/*.sra; do
    [ -e "$sra" ] || continue
    acc=$(basename "$sra" .sra)
    sra_is_dropped "$acc" && { rm -f "$sra" "$sdir/${acc}.sra.vdbcache"; continue; }   # already gave up on it
    # a LIVE converter owns this accession -- never touch its source (checked BEFORE the converted test: a job
    # that is mid-publish must not have its .sra deleted out from under it)
    sra_has_live "${JOB_TAG}_fqd_${acc}" "$LIVE" && continue
    [ -n "$LIVE_B" ] && sra_has_live "${JOB_TAG}_fqd_${acc}" "$LIVE_B" && continue   # 2nd-snapshot re-verify (T2.1): in-memory, NOT per-acc bjobs
    # STAR already aligned this run (its FASTQ was deleted after the BAM, <acc>.aligned left) -> the .sra is redundant
    [ -e "$sdir/$acc.aligned" ] && { rm -f "$sra" "$sdir/${acc}.sra.vdbcache"; continue; }
    # already converted? DROP the now-redundant source .sra. (The per-acc converter deletes it on
    # success, but the bulk convert_study path can leave it behind -> stranded .sra keep nsra>0 forever
    # -> the "zero .sra left" completion gate never passes -> a FALSE STALL despite all data present.)
    # "Converted" = a final-named .fastq.gz exists AND no hidden .part temp is left over: fasterqdump_job.sh
    # publishes via temp+rename, so a leftover temp means an INTERRUPTED publish (e.g. _1 landed, _2 did not)
    # -> keep the .sra and let the conversion be resubmitted instead of deleting the only copy of _2.
    if { compgen -G "$sdir/$acc.fastq.gz" >/dev/null 2>&1 || compgen -G "$sdir/${acc}_[0-9].fastq.gz" >/dev/null 2>&1; } \
       && ! compgen -G "$sdir/.${acc}.fastq.gz.part.*" >/dev/null 2>&1 \
       && ! compgen -G "$sdir/.${acc}_[0-9].fastq.gz.part.*" >/dev/null 2>&1; then
      rm -f "$sra" "$sdir/${acc}.sra.vdbcache"; continue
    fi
    # a stranded .sra with no .fastq.gz and no live converter = the last conversion FAILED (or never ran) ->
    # DROP after MAX_FAILS real attempts so one un-convertible run can't keep the study from ever completing.
    # An attempt counts only once bsub has TAKEN the job: counting before the submit let a full queue (bsub
    # blocked -> 124) burn one attempt per pass on the same accession and drop it without a single try.
    if [ "$(sra_attempts "$acc")" -ge "${MAX_FAILS:-3}" ]; then
      sra_drop_acc "$acc" "$sdir" conversion
      say "DROPPED $acc after $(sra_attempts "$acc") failed conversions -> logged to dropped_accessions.txt"
      continue
    fi
    # FAIL-FAST: the helper returns 124 if it can't queue (full) -> STOP resubmitting THIS pass. Trying the
    # other hundreds of stranded accessions would just hang the pass on each blocked bsub (the 8h20m hang).
    # The reschedule already queued the next pass, which resumes from here once the queue drains.
    sra_submit_conversion "$acc" "$sdir" >/dev/null; _src=$?
    if [ "$_src" -eq 124 ]; then
      say "resubmit blocked (queue full) after $resub -> stopping resubmit this pass; next pass resumes"
      blocked=1
      break 2
    fi
    [ "$_src" -eq 0 ] && { sra_bump_attempt "$acc" >/dev/null; resub=$((resub+1)); }
  done
done
[ "$resub" -gt 0 ] && say "resubmitted $resub orphaned/failed conversion(s)"

# HEAL-ONLY (launcher temp-watchdog): conversions resubmitted — STOP here. Do NOT refetch (run_all is still
# submitting the prefetches, so "missing" just means not-yet-downloaded, not lost), do not decide completion,
# do not finalize. The launcher arms the real watchdog once submission finishes.
if [ "$HEAL_ONLY" = "1" ]; then
  say "heal-only pass done (resubmitted ${resub:-0}; no refetch/decision/finalize)"; exit 0
fi

# 2) re-fetch still-missing accessions (idempotent)
miss_out=$(bash "$SCRIPTS_DIR/fetch_missing.sh" 2>/dev/null | tail -1)
say "fetch_missing -> ${miss_out:-none}"

# 3) progress accounting
total_done=0; total_exp=0
for S in */; do
  [ -f "$S/SraAccList.txt" ] || continue
  total_exp=$((total_exp + $(sra_count_nonblank "$S/SraAccList.txt")))
  total_done=$((total_done + $(sra_done_count "$STUDIES_DIR/$(basename "$S")")))
done
# count only WORK jobs (pf/cs/fqd/…), NOT the watchdog itself, or nlive never hits 0
LIVE2="$(sra_live_names)"; SRA_LIVE2_RC=$?   # capture bjobs rc in the PARENT (command-subst is a subshell)
# GUARD (T2.1): a FAILED/empty-with-error bjobs here would yield nlive=0 -> a FALSE STALL/COMPLETE below.
# Treat it as UNKNOWN and RE-POLL: skip the WHOLE decision this pass; the queued successor retries.
if [ "$SRA_LIVE2_RC" -ne 0 ] || [ -z "$(printf '%s' "$LIVE2" | tr -d '[:space:]')" ]; then
  say "WARNING: bjobs failed/empty (rc=$SRA_LIVE2_RC) on live-job recount -- skipping decision this pass"
  exit 0
fi
nlive=$(sra_count_work "$LIVE2")   # pure-bash count (grep -c empty on compute nodes)
dropped=$(sra_dropped_count)       # accessions abandoned after MAX_FAILS failed download/convert attempts
say "progress: $total_done/$total_exp converted ($dropped dropped after ${MAX_FAILS:-3} fails), $nlive live jobs"

# SANITY FLOOR (T2.1): live jobs collapsed >50% vs last pass with no progress -> suspect partial bjobs ->
# skip the completion/stall DECISION this pass (don't falsely finalize on a bad snapshot).
_prev_nlive=$(cat "$STATE.nlive" 2>/dev/null || echo -1); echo "$nlive" > "$STATE.nlive"
_prev_done=$(cat "$STATE.donen" 2>/dev/null || echo -1); echo "$total_done" > "$STATE.donen"
if [ "$_prev_nlive" -gt 1 ] && [ "$nlive" -lt "$(( _prev_nlive / 2 ))" ] && [ "$total_done" -le "$_prev_done" ]; then
  say "WARNING: live jobs collapsed $_prev_nlive->$nlive with no progress (suspect partial bjobs) -- skipping decision this pass"
  exit 0
fi

# 4) decide: complete / stalled / keep going
# Robust completion: require the count AND zero .sra left AND zero live jobs, so a
# transient over-count (e.g. an NFS listing glitch) can never finalize prematurely.
_sra=("$STUDIES_DIR"/*/*.sra "$STUDIES_DIR"/*/*/*.sra); nsra=${#_sra[@]}  # nullglob-safe
# done + dropped: a run abandoned after MAX_FAILS counts as "accounted for" so a permanently-undeliverable
# accession can't hold the gate open forever (its source .sra was removed at drop time, so nsra still 0).
if [ "$(( total_done + dropped ))" -ge "$total_exp" ] && [ "$nsra" -eq 0 ] && [ "$nlive" -eq 0 ]; then
  finalize "COMPLETE"; exit 0
fi

queued_new=0; printf '%s' "$miss_out" | grep -qE 'queued [1-9]' && queued_new=1
# a submit that BLOCKED on a full queue is pending work, not "no progress": the pending-job cap can be filled by the
# user's OTHER runs (nlive = 0 here), and two such passes used to finalize STALLED with runs still to deliver. A
# queue that never drains is still bounded by ABSOLUTE_MAX_PASSES / MAX_WALL_HOURS.
printf '%s' "$miss_out" | grep -q 'submit blocked' && blocked=1
[ "${blocked:-0}" = "1" ] && say "a submit blocked on the full queue this pass -> not counted toward a stall"
if [ "$nlive" -eq 0 ] && [ "$resub" -eq 0 ] && [ "$queued_new" -eq 0 ] && [ "${blocked:-0}" = "0" ]; then
  prev=$(cat "$STATE" 2>/dev/null || echo -1)
  stall=0; [ "$total_done" = "$prev" ] && stall=$(( $(cat "$STATE.stall" 2>/dev/null || echo 0) + 1 ))
  echo "$total_done" > "$STATE"; echo "$stall" > "$STATE.stall"
  if [ "$stall" -ge 2 ]; then finalize "STALLED"; exit 0; fi
else
  echo "$total_done" > "$STATE"; echo 0 > "$STATE.stall"
fi
# Safety net (T2.4): re-arm ONLY if the pass-start reschedule actually FAILED (rc!=0) with no successor id
# -- never on a successful-but-unparsed submit (that would double-arm two chains). On a second failure,
# write an ORPHAN marker so the dead chain is detectable instead of silently stopping with work pending.
if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
  say "WARNING: pass-start reschedule FAILED (rc=$RESCHED_RC, no successor id) -- retrying self-schedule now"
  reschedule
  if [ "${RESCHED_RC:-0}" -ne 0 ] && [ -z "${WATCHDOG_NEXT_JID:-}" ]; then
    say "ORPHAN: could not queue a successor twice (bsub failing at the PEND cap?) -- chain may stop"
    { echo "Download watchdog could not reschedule at $(ts): bsub failed twice (likely the LSF pending-job cap)."
      echo "Re-arm manually:  bsub -L /bin/bash -n 1 -M 1000 -W $(( ${WATCHDOG_INTERVAL_MIN:-30} - 5 )) -J ${JOB_TAG}_watchdog $SCRIPTS_DIR/watchdog.sh"
    } > "$PIPELINE_ROOT/PIPELINE_ORPHANED.txt"
  fi
fi
say "pass end -> next pass queued (job ${WATCHDOG_NEXT_JID:-?})"
