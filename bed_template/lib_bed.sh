#!/usr/bin/env bash
# lib_bed.sh -- shared LSF submit + accounting helpers for the BAM->BED stage.
# Sourced AFTER config.sh by submit_all.sh, watchdog.sh, status.sh, setup.sh, run_bed_job.sh.

# Fail fast if not on an LSF submit host.
bed_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found -- run this on the LSF SUBMIT host (e.g. bmiclusterp-head)." >&2
  exit 1
}

# QOPT=(-q QUEUE) if a queue is configured, else empty array.
bed_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Parse "Job <12345> is submitted ..." -> 12345
bed_jobid() { sed -n 's/.*Job <\([0-9]\{1,\}\)>.*/\1/p'; }

# Snapshot of all live (PEND+RUN) LSF job names, one per line.
bed_live_names() { bjobs -noheader -o job_name 2>/dev/null; }

# Is job-name $1 present (exact match) in the newline-separated snapshot $2?
bed_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# An LSF-safe, length-capped job name for a (possibly long/odd) sample label.
# Deterministic across passes so skip-if-live keeps matching the same job. The "_bed_"
# infix is what lets bed_live_work_count tell WORK jobs apart from the watchdog.
bed_jobname() {
  local s="$1"
  s="${s//[^A-Za-z0-9_.-]/_}"               # only LSF-safe characters
  if [ "${#s}" -gt 40 ]; then               # cap length: readable head + stable hash tail
    # checksum AND byte-count (was checksum only) so two different long labels must collide on BOTH 32-bit
    # fields before they alias to one job name -> collision becomes vanishingly unlikely.
    local h; h="$(printf '%s' "$1" | cksum | awk '{print $1"x"$2}')"
    s="${s:0:31}_${h}"
  fi
  printf '%s_bed_%s' "$JOB_TAG" "$s"
}

# THE single "is this BAM converted" predicate -- used by the job body AND the watchdog,
# so they can never disagree. <sample>__junction.bed is required in EVERY mode; the second
# file depends on BED_MODE. NOTE the -e (EXISTS) check for __intronJunction.bed, NOT -s:
# BAMtoExonBED hard-gates intron-retention junctions, so a perfectly good sample can
# legitimately produce an EMPTY __intronJunction.bed -- requiring it non-empty would
# resubmit that sample forever -> STALL. __junction.bed/__exon.bed stay -s (non-empty).
# A BED file looks COMPLETE (not truncated): non-empty, ends with a newline (the final record wasn't cut
# off mid-line by an LSF kill / NFS stall), and its last line parses as a BED row (>=3 fields, numeric
# start/end). Rejects a truncated-but-nonempty .bed that a bare [ -s ] would wrongly accept as DONE.
bed_file_ok() {
  local f="$1"
  [ -s "$f" ] || return 1
  [ -z "$(tail -c1 "$f" 2>/dev/null)" ] || return 1     # last byte is a newline -> final record complete
  tail -n 1 "$f" 2>/dev/null | awk 'NF>=3 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ {ok=1} END{exit ok?0:1}'
}

# An __intronJunction.bed is OK if it is EITHER legitimately EMPTY (BAMtoExonBED hard-gates intron-retention
# junctions, so a perfectly good sample can produce 0 rows) OR a fully-intact BED12 -- EVERY data line is 12
# tab columns with integer chromStart/chromEnd (track/browser/# headers skipped); early-exits on the first
# bad line. A NON-EMPTY but corrupt/truncated/interleaved intron bed is REJECTED. WHY this exists: the bare
# -e check below let a corrupt-but-present intron bed pass as DONE, its BAM was deleted (DELETE_BAM_AFTER_BED),
# and the corrupt bed then DEADLOCKED AltAnalyze's PSI import -- RNASeq.py's bad-line handler is the undefined
# `print t; force_exception` -> NameError -> a multiprocessing worker dies -> the parent hangs for the entire
# walltime with no output (observed LIVE: 9 corrupt A549 intron beds froze PSI for 14h). A mid-file scan is
# required because the corruption was NOT at the tail, so the last-line bed_file_ok was blind to it.
bed_intron_ok() {
  local f="$1"
  [ -e "$f" ] || return 1
  [ -s "$f" ] || return 0                                  # legitimately empty -> OK
  [ -z "$(tail -c1 "$f" 2>/dev/null)" ] || return 1        # non-empty -> must end in a newline
  LC_ALL=C awk -F'\t' '/^(track|browser|#)/{next} (NF!=12 || $2!~/^[0-9]+$/ || $3!~/^[0-9]+$/){exit 1}' "$f"
}

# Completeness test for a content-bearing BED. RECONCILED: run_bed_job.sh validates its outputs with
# bed_file_ok before the atomic publish, so the watchdog's "is this BAM done" predicate MUST use the
# SAME check -- otherwise the watchdog could count a truncated-but-nonempty [ -s ] BED as DONE that
# run_bed_job would (correctly) reject, and the two disagree. bed_file_ok is now the DEFAULT. The
# lenient [ -s ] survives ONLY as an explicit opt-out (STRICT_BED_CHECK=0), for a live mid-run
# deployment that must not re-evaluate already-published BEDs.
bed_present() {
  if [ "${STRICT_BED_CHECK:-1}" = "0" ]; then [ -s "$1" ]; else bed_file_ok "$1"; fi
}

bed_done() {
  local stem="${BED_OUT_DIR:-$BAM_INPUT_DIR}/$1"
  bed_present "${stem}__junction.bed" || return 1
  case "${BED_MODE:-intron}" in
    exon) bed_present "${stem}__exon.bed" ;;
    # 'both' produces the exon bed BEST-EFFORT but NEVER blocks on it: PSI/AltAnalyze splicing uses
    # junction + intronJunction, not __exon.bed, and the exon pass OOM-truncates on very large BAMs --
    # so requiring a valid exon bed here would resubmit those samples forever -> STALL (LIVE A549 2026-06).
    both) bed_intron_ok "${stem}__intronJunction.bed" ;;
    *)    bed_intron_ok "${stem}__intronJunction.bed" ;;
  esac
}

# ---- per-sample failure tracking: drop a BAM after BED_MAX_FAILS failed conversions (user 2026-06-22) ---
# Mirrors the download stage's drop-after-N. A BAM whose BED conversion fails BED_MAX_FAILS times (e.g. a
# truncated/odd BAM the converter can't handle) is DROPPED so one bad sample can't STALL the whole stage:
# per-sample attempts live in $PIPELINE_ROOT/.attempts/<label>.n; a <label>.dropped marker stops further
# resubmits; the drop is logged to bed_dropped.txt and COUNTED toward completion (done_n + dropped >= exp).
: "${BED_MAX_FAILS:=3}"
BED_ATTEMPTS_DIR="$PIPELINE_ROOT/.attempts"
BED_DROPPED_LIST="$PIPELINE_ROOT/bed_dropped.txt"
bed_attempts()   { cat "$BED_ATTEMPTS_DIR/$1.n" 2>/dev/null || echo 0; }      # $1=label -> attempt count
bed_is_dropped() { [ -f "$BED_ATTEMPTS_DIR/$1.dropped" ]; }                   # $1=label
bed_bump_attempt() {                                                          # $1=label -> echoes NEW count
  mkdir -p "$BED_ATTEMPTS_DIR" 2>/dev/null
  local n=$(( $(bed_attempts "$1") + 1 )); echo "$n" > "$BED_ATTEMPTS_DIR/$1.n"; echo "$n"
}
bed_drop_sample() {                                                           # $1=label $2=reason
  [ -f "$BED_ATTEMPTS_DIR/$1.dropped" ] && return 0          # idempotent
  mkdir -p "$BED_ATTEMPTS_DIR" 2>/dev/null; : > "$BED_ATTEMPTS_DIR/$1.dropped"
  printf '%s\t%s\tafter %s attempts\t%s\n' "$1" "${2:-conversion}" "$(bed_attempts "$1")" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$BED_DROPPED_LIST"
}
# Count dropped samples with a PURE-BASH loop (NOT ls|wc / grep -c -> unreliable on compute nodes).
bed_dropped_count() { local n=0 f; for f in "$BED_ATTEMPTS_DIR"/*.dropped; do [ -e "$f" ] && n=$((n+1)); done; echo "$n"; }

# Submit ONE BAM. Arg: label (= BAM basename without .bam). Echoes the LSF job id.
bed_submit_sample() {
  local label="$1" jn
  jn="$(bed_jobname "$label")"
  bed_qopt
  # WALLTIME / MEM SELF-HEAL: if THIS sample's previous attempt hit an LSF limit, escalate its -W/-M for the
  # retry (else the same limit kills it again and it's dropped). Per-sample + LOCAL. WALL -> queue max on a
  # walltime kill; MEM +50% per mem kill. Reads the last termination from the sample's -o log (awk-safe).
  local _wall="$WALL" _mem="$MEM_MB" _jo="$LOG_DIR/bed_${label}.out" _t _nm
  if [ -f "$_jo" ]; then
    _t=$(awk 'match($0,/TERM_(RUNLIMIT|MEMLIMIT)/){m=substr($0,RSTART,RLENGTH)} END{print m}' "$_jo" 2>/dev/null)
    _nm=$(awk '/TERM_MEMLIMIT/{n++} END{print n+0}' "$_jo" 2>/dev/null); _nm=${_nm:-0}
    [ "$_t" = "TERM_RUNLIMIT" ] && _wall="1108:00"
    [ "${_nm:-0}" -gt 0 ] && _mem=$(( _mem + _mem * _nm / 2 ))
  fi
  bsub -L /bin/bash ${QOPT[@]+"${QOPT[@]}"} \
       -J "$jn" -n "$THREADS" -W "$_wall" -M "$_mem" \
       -R "span[hosts=1]" \
       -o "$LOG_DIR/bed_${label}.out" -e "$LOG_DIR/bed_${label}.err" \
       "$SCRIPTS_DIR/run_bed_job.sh" "$label" | bed_jobid
  # Stamp the submit time ONLY on a successful bsub (PIPESTATUS[0] = bsub's own rc, not bed_jobid's).
  # bed_job_ended() compares the job's -o log mtime to this stamp to tell "the last attempt ENDED" from
  # "still running" -- so the watchdog never counts a running job as a failed conversion (the false-drop ->
  # spurious-meltdown bug). A failed bsub leaves no stamp, so the sample is simply resubmitted next pass.
  if [ "${PIPESTATUS[0]:-1}" -eq 0 ]; then
    mkdir -p "$BED_ATTEMPTS_DIR" 2>/dev/null
    : > "$BED_ATTEMPTS_DIR/${label}.lastsub" 2>/dev/null
  fi
}

# Non-empty rows in the BAM list = the fixed-at-launch denominator. Counted with a PURE-BASH loop, NOT
# `grep -c`: on this cluster's COMPUTE NODES `grep -c` returns an EMPTY count (grep MATCHING works, but the
# -c count output comes back blank) -> exp_n="" -> the watchdog's integer completion test errored and the
# stage NEVER finalized despite all BEDs done (hit LIVE 2026-06-07 on the Blister/MDSL run). A while-read
# loop is immune (it's how bed_done_count already counts reliably).
bed_expected_count() {
  local n=0 _l
  [ -f "$BAM_LIST" ] || { echo 0; return; }
  while IFS= read -r _l || [ -n "$_l" ]; do
    case "$_l" in (*[![:space:]]*) n=$((n+1)) ;; esac
  done < "$BAM_LIST"
  echo "$n"
}

# Rows whose BAM has both valid BEDs (anchored to the list, immune to stray *.bed).
bed_done_count() {
  local n=0 label rest
  [ -f "$BAM_LIST" ] || { echo 0; return; }
  while IFS=$'\t' read -r label rest; do
    [ -n "$label" ] || continue
    bed_done "$label" && n=$((n+1))
  done < "$BAM_LIST"
  echo "$n"
}

# Count ${JOB_TAG}_bed_* WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH loop -- NOT
# `grep -c` (its count output is empty on this cluster's compute nodes; see bed_expected_count). The
# watchdog passes its already-captured snapshot so the count matches the same bjobs read it gated on.
bed_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in ${JOB_TAG}_bed_*) n=$((n+1)) ;; esac
  done <<< "${1:-}"
  echo "$n"
}

# Count of live WORK jobs (the _bed_ infix), never the watchdog -> can reach 0.
bed_live_work_count() { bed_count_work "$(bed_live_names)"; }

# ---- reliability helpers (reinforcement pass; mirror lib_star.sh) ------------------------------
# bjobs snapshot of live job NAMES. CAPTURE ITS rc AT THE CALL SITE: LIVE="$(bed_snapshot)"; BED_SNAP_RC=$?
# (command substitution is a SUBSHELL, so a global set inside is lost -- read $? in the parent). rc!=0 =>
# bjobs FAILED -> skip the pass (an empty/partial list would resubmit running jobs or falsely finalize).
bed_snapshot() { bjobs -noheader -o 'job_name:90' 2>/dev/null | awk '{print $1}'; }   # :90 = wide column so
# long ${JOB_TAG}_bed_<label> names are NOT truncated (the default width truncates them, which silently
# breaks bed_has_live's exact match); awk strips the column padding so the exact match still works.

# Targeted liveness re-check for ONE job name before an expensive resubmit -- a SECOND independent bjobs
# query a partial bulk snapshot can't fool. Live -> 0 (skip the resubmit).
bed_job_is_live() { bjobs -noheader -o stat -J "$1" 2>/dev/null | grep -qE 'RUN|PEND'; }

# Has this sample been submitted at least once? (a .lastsub stamp exists). Distinguishes a never-run sample
# (resubmit, no fail count) from one whose attempt must be checked with bed_job_ended before counting a fail.
bed_job_submitted() { [ -f "$BED_ATTEMPTS_DIR/$1.lastsub" ]; }

# Did this sample's LAST attempt actually TERMINATE? LSF writes a job's -o log only when the job ENDS, so an
# -o log NEWER than the last submit stamp means the latest attempt finished (success or failure); OLDER (or
# absent) means it's still running -- even if a flaky/overloaded bjobs momentarily reported it not-live. This
# is the authoritative "is it a real failure" gate that prevents false drops -> spurious meltdown STALLs.
bed_job_ended() {
  local jo="$LOG_DIR/bed_$1.out" sub="$BED_ATTEMPTS_DIR/$1.lastsub"
  [ -f "$jo" ] || return 1
  [ -f "$sub" ] || return 0          # log exists but no submit stamp -> treat as ended (legacy/first run)
  [ "$jo" -nt "$sub" ]               # -o written AFTER the last submit -> the latest attempt ended
}

# Exactly-once finalization claim via an atomic mkdir (NFS-safe where flock degrades). First caller -> 0.
bed_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# Stable hash of the live WORK-job name SET (sorted) for no-churn stall detection. Reuses snapshot ($1).
bed_live_work_hash() { printf '%s\n' "$1" | grep -E "^${JOB_TAG}_bed_" | sort | cksum | cut -d' ' -f1; }

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass.
# Called by run_bed_job.sh right after it publishes both BEDs. If this job is the LAST live work job,
# it queues a watchdog pass GATED ON THIS JOB ENDING, so finalize lands within seconds of the final
# BED instead of up to a full poll interval later. PURE ACCELERATOR -- the timed poll stays the
# fallback, so a last job that dies WITHOUT nudging is still finalized by the next poll.
#   * The watchdog (${JOB_TAG}_watchdog) is NOT in bed_live_work_count -- that counts only
#     ^${JOB_TAG}_bed_ WORK jobs -- so the "<= 1" test refers to THIS calling job (still RUN).
#   * -w "ended($LSB_JOBID)": the woken pass starts only AFTER this job leaves the queue, so it
#     sees nlive==0 and FINALIZES instead of racing us-still-RUN and merely rescheduling.
#   * flock(-n): if several jobs finish together, only ONE nudges; spurious/early nudges are absorbed
#     by the already-finalized guard + reschedule-first.
bed_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0
  local nlive; nlive="$(bed_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  # EXACTLY 1 (this still-RUN job), NOT <=1: this job is live so a healthy count is >=1; nlive==0 means
  # bjobs FAILED / was overloaded -> do NOT nudge. A spurious 0 under load is EXACTLY what spawned ~1000
  # a549_bed watchdogs. The timed poll still finalizes, so a skipped nudge only loses acceleration.
  [ "$nlive" -eq 1 ] || return 0
  # DOUBLE-FINALIZE GUARD: do NOT queue a new watchdog if one already exists (a timed-poll successor is
  # always armed reschedule-first, and another job may have just nudged). Two watchdogs that both see
  # nlive==0 could race the finalize claim/cleanup. A targeted bjobs check for ANY live ${JOB_TAG}_watchdog
  # (RUN or PEND) means our nudge only ADDS a pass when none is queued -- the timed poll stays the fallback.
  if bjobs -noheader -o stat -J "${JOB_TAG}_watchdog" 2>/dev/null | grep -qE 'RUN|PEND'; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' last live work job, but a ${JOB_TAG}_watchdog is already queued -> skip (no double-arm)" \
         >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
    return 0
  fi
  bed_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  # LOG the bsub rc (was logged 'queued' UNCONDITIONALLY, hiding a failed accelerator submit). The timed
  # poll is still the fallback, so a failed nudge only loses acceleration -- but now it leaves a trace.
  if [ "$?" -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' last live work job -> watchdog queued on ended(${LSB_JOBID:-?})" \
         >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' bsub FAILED to queue the watchdog accelerator (timed poll remains the fallback)" \
         >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
  fi
}
