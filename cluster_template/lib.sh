#!/usr/bin/env bash
# lib.sh — submission helpers shared by the driver scripts. Source AFTER config.sh.
# All jobs are submitted with "-L /bin/bash" so the compute-node login shell sets
# up environment modules; job names are namespaced with $JOB_TAG so resubmission
# is idempotent (a driver can check "is <name> already live?" before submitting).

# Fail early with a clear message if not on an LSF submit host (the #1 setup
# mistake: launching from a login node where bsub isn't available).
sra_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found on host '$(hostname)'." >&2
  echo "       Launch the pipeline from your LSF SUBMIT HOST, not the login node." >&2
  echo "       On this cluster that is the head node, e.g.:  ssh bmiclusterp-head" >&2
  exit 1
}

# Set QOPT=(-q QUEUE) if a queue is configured, else empty.
sra_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Read "Job <12345> is submitted ..." on stdin -> print 12345
sra_jobid() { sed -n 's/.*Job <\([0-9]*\)>.*/\1/p'; }

# Snapshot of my live (RUN+PEND) LSF job names, one per line. GUARDED by a timeout so a hung LSF returns
# promptly (non-zero); callers must not read an empty result as "no jobs" without checking rc.
sra_live_names() { timeout 60 bjobs -noheader -o job_name 2>/dev/null; }

# Is job name $1 present in the newline-separated snapshot $2 ?
sra_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# ---- reliability helpers (reinforcement pass; mirror lib_star.sh) ------------------------------
# bjobs snapshot of live job NAMES. CAPTURE ITS rc AT THE CALL SITE: LIVE="$(sra_snapshot)"; SRA_SNAP_RC=$?
# (command substitution is a SUBSHELL -- a global set inside is lost; read $? in the parent). rc!=0 => skip.
# GUARDED by a timeout so a hung LSF can't wedge the pass: a timed-out bjobs returns non-zero, which the
# call site treats as UNKNOWN (skip resubmit + completion, re-poll next pass) -- never as "no jobs".
sra_snapshot() { timeout 60 bjobs -noheader -o job_name 2>/dev/null; }

# Targeted, fail-CLOSED liveness re-check for ONE job name before an expensive resubmit (a 2nd query a
# partial bulk snapshot can't fool). Live -> 0 (skip the resubmit). bjobs is GUARDED by a timeout and its
# rc is honored: if the query FAILS (non-zero rc, e.g. a hung/timed-out LSF) treat the state as UNKNOWN
# and return 0 (live) so the caller SKIPS the resubmit -- never resubmit a possibly-running job on a bad
# query. Only a clean rc=0 query with no RUN/PEND counts as genuinely not-live.
sra_job_is_live() {
  local _out _rc
  _out=$(timeout 60 bjobs -noheader -o stat -J "$1" 2>/dev/null); _rc=$?
  [ "$_rc" -ne 0 ] && return 0     # query failed/timed out -> UNKNOWN -> fail-closed (treat as live)
  printf '%s\n' "$_out" | grep -qE 'RUN|PEND'
}

# Exactly-once finalization claim via atomic mkdir (NFS-safe). First caller -> 0.
sra_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# Stable hash of the live WORK-job name SET (pf/cs/fqd/pfm/cm) for no-churn stall detection. Snapshot=$1.
sra_live_work_hash() { printf '%s\n' "$1" | grep -E "^${JOB_TAG}_(pf|cs|fqd|pfm|cm)_" | sort | cksum | cut -d' ' -f1; }

# ---- per-accession failure tracking: drop a download/conversion after MAX_FAILS tries (user 2026-06-17) -
# A run that can't be fetched OR converted after MAX_FAILS attempts is DROPPED so one bad accession can't
# stall a study forever. Per-acc attempt count lives in $ATTEMPTS_DIR/<acc>.n; a <acc>.dropped marker stops
# further retries; the drop is appended to $DROPPED_LIST and COUNTED toward completion (so the gate passes),
# and any stranded source .sra is removed so the "zero .sra left" gate can also pass.
: "${MAX_FAILS:=3}"
ATTEMPTS_DIR="$PIPELINE_ROOT/.attempts"
DROPPED_LIST="$PIPELINE_ROOT/dropped_accessions.txt"
sra_attempts()   { cat "$ATTEMPTS_DIR/$1.n" 2>/dev/null || echo 0; }      # $1=acc -> attempt count
sra_is_dropped() { [ -f "$ATTEMPTS_DIR/$1.dropped" ]; }                   # $1=acc
sra_bump_attempt() {                                                      # $1=acc -> echoes the NEW count
  mkdir -p "$ATTEMPTS_DIR" 2>/dev/null
  local n=$(( $(sra_attempts "$1") + 1 )); echo "$n" > "$ATTEMPTS_DIR/$1.n"; echo "$n"
}
sra_drop_acc() {                                                          # $1=acc $2=studydir $3=stage
  [ -f "$ATTEMPTS_DIR/$1.dropped" ] && return 0          # idempotent: already dropped
  mkdir -p "$ATTEMPTS_DIR" 2>/dev/null; : > "$ATTEMPTS_DIR/$1.dropped"
  rm -f "$2/$1.sra" "$2/$1.sra.vdbcache" 2>/dev/null     # clear stranded source so the nsra==0 gate can pass
  printf '%s\t%s\t%s\tafter %s attempts\t%s\n' \
    "$1" "$(basename "$2")" "$3" "$(sra_attempts "$1")" "$(date '+%Y-%m-%d %H:%M:%S')" >> "$DROPPED_LIST"
}
# Is run $2 of study dir $1 DELIVERED? = it has a .fastq.gz (single- or paired-end, ANCHORED so SRR123 can't hide
# behind SRR1234), OR the STAR stage already aligned it and deleted its FASTQ to free disk -- run_star_job.sh leaves
# <study>/<acc>.aligned beside the FASTQ it deletes. Without that marker, re-arming the download stage after STAR
# (a re-run, a manual re-arm) counted every aligned run as missing and RE-DOWNLOADED it (K562/A549, 2026-09-22).
sra_delivered() {
  compgen -G "$1/$2.fastq.gz" >/dev/null 2>&1 || compgen -G "$1/${2}_[0-9].fastq.gz" >/dev/null 2>&1 || [ -e "$1/$2.aligned" ]
}

# Count dropped accessions with a PURE-BASH loop (NOT `ls|wc`/`grep -c` -> unreliable on compute nodes).
# Counts ONLY accessions a study's SraAccList.txt still lists AND that are not delivered: the completion gate is
# done + dropped >= expected, so a drop marker of a run no list names any more (a list rebuilt for a re-run), or of
# a run that was delivered after all (converted on a later attempt), would count twice and could finalize the stage
# with runs still missing (the 2026-09-22 re-run kit had to move such markers aside by hand).
sra_dropped_count() {
  local n=0 f a L sdir
  local -A _where=()
  for L in "$STUDIES_DIR"/*/SraAccList.txt; do
    [ -f "$L" ] || continue
    sdir="$(dirname "$L")"
    while IFS= read -r a || [ -n "$a" ]; do a="${a%$'\r'}"; [ -n "$a" ] && _where["$a"]="$sdir"; done < "$L"
  done
  for f in "$ATTEMPTS_DIR"/*.dropped; do
    [ -e "$f" ] || continue
    a="$(basename "$f" .dropped)"
    sdir="${_where[$a]:-}"
    [ -n "$sdir" ] || continue                 # no list names it any more -> not part of this run
    sra_delivered "$sdir" "$a" && continue     # delivered after all -> already counted as done
    n=$((n+1))
  done
  echo "$n"
}

# Submit a prefetch (download) job. Args: studydir  listfile  [dep_jobid]
# Downloads every accession in <studydir>/<listfile> into <studydir>. Echoes job id.
sra_submit_prefetch() {
  local sdir="$1" list="${2:-SraAccList.txt}" dep="${3:-}"
  sra_qopt; local wopt=(); [ -n "$dep" ] && wopt=(-w "ended($dep)")
  # WALLTIME / MEM SELF-HEAL: if this study's previous prefetch hit an LSF limit, escalate -W/-M for the
  # retry (else a huge download just gets killed again). WALL -> queue max on walltime; MEM +50% per mem kill.
  local _wall="$WALL" _mem="$PREFETCH_MEM_MB" _jo="$sdir/prefetch.out" _t _nm
  if [ -f "$_jo" ]; then
    _t=$(awk 'match($0,/TERM_(RUNLIMIT|MEMLIMIT)/){m=substr($0,RSTART,RLENGTH)} END{print m}' "$_jo" 2>/dev/null)
    _nm=$(awk '/TERM_MEMLIMIT/{n++} END{print n+0}' "$_jo" 2>/dev/null); _nm=${_nm:-0}
    [ "$_t" = "TERM_RUNLIMIT" ] && _wall="1108:00"
    [ "${_nm:-0}" -gt 0 ] && _mem=$(( _mem + _mem * _nm / 2 ))
  fi
  # FAIL-FAST on a full queue (see sra_submit_conversion): bound the bsub so a CALLER (run_all / fetch_missing)
  # never blocks here holding its slot; return 124 on timeout so the caller breaks + retries next pass.
  local _out _rc
  _out=$(timeout "${SRA_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M "$_mem" -W "$_wall" \
       -J "${JOB_TAG}_pf_$(basename "$sdir")" \
       -o "$sdir/prefetch.out" -e "$sdir/prefetch.err" \
       ${QOPT[@]+"${QOPT[@]}"} ${wopt[@]+"${wopt[@]}"} \
       "$SCRIPTS_DIR/prefetch_job.sh" "$sdir" "$list" 2>&1)
  _rc=$?
  [ "$_rc" -eq 124 ] && return 124
  printf '%s\n' "$_out" | sra_jobid
}

# Submit ONE conversion job. Args: accession  studydir   Echoes job id.
sra_submit_conversion() {
  local acc="$1" sdir="$2"
  sra_qopt
  # WALLTIME / MEM SELF-HEAL (see sra_submit_prefetch): escalate -W/-M if this accession's prior fasterq-dump
  # hit an LSF limit. WALL -> queue max on walltime; MEM +50% per mem kill.
  local _wall="$WALL" _mem="$MEM_MB" _jo="$sdir/fasterqdump_${acc}.out" _t _nm
  if [ -f "$_jo" ]; then
    _t=$(awk 'match($0,/TERM_(RUNLIMIT|MEMLIMIT)/){m=substr($0,RSTART,RLENGTH)} END{print m}' "$_jo" 2>/dev/null)
    _nm=$(awk '/TERM_MEMLIMIT/{n++} END{print n+0}' "$_jo" 2>/dev/null); _nm=${_nm:-0}
    [ "$_t" = "TERM_RUNLIMIT" ] && _wall="1108:00"
    [ "${_nm:-0}" -gt 0 ] && _mem=$(( _mem + _mem * _nm / 2 ))
  fi
  # FAIL-FAST on a full queue: bound the submit so a convert_study (cs) job NEVER spins here holding its run
  # slot while bsub retries "Pending job threshold reached" -- that DEADLOCKS (LIVE 2026-06-26: 119 cs jobs
  # held every one of the 125 slots, each unable to submit the very fqd conversion jobs that needed those
  # slots). On timeout return 124 so the caller (convert_study.sh) EXITS + releases the slot; the watchdog
  # re-runs it idempotently when the queue frees.
  local _out _rc
  _out=$(timeout "${SRA_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n "$THREADS" -M "$_mem" -W "$_wall" -R "span[hosts=1]" \
       -J "${JOB_TAG}_fqd_${acc}" \
       -o "$sdir/fasterqdump_${acc}.out" -e "$sdir/fasterqdump_${acc}.err" \
       ${QOPT[@]+"${QOPT[@]}"} \
       "$SCRIPTS_DIR/fasterqdump_job.sh" "$acc" "$sdir" 2>&1)
  _rc=$?
  [ "$_rc" -eq 124 ] && return 124          # submit blocked (queue full) -> don't hold the slot
  printf '%s\n' "$_out" | sra_jobid
}

# Submit the flatten+convert step for a study (typically gated on its prefetch).
# Args: studydir  [listfile]  [dep_jobid]
#   listfile empty -> handle ALL .sra in the study; else only those accessions.
# Echoes job id.
sra_submit_convert_study() {
  local sdir="$1" list="${2:-}" dep="${3:-}"
  sra_qopt; local wopt=(); [ -n "$dep" ] && wopt=(-w "ended($dep)")
  # FAIL-FAST on a full queue (see sra_submit_conversion): bound the bsub so a CALLER (run_all / fetch_missing)
  # never blocks here holding its slot; return 124 on timeout so the caller breaks + retries next pass.
  local _out _rc
  _out=$(timeout "${SRA_SUBMIT_TIMEOUT:-120}" bsub -L /bin/bash -n 1 -M 2000 -W 66480 \
       -J "${JOB_TAG}_cs_$(basename "$sdir")" \
       -o "$sdir/convert_study.out" -e "$sdir/convert_study.err" \
       ${QOPT[@]+"${QOPT[@]}"} ${wopt[@]+"${wopt[@]}"} \
       "$SCRIPTS_DIR/convert_study.sh" "$sdir" "$list" 2>&1)
  _rc=$?
  [ "$_rc" -eq 124 ] && return 124
  printf '%s\n' "$_out" | sra_jobid
}

# Count accessions in a study that are delivered (a .fastq.gz, or aligned by STAR -- see sra_delivered).
# Anchored to SraAccList.txt when present (bounded by the list, immune to stale-NFS
# directory over-counts on compute nodes); falls back to a file scan otherwise.
sra_done_count() {  # arg: studydir
  local sdir="$1" a n=0
  if [ -f "$sdir/SraAccList.txt" ]; then
    while read -r a; do
      a=$(echo "$a" | tr -d '\r'); [ -z "$a" ] && continue
      sra_delivered "$sdir" "$a" && n=$((n+1))
    done < "$sdir/SraAccList.txt"
    echo "$n"
  else
    ls "$sdir"/*.fastq.gz "$sdir"/*.aligned 2>/dev/null \
      | sed 's#.*/##; s/_[0-9]\.fastq\.gz$//; s/\.fastq\.gz$//; s/\.aligned$//' | sort -u | wc -l
  fi
}

# Count ${JOB_TAG}_(pf|cs|fqd|pfm|cm)_* WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH
# loop -- NOT `grep -c` (on this cluster's COMPUTE NODES grep -c returns an EMPTY count even though
# matching works; that wedged the watchdog completion gate on the BED stage, LIVE 2026-06-07).
sra_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in
      ${JOB_TAG}_pf_*|${JOB_TAG}_cs_*|${JOB_TAG}_fqd_*|${JOB_TAG}_pfm_*|${JOB_TAG}_cm_*) n=$((n+1)) ;;
    esac
  done <<< "${1:-}"
  echo "$n"
}

# Count non-blank lines in a file with a PURE-BASH loop (replaces the unreliable `grep -c .`).
sra_count_nonblank() {
  local n=0 _l
  [ -f "${1:-}" ] || { echo 0; return; }
  while IFS= read -r _l || [ -n "$_l" ]; do
    case "$_l" in (*[![:space:]]*) n=$((n+1)) ;; esac
  done < "$1"
  echo "$n"
}

# Count of live WORK jobs (prefetch/convert/fasterqdump + the re-fetch variants), NEVER the watchdog
# itself -> can reach 0. Mirrors star_live_work_count / bed_live_work_count.
sra_live_work_count() { sra_count_work "$(sra_live_names)"; }

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass. Called by
# fasterqdump_job.sh right after the LAST conversion publishes its .fastq.gz + drops the source .sra. If
# this is the LAST live work job, it queues a watchdog pass GATED ON THIS JOB ENDING (-w ended($LSB_JOBID))
# so finalize lands within seconds of the final conversion instead of up to a full poll interval later.
# PURE ACCELERATOR -- the timed 30-min poll stays the fallback, so a last job that dies WITHOUT nudging is
# still finalized by the poll. Mirrors star_nudge_watchdog / bed_nudge_watchdog (incl. the runaway fix):
#   * EXACTLY 1 (this still-RUN job), NOT <=1 -- nlive==0 means bjobs failed/overloaded -> do NOT nudge
#     (a spurious 0 under load is what once spawned ~1000 watchdogs). flock(-n): only ONE nudger if several
#     finish together. Spurious/early nudges are absorbed by the already-finalized guard + reschedule-first.
sra_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0                 # another finisher is already nudging -> skip
  local nlive; nlive="$(sra_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  [ "$nlive" -eq 1 ] || return 0
  sra_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  bsub -L /bin/bash -n 1 -M 1000 -W "$(( ${WATCHDOG_INTERVAL_MIN:-30} - 5 ))" -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$PIPELINE_ROOT/watchdog.out" -e "$PIPELINE_ROOT/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' last live work job -> watchdog queued on ended(${LSB_JOBID:-?})" \
       >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
}
