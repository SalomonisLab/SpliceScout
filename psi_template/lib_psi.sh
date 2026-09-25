#!/usr/bin/env bash
# lib_psi.sh -- shared LSF submit + accounting helpers for the AltAnalyze (PSI) stage.
# Sourced AFTER config.sh by run_psi_pipeline.sh, watchdog.sh, run_psi_job.sh, setup.sh, status.sh.
# This stage runs ONE AltAnalyze job over the whole BED dir (not per-sample), so the accounting is
# simpler than BED's: "done" = the PSI output table exists; "live" = the single ${JOB_TAG}_job is queued.

# Fail fast if not on an LSF submit host.
psi_require_bsub() {
  command -v bsub >/dev/null 2>&1 && return 0
  echo "ERROR: 'bsub' not found -- run this on the LSF SUBMIT host (e.g. bmiclusterp-head)." >&2
  exit 1
}

# QOPT=(-q QUEUE) if a queue is configured, else empty array.
psi_qopt() { QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE"); }

# Parse "Job <12345> is submitted ..." -> 12345
psi_jobid() { sed -n 's/.*Job <\([0-9]\{1,\}\)>.*/\1/p'; }

# Snapshot of all live (PEND+RUN) LSF job names, one per line.
psi_live_names() { bjobs -noheader -o job_name 2>/dev/null; }
psi_snapshot()   { bjobs -noheader -o job_name 2>/dev/null; }

# Is job-name $1 present (exact match) in the newline-separated snapshot $2?
psi_has_live() { printf '%s\n' "$2" | grep -qxF "$1"; }

# The single work job's LSF name (the "_job" infix tells it apart from the watchdog).
psi_jobname() { printf '%s_job' "$JOB_TAG"; }

# Targeted, fail-CLOSED liveness re-check for the work job before an expensive resubmit.
psi_job_is_live() { bjobs -noheader -o stat -J "$(psi_jobname)" 2>/dev/null | grep -qE 'RUN|PEND'; }

# THE single "is the PSI analysis finished" predicate -- used by the job body AND the watchdog so they can
# never disagree. AltAnalyze's RNASeq splicing deliverable is the per-sample PSI event-annotation table
# under <PSI_OUT>/AltResults/AlternativeOutput/ (written whether or not a group comparison runs). Counted
# with PURE-BASH globbing (nullglob), NOT ls|grep -c: on this cluster's COMPUTE NODES `grep -c` returns an
# EMPTY count (see lib_bed.sh) which would wedge the watchdog's integer gate.
psi_done() {
  shopt -s nullglob
  local a f table=0
  # require a NON-EMPTY table with >1 line (header + >=1 event), not mere existence: a truncated/header-only
  # EventAnnotation from a crashed/killed AltAnalyze run would otherwise count as DONE -> a false COMPLETE
  # (and the watchdog would never resubmit). (awk END{NR}, not grep -c -- reliable on compute nodes.)
  a=("$PSI_OUT"/AltResults/AlternativeOutput/*EventAnnotation*)
  for f in ${a[@]+"${a[@]}"}; do
    [ -s "$f" ] && [ "$(awk 'END{print NR+0}' "$f" 2>/dev/null)" -gt 1 ] && { table=1; break; }
  done
  if [ "$table" = 0 ]; then
    a=("$PSI_OUT"/AltResults/AlternativeOutput/*PSI*.txt)   # fallback: any non-trivial PSI table emitted
    for f in ${a[@]+"${a[@]}"}; do
      [ -s "$f" ] && [ "$(awk 'END{print NR+0}' "$f" 2>/dev/null)" -gt 1 ] && { table=1; break; }
    done
  fi
  [ "$table" = 1 ] || return 1
  # The per-sample table is written BEFORE the per-COMPARISON dPSI files (Events-dPSI_*/PSI.<cond>_vs_<ctrl>.txt,
  # the only thing concordance scores). The table alone used to mean DONE, so an AltAnalyze run that crashed,
  # was killed or froze part-way through the comparisons became COMPLETE with only the FIRST comparisons written
  # (numbered in sorted-label = sorted-GSE order) -> concordance scored a handful of studies and silently dropped
  # the rest; a resubmit even skipped the rerun for the same reason. Now: done = the table AND (AltAnalyze exited
  # cleanly -- ALTANALYZE_OK.txt, written by run_psi_job.sh -- OR every requested comparison file exists).
  local exp got
  exp="$(psi_comparisons_expected)"
  [ "${exp:-0}" -gt 0 ] || return 0                 # no comparisons requested -> the table is the deliverable
  [ -f "$PIPELINE_ROOT/ALTANALYZE_OK.txt" ] && return 0
  got="$(psi_comparisons_produced)"
  [ "${got:-0}" -ge "$exp" ]
}

# Comparisons REQUESTED = non-blank lines of comps.<expname>.txt (pure bash; never grep -c on compute nodes).
psi_comparisons_expected() {
  local n=0 _l
  [ -s "${COMPS_FILE:-}" ] || { echo 0; return 0; }
  while IFS= read -r _l || [ -n "$_l" ]; do case "$_l" in (*[![:space:]]*) n=$((n+1)) ;; esac; done < "$COMPS_FILE"
  echo "$n"
}

# Comparisons PRODUCED = distinct PSI.*_vs_*.txt[.gz] dPSI files under any Events-dPSI_* folder (the PSI stage's own
# compression may have gzipped them).
psi_comparisons_produced() {
  shopt -s nullglob
  local -A _seen
  local n=0 f b
  for f in "$PSI_OUT"/AltResults/AlternativeOutput/Events-dPSI_*/PSI.*_vs_*.txt \
           "$PSI_OUT"/AltResults/AlternativeOutput/Events-dPSI_*/PSI.*_vs_*.txt.gz; do
    b="${f##*/}"; b="${b%.gz}"
    if [ -z "${_seen[$b]+x}" ]; then _seen[$b]=1; n=$((n+1)); fi
  done
  echo "$n"
}

# Per-comparison report -> $PIPELINE_ROOT/PSI_COMPARISONS.tsv (requested comparison, expected dPSI file, produced?,
# event rows). Echoes "<produced>/<requested>". Read by the concordance stage's per-study coverage section.
psi_comparison_report() {
  shopt -s nullglob
  local out="$PIPELINE_ROOT/PSI_COMPARISONS.tsv" key gnum gname e b ne nb f cand n=0 got=0 rows
  local -A _name
  if [ -s "${GROUPS_FILE:-}" ]; then
    while IFS=$'\t' read -r key gnum gname || [ -n "${key:-}" ]; do
      [ -n "${gnum:-}" ] && _name[$gnum]="${gname:-group$gnum}"
    done < "$GROUPS_FILE"
  fi
  printf 'exp_group\tbase_group\tdpsi_file\tproduced\tevent_rows\n' > "$out"
  if [ -s "${COMPS_FILE:-}" ]; then
    while IFS=$'\t' read -r e b || [ -n "${e:-}" ]; do
      e="${e%$'\r'}"; b="${b%$'\r'}"
      [ -n "${e:-}" ] && [ -n "${b:-}" ] || continue
      ne="${_name[$e]:-$e}"; nb="${_name[$b]:-$b}"
      n=$((n+1)); f=""
      for cand in "$PSI_OUT"/AltResults/AlternativeOutput/Events-dPSI_*/"PSI.${ne}_vs_${nb}.txt" \
                  "$PSI_OUT"/AltResults/AlternativeOutput/Events-dPSI_*/"PSI.${ne}_vs_${nb}.txt.gz"; do
        f="$cand"; break
      done
      if [ -n "$f" ]; then
        got=$((got+1))
        case "$f" in *.gz) rows="$(gzip -dc "$f" 2>/dev/null | awk 'END{print NR-1}')" ;;
                     *)    rows="$(awk 'END{print NR-1}' "$f" 2>/dev/null)" ;; esac
        printf '%s\t%s\t%s\tyes\t%s\n' "$ne" "$nb" "${f##*/}" "${rows:-}" >> "$out"
      else
        printf '%s\t%s\tPSI.%s_vs_%s.txt\tNO\t\n' "$ne" "$nb" "$ne" "$nb" >> "$out"
      fi
    done < "$COMPS_FILE"
  fi
  echo "$got/$n"
}

# Count ${JOB_TAG}_job WORK-job names in a bjobs snapshot string ($1) with a PURE-BASH loop -- NOT
# `grep -c` (its count output is empty on the compute nodes). The watchdog passes its already-captured
# snapshot so the count matches the same bjobs read it gated on. (At most 1 for this single-job stage.)
psi_count_work() {
  local n=0 _jn
  while IFS= read -r _jn; do
    case "$_jn" in ${JOB_TAG}_job) n=$((n+1)) ;; esac
  done <<< "${1:-}"
  echo "$n"
}
psi_live_work_count() { psi_count_work "$(psi_live_names)"; }

# Submit the ONE AltAnalyze job. Echoes the LSF job id.
psi_submit_job() {
  psi_qopt
  bsub -L /bin/bash ${QOPT[@]+"${QOPT[@]}"} \
       -J "$(psi_jobname)" -n "$THREADS" -W "$WALL" -M "$MEM_MB" \
       -R "span[hosts=1]" \
       -o "$LOG_DIR/psi_job.out" -e "$LOG_DIR/psi_job.err" \
       "$SCRIPTS_DIR/run_psi_job.sh" | psi_jobid
}

# Exactly-once finalization claim via an atomic mkdir (NFS-safe where flock degrades). First caller -> 0.
psi_finalize_once() { mkdir "$PIPELINE_ROOT/.finalized.lock" 2>/dev/null; }

# Wake the watchdog NOW instead of waiting up to WATCHDOG_INTERVAL_MIN for its next timed pass. Called by
# run_psi_job.sh after AltAnalyze finishes and the PSI table is present. Gated on THIS job ending so the
# woken pass sees nlive==0 and FINALIZES. PURE ACCELERATOR -- the timed poll stays the fallback.
#   * EXACTLY 1 live work job (this still-RUN job): nlive==0 means bjobs FAILED -> do NOT nudge.
#   * flock(-n): only one nudge; spurious/early nudges are absorbed by the already-finalized guard.
psi_nudge_watchdog() {
  local who="${1:-?}"
  command -v bsub  >/dev/null 2>&1 || return 0
  command -v flock >/dev/null 2>&1 || return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_COMPLETE.txt" ] && return 0
  [ -f "$PIPELINE_ROOT/PIPELINE_STALLED.txt"  ] && return 0
  exec 9>"$PIPELINE_ROOT/.nudge.lock" 2>/dev/null || return 0
  flock -n 9 2>/dev/null || return 0
  local nlive; nlive="$(psi_live_work_count 2>/dev/null)"; nlive="${nlive:-0}"
  [ "$nlive" -eq 1 ] || return 0
  psi_qopt
  local DEPW=(); [ -n "${LSB_JOBID:-}" ] && DEPW=(-w "ended(${LSB_JOBID})")
  bsub -L /bin/bash -n 1 -M 1000 -W 20 -J "${JOB_TAG}_watchdog" \
       ${DEPW[@]+"${DEPW[@]}"} \
       -o "$LOG_DIR/watchdog.out" -e "$LOG_DIR/watchdog.err" \
       ${QOPT[@]+"${QOPT[@]}"} "$SCRIPTS_DIR/watchdog.sh" >/dev/null 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] nudge: '$who' finished -> watchdog queued on ended(${LSB_JOBID:-?})" \
       >> "$PIPELINE_ROOT/watchdog.log" 2>/dev/null || true
}

# ---- BED integrity pre-flight (consumer-side guard) -------------------------------------------------
# AltAnalyze's RNASeq importer has a BROKEN bad-line handler (it raises an undefined name
# 'force_exception'), so ONE truncated bed makes a worker die and the whole import wedge -- CPU spinning,
# zero output -- until walltime. This guard runs BEFORE AltAnalyze and moves any truncated/corrupt bed out
# of the way so the run completes on the good samples instead of hanging for hours. A bed "looks intact" if
# it is non-empty, ENDS IN A NEWLINE (not cut mid-line by a kill / NFS stall), and its last row parses as a
# bed line (>=6 tab fields, numeric start/end). Same test the BAM->BED stage uses (lib_bed.sh bed_file_ok),
# reproduced here so the PSI stage stays self-contained. (Exon line-count is NOT used: the exon.bed count is
# constant per species but NOT equal to the reference's line count, so it isn't portably derivable.)
psi_bed_file_ok() {
  local f="$1"
  [ -s "$f" ] || return 1
  [ -z "$(tail -c1 "$f" 2>/dev/null)" ] || return 1                 # last byte must be a newline
  tail -n 1 "$f" 2>/dev/null | awk -F'\t' 'NF>=6 && $2 ~ /^[0-9]+$/ && $3 ~ /^[0-9]+$/ {ok=1} END{exit ok?0:1}'
}

# DEEP full-file integrity scan for a junction/intronJunction BED (BED12). EVERY data line must be 12
# tab-separated columns with integer chromStart/chromEnd; track/browser/# header lines are skipped.
# Early-exits on the FIRST bad line, so a corrupt file is cheap while a clean one is read fully (the cost
# of certifying it). Unlike psi_bed_file_ok (which checks only the LAST line), this catches MID-FILE
# interleaved/garbled/NUL-byte corruption -- the kind a tail check is blind to, and the kind that wedges
# AltAnalyze's RNASeq.py importBEDFile: its bad-line handler is `print t; force_exception` (an UNDEFINED
# name) -> NameError -> the multiprocessing import worker dies -> the parent DEADLOCKS for the whole
# walltime with no output (observed LIVE: a 14h-frozen A549 PSI job on a corrupt __intronJunction.bed).
psi_bed_scan_ok() {
  local f="$1"
  [ -s "$f" ] || return 1
  [ -z "$(tail -c1 "$f" 2>/dev/null)" ] || return 1
  LC_ALL=C awk -F'\t' '/^(track|browser|#)/{next} (NF!=12 || $2!~/^[0-9]+$/ || $3!~/^[0-9]+$/){exit 1}' "$f"
}

# Move every sample whose junction OR exon bed is not intact (ALL its bed types) into a quarantine dir
# OUTSIDE the bedDir (so AltAnalyze's --bedDir glob never sees them), log each to BED_QUARANTINE.txt, and
# echo the count quarantined. Idempotent; safe to run on every attempt. mv on a symlinked bed moves the
# symlink (the real bed is untouched).
psi_check_beds() {
  shopt -s nullglob
  local q=0 qd="$PIPELINE_ROOT/quarantined_beds" rep="$PIPELINE_ROOT/BED_QUARANTINE.txt"
  local j s suf ok
  for j in "$BED_INPUT_DIR"/*__junction.bed; do
    s="$(basename "$j" __junction.bed)"
    ok=1
    # junction + intronJunction are BOTH fed to AltAnalyze (the PSI inputs) -> FULL-SCAN both. The
    # intronJunction bed was previously UNCHECKED, which is exactly how a corrupt one slipped through and
    # deadlocked the live A549 PSI for 14h. (exon bed, if any reached the bedDir, keeps the cheap last-line
    # check -- it is a different 10-col format and is excluded from the PSI input anyway.)
    psi_bed_scan_ok "$j" || ok=0
    # A ZERO-BYTE __intronJunction.bed is VALID: BAMtoExonBED's intron-retention pass is hard-gated and
    # legitimately writes nothing for some libraries, and the BED stage accepts it (lib_bed.sh bed_intron_ok:
    # "exists, empty OK"). psi_bed_scan_ok rejects empty files, so scanning it unconditionally quarantined
    # perfectly good samples -- enough to drop small control groups below MIN_PER_GROUP and silently remove
    # every comparison of that study. Only a NON-empty intron BED is scanned (a corrupt one still quarantines).
    if [ "$ok" = "1" ] && [ -s "$BED_INPUT_DIR/${s}__intronJunction.bed" ]; then
      psi_bed_scan_ok "$BED_INPUT_DIR/${s}__intronJunction.bed" || ok=0
    fi
    if [ "$ok" = "1" ] && [ -e "$BED_INPUT_DIR/${s}__exon.bed" ]; then
      psi_bed_file_ok "$BED_INPUT_DIR/${s}__exon.bed" || ok=0
    fi
    [ "$ok" = "1" ] && continue
    mkdir -p "$qd" 2>/dev/null
    for suf in __junction.bed __exon.bed __intronJunction.bed; do
      [ -e "$BED_INPUT_DIR/${s}${suf}" ] && mv -f "$BED_INPUT_DIR/${s}${suf}" "$qd/" 2>/dev/null
    done
    q=$((q+1))
    echo "$(date '+%Y-%m-%d %H:%M:%S')  $s  (truncated/corrupt junction/intronJunction/exon BED)" >> "$rep"
  done
  [ "$q" -gt 0 ] && echo "[psi] PREFLIGHT: quarantined $q sample(s) with truncated BEDs -> $qd (see $(basename "$rep"))" >&2
  echo "$q"
}
