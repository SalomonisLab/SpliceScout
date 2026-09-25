#!/usr/bin/env bash
# lib_star.sh -- shared LSF submit + accounting helpers for the STAR pipeline.
# Sourced AFTER config.sh by submit_all.sh, watchdog.sh, status.sh, setup.sh.

# Fail fast if not on an LSF submit host.
star_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found -- run this on the LSF SUBMIT host (e.g. bmiclusterp-head)." >&2
  exit 1
}

# QOPT=(-q QUEUE) if a queue is configured, else empty array.
star_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Parse "Job <12345> is submitted ..." -> 12345
star_jobid() { sed -n 's/.*Job <\([0-9]\{1,\}\)>.*/\1/p'; }

# Snapshot of all live (PEND+RUN) LSF job names, one per line. Capped with `timeout 60` so a hung
# bjobs (overloaded LSF) can't wedge a whole watchdog pass; the caller's empty/rc guard then skips.
star_live_names() { timeout 60 bjobs -noheader -o job_name 2>/dev/null; }

# Is job-name $1 present (exact match) in the newline-separated snapshot $2?
star_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# An LSF-safe, length-capped job name for a (possibly long/odd) sample label.
# Deterministic across passes so skip-if-live keeps matching the same job.
star_jobname() {
  local s="$1"
  s="${s//[^A-Za-z0-9_.-]/_}"               # only LSF-safe characters
  if [ "${#s}" -gt 40 ]; then               # cap length: readable head + stable hash tail
    # checksum AND byte-count (was checksum only) so two different long labels must collide on BOTH 32-bit
    # fields before they alias to one job name -> collision becomes vanishingly unlikely.
    local h; h="$(printf '%s' "$1" | cksum | awk '{print $1"x"$2}')"
    s="${s:0:31}_${h}"
  fi
  printf '%s_star_%s' "$JOB_TAG" "$s"
}

# THE single "is this sample DONE" predicate -- used by BOTH the job body and the watchdog, so they can
# never disagree: BAM exists AND samtools quickcheck passes. With STRICT_BAM_CHECK=1 (new runs; OFF by
# default so live mid-run deployments don't re-evaluate already-done BAMs) it ALSO requires the STAR QC
# log AND >0 mapped reads -- so a truncated-but-EOF-valid or empty-but-valid BAM can't pass the gate and
# then get its source FASTQ irreversibly deleted (quickcheck only validates the header + BGZF EOF block).
star_bam_ok() {
  local b="$BAM_OUT/$1.bam"
  [ -s "$b" ] && samtools quickcheck "$b" 2>/dev/null || return 1
  if [ "${STRICT_BAM_CHECK:-0}" = "1" ]; then
    [ -f "$LOG_DIR/$1.Log.final.out" ] || return 1
    # `samtools idxstats` needs an index: with NO .bai it prints nothing and exits 0, so a missing index
    # would zero the mapped-read sum and FALSELY fail an otherwise-good BAM. quickcheck already passed
    # above, so create the index if it's missing before counting (idempotent; harmless if it exists).
    [ -s "$b.bai" ] || [ -s "${b%.bam}.bai" ] || samtools index "$b" 2>/dev/null || return 1
    samtools idxstats "$b" 2>/dev/null | awk '{m+=$3} END{exit !(m>0)}' || return 1
  fi
  return 0
}

# Submit ONE sample. Args: label fastq1 fastq2(:-NA). Echoes the LSF job id on success.
# VERIFY bsub SUCCESS on (re)arm: capture bsub's OWN exit status (a pipe to star_jobid would hide it
# behind sed's rc) -- a failed bsub (PEND cap, transient LSF) echoes NOTHING and returns non-zero, so a
# caller's `&& resub=...` won't miscount a non-submit as a resubmit and the sample is retried next pass.
star_submit_sample() {
  local label="$1" f1="$2" f2="${3:-NA}" jn out rc
  jn="$(star_jobname "$label")"
  star_qopt
  # WALLTIME / MEM SELF-HEAL: if THIS sample's previous attempt hit an LSF limit, escalate its -W/-M for the
  # retry -- else the same limit kills it again and it's DROPPED after STAR_MAX_FAILS. Per-sample + LOCAL, so
  # one big sample never inflates the others. WALL -> queue max on a walltime kill; MEM (+rusage) +50% per
  # mem kill. Reads the LAST termination from the sample's accumulated -o log (awk -> compute-node safe).
  local _wall="$WALL" _mem="$MEM_MB" _rus="${MEM_RUSAGE:-$MEM_MB}" _jo="$LOG_DIR/star_${label}.out" _t _nm
  if [ -f "$_jo" ]; then
    _t=$(awk 'match($0,/TERM_(RUNLIMIT|MEMLIMIT)/){m=substr($0,RSTART,RLENGTH)} END{print m}' "$_jo" 2>/dev/null)
    _nm=$(awk '/TERM_MEMLIMIT/{n++} END{print n+0}' "$_jo" 2>/dev/null); _nm=${_nm:-0}
    [ "$_t" = "TERM_RUNLIMIT" ] && _wall="1108:00"
    [ "${_nm:-0}" -gt 0 ] && { _mem=$(( _mem + _mem * _nm / 2 )); _rus=$(( _rus + _rus * _nm / 2 )); }
  fi
  # If a build-once index job is pending (BUILD_JID, from RESOLVED_INDEX.env), wait for it.
  local DEPW=(); [ -n "${BUILD_JID:-}" ] && DEPW=(-w "done(${BUILD_JID})")
  out=$(bsub -L /bin/bash ${QOPT[@]+"${QOPT[@]}"} ${DEPW[@]+"${DEPW[@]}"} \
       -J "$jn" -n "$THREADS" -W "$_wall" -M "$_mem" \
       -R "rusage[mem=${_rus}] span[hosts=1]" \
       -o "$LOG_DIR/star_${label}.out" -e "$LOG_DIR/star_${label}.err" \
       "$SCRIPTS_DIR/run_star_job.sh" "$label" "$f1" "$f2" 2>&1)
  rc=$?
  [ "$rc" -eq 0 ] || return 1                  # bsub itself failed -> NOT submitted; let the caller retry
  printf '%s' "$out" | star_jobid
}

# Non-empty rows in the sample list = the fixed-at-launch denominator. Counted with a PURE-BASH loop, NOT
# `grep -c`: on this cluster's COMPUTE NODES `grep -c` returns an EMPTY count (matching works, but the -c
# count output is blank) -> exp_n="" -> the watchdog's integer completion test errors and the stage never
# finalizes (hit LIVE 2026-06-07 on the BED stage; same code path). A while-read loop is immune.
star_expected_count() {
  local n=0 _l
  [ -f "$SAMPLE_LIST" ] || { echo 0; return; }
  while IFS= read -r _l || [ -n "$_l" ]; do
    case "$_l" in (*[![:space:]]*) n=$((n+1)) ;; esac
  done < "$SAMPLE_LIST"
  echo "$n"
}

# Rows whose label has a valid BAM (anchored to the list, immune to stray *.bam).
star_done_count() {
  local n=0 label rest
  [ -f "$SAMPLE_LIST" ] || { echo 0; return; }
  while IFS=$'\t' read -r label rest; do
    [ -n "$label" ] || continue
    star_bam_ok "$label" && n=$((n+1))
  done < "$SAMPLE_LIST"
  echo "$n"
}

# Count ${JOB_TAG}_star_* WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH loop -- NOT
# `grep -c` (empty count on compute nodes; see star_expected_count).
star_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in ${JOB_TAG}_star_*) n=$((n+1)) ;; esac
  done <<< "${1:-}"
  echo "$n"
}

# Count of live WORK jobs (the star_ prefix), never the watchdog -> can reach 0.
star_live_work_count() { star_count_work "$(star_live_names)"; }

# ---- reliability helpers (reinforcement pass) -------------------------------------------------
# bjobs snapshot of live job NAMES. CAPTURE ITS exit status AT THE CALL SITE:
#     LIVE="$(star_snapshot)"; STAR_SNAP_RC=$?
# A non-zero rc means bjobs itself FAILED (overloaded/transient) -- the caller must NOT act on the result
# (an empty/partial list would resubmit running jobs or falsely finalize). The rc CANNOT be returned via a
# global here: command substitution runs in a SUBSHELL, so any var set inside is lost -- read $? instead.
# `timeout 60`: a hung bjobs under load returns non-zero here, which the caller treats as "unreliable,
# skip the pass" -- far safer than blocking the watchdog walltime out on a single bjobs.
star_snapshot() { timeout 60 bjobs -noheader -o job_name 2>/dev/null; }

# Targeted, fail-CLOSED liveness re-check for ONE job name, used right BEFORE an expensive resubmit:
# a SECOND independent bjobs query that a partial/truncated bulk snapshot can't fool. Live -> return 0
# (skip the resubmit). (A transient failure here just defers to the pass-level snapshot guard + the
# miss being retried next pass -- far cheaper than a duplicate alignment colliding on the same BAM.)
# Guard: `timeout 60` caps a hung query, an EMPTY result is NOT treated as done (an empty bjobs under
# load must not green-light a duplicate submit), and an UNKNOWN/transient stat is re-polled ONCE before
# we conclude the job is gone -- so a flaky single query can't drop a still-live job into a resubmit.
star_job_is_live() {
  local out
  out="$(timeout 60 bjobs -noheader -o stat -J "$1" 2>/dev/null)"
  printf '%s\n' "$out" | grep -qE 'RUN|PEND' && return 0
  # UNKNOWN/UNKWN (LSF lost contact with the exec host) or an empty result: re-poll once before deciding.
  if printf '%s\n' "$out" | grep -qiE 'UNKWN|UNKNOWN' || [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
    out="$(timeout 60 bjobs -noheader -o stat -J "$1" 2>/dev/null)"
    printf '%s\n' "$out" | grep -qE 'RUN|PEND' && return 0
  fi
  return 1
}

# Exactly-once finalization claim via an atomic mkdir (works over NFS where flock can silently
# degrade). Only the FIRST caller in a finalize race gets 0; everyone else gets non-zero and bails.
star_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# A stable hash of the live WORK-job name SET (sorted) for no-churn stall detection: if both done_n and
# this hash are unchanged across passes, no forward progress is being made (e.g. a permanently-PENDING
# job), so the watchdog can STALL instead of looping forever. Reuses a provided snapshot ($1).
star_live_work_hash() { printf '%s\n' "$1" | grep -E "^${JOB_TAG}_star_" | sort | cksum | cut -d' ' -f1; }

# ---- per-sample failure tracking: drop a sample after STAR_MAX_FAILS failed alignments (user 2026-06-22) -
# Mirrors the BED/download drop-after-N so one bad sample can't STALL the stage: per-sample attempts in
# $PIPELINE_ROOT/.attempts/<label>.n; a <label>.dropped marker stops resubmits; logged to star_dropped.txt
# and COUNTED toward completion (done_n + dropped >= exp).
: "${STAR_MAX_FAILS:=3}"
STAR_ATTEMPTS_DIR="$PIPELINE_ROOT/.attempts"
STAR_DROPPED_LIST="$PIPELINE_ROOT/star_dropped.txt"
star_attempts()   { cat "$STAR_ATTEMPTS_DIR/$1.n" 2>/dev/null || echo 0; }
star_is_dropped() { [ -f "$STAR_ATTEMPTS_DIR/$1.dropped" ]; }
star_bump_attempt() {
  mkdir -p "$STAR_ATTEMPTS_DIR" 2>/dev/null
  local n=$(( $(star_attempts "$1") + 1 )); echo "$n" > "$STAR_ATTEMPTS_DIR/$1.n"; echo "$n"
}
star_drop_sample() {
  [ -f "$STAR_ATTEMPTS_DIR/$1.dropped" ] && return 0
  mkdir -p "$STAR_ATTEMPTS_DIR" 2>/dev/null; : > "$STAR_ATTEMPTS_DIR/$1.dropped"
  printf '%s\t%s\tafter %s attempts\t%s\n' "$1" "${2:-alignment}" "$(star_attempts "$1")" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$STAR_DROPPED_LIST"
}
# Counts ONLY labels of the CURRENT sample list that have no valid BAM: the gate is done + dropped >= expected, so a
# marker left by an earlier run (a list rebuilt for a re-run) or of a sample that aligned after all would count twice
# and could finalize the stage with samples still unaligned (the 2026-09-22 re-run kit moved such markers by hand).
star_dropped_count() {
  local n=0 label rest
  [ -f "$SAMPLE_LIST" ] || { echo 0; return; }
  while IFS=$'\t' read -r label rest; do
    [ -n "$label" ] || continue
    [ -f "$STAR_ATTEMPTS_DIR/$label.dropped" ] || continue
    star_bam_ok "$label" && continue
    n=$((n+1))
  done < "$SAMPLE_LIST"
  echo "$n"
}

# Does at least one of a comma-list of FASTQ paths still exist on disk? Used by the watchdog to detect
# that a sample's source reads were already deleted (DELETE_FASTQ_AFTER_BAM) -- a resubmit would be doomed,
# so it reports UNRECOVERABLE instead of looping a job that can only fail.
star_fastq_exists() {
  local f arr
  IFS=',' read -ra arr <<< "$1"
  for f in ${arr[@]+"${arr[@]}"}; do
    [ "$f" = "NA" ] && continue
    [ -e "$f" ] && return 0
  done
  return 1
}

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass.
# Called by run_star_job.sh right after it publishes a BAM. If this job is the LAST live work job, it
# queues a watchdog pass GATED ON THIS JOB ENDING, so finalize lands within seconds of the final BAM
# instead of up to a full poll interval later. PURE ACCELERATOR -- the timed poll stays the fallback,
# so a last job that dies WITHOUT nudging (OOM / walltime kill / crash) is still finalized by the poll.
#   * The watchdog (${JOB_TAG}_watchdog) is NOT in star_live_work_count -- that counts only
#     ^${JOB_TAG}_star_ WORK jobs -- so it never inflates the total; the "<= 1" test refers to THIS
#     calling job, which is itself still RUN while it executes this, so "only work job left" == 1.
#   * -w "ended($LSB_JOBID)": the woken pass starts only AFTER this job leaves the queue, so it sees
#     nlive==0 and FINALIZES -- instead of racing us-still-RUN, seeing nlive==1, and merely
#     rescheduling (which would leave the timed poll as the only finalizer, defeating the nudge).
#     One job id -> same single-dep mechanism star_submit_sample already uses for the genome build.
#   * flock(-n): if several samples finish together, only ONE nudges; the rest skip.
#   * Spurious/early nudges are harmless: the already-finalized guard + reschedule-first absorb a
#     duplicate pass, and a not-yet-done pass just resubmits stragglers early (a bonus). Waking a
#     watchdog that is ALREADY active is safe too -- each pass reschedule-firsts exactly one
#     successor and finalize() bkills it, so extra instances no-op via the already-finalized guard.
star_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0                 # another finisher is already nudging -> skip
  local nlive; nlive="$(star_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  # EXACTLY 1 (this still-RUN job), NOT <=1: this job is live so a healthy count is >=1; nlive==0 means
  # bjobs FAILED / was overloaded -> do NOT nudge. (A spurious 0 under load is what let the BED nudge
  # spawn ~1000 watchdogs.) The timed poll still finalizes, so a skipped nudge only loses acceleration.
  [ "$nlive" -eq 1 ] || return 0
  star_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  timeout "${WATCHDOG_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' last live work job -> watchdog queued on ended(${LSB_JOBID:-?})" \
       >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
}
