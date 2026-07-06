#!/usr/bin/env bash
# =============================================================================
# run_bed_job.sh -- AltAnalyze junction+exon BED extraction for ONE BAM (LSF job body).
# Sources config.sh for all settings. Hardened:
#   * idempotent: skips if both BEDs already exist & are non-empty (bed_done)
#   * indexes the BAM only if a .bai is missing (STAR usually already indexed it)
#   * runs the VENDORED AltAnalyze scripts (altanalyze/) -> no AltAnalyze install needed;
#     only the stock python/2.7.5 (pysam) + samtools modules
#   * writes <sample>__junction.bed (+ __exon.bed and/or __intronJunction.bed per BED_MODE) NEXT TO the BAM (tool-forced)
#   * verifies both BEDs landed (else exits non-zero so the watchdog resubmits)
# Usage: run_bed_job.sh <LABEL>     (LABEL = BAM basename without .bam)
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_bed.sh"
set -u
: "${BED_OUT_DIR:=$(dirname "$BAM_INPUT_DIR")/STAR_beds}"   # robust if an older deployed config.sh predates BED_OUT_DIR

LABEL="$1"
BAM="$BAM_INPUT_DIR/$LABEL.bam"

bed_load_modules
command -v samtools >/dev/null 2>&1 || { echo "[bed] $LABEL: samtools not on PATH" >&2; exit 1; }
command -v python   >/dev/null 2>&1 || { echo "[bed] $LABEL: python (2.7) not on PATH" >&2; exit 1; }
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" || { echo "[bed] $LABEL: cannot mkdir state dirs" >&2; exit 1; }

[ -s "$BAM" ]      || { echo "[bed] $LABEL: BAM missing/empty: $BAM" >&2; exit 1; }
[ -s "$EXON_REF" ] || { echo "[bed] $LABEL: exon reference missing: $EXON_REF" >&2; exit 1; }

# idempotent: both BEDs already present & non-empty -> skip
if bed_done "$LABEL"; then echo "[bed] $LABEL: BEDs present -> skip"; exit 0; fi

JB="$ALTANALYZE_DIR/import_scripts/BAMtoJunctionBED.py"
EB="$ALTANALYZE_DIR/import_scripts/BAMtoExonBED.py"
[ -s "$JB" ] && [ -s "$EB" ] || { echo "[bed] $LABEL: vendored AltAnalyze scripts missing under $ALTANALYZE_DIR" >&2; exit 1; }

# 1) index only if missing (STAR already indexes on publish; accept .bam.bai OR .bai)
if [ ! -s "$BAM.bai" ] && [ ! -s "${BAM%.bam}.bai" ]; then
  echo "[bed] $LABEL: samtools index"
  samtools index "$BAM" || { echo "[bed] $LABEL: samtools index FAILED" >&2; exit 1; }
fi

# PYTHONPATH lets the vendored scripts find export/unique (siblings) + the import_scripts package.
export PYTHONPATH="$ALTANALYZE_DIR${PYTHONPATH:+:$PYTHONPATH}"

# toolkit re-check (T1.3): a racing finalize cleanup is now gated on no-live-jobs, but re-verify the
# vendored scripts + exon ref right before use anyway and fail DISTINCTLY (rc=3) if they vanished mid-job.
[ -s "$JB" ] && [ -s "$EB" ] && [ -s "$EXON_REF" ] || {
  echo "[bed] $LABEL: vendored AltAnalyze toolkit/exon-ref vanished mid-job (cleanup race?) -- aborting (rc=3)" >&2; exit 3; }

# COLLISION-PROOF WORKSPACE -- the ROOT FIX for the concurrent-writer corruption. The AltAnalyze tools
# derive EVERY output path by string.replace on the --i BAM path (BAMtoJunctionBED.py:254 -> <bam>__junction.bed,
# plus __isoforms.txt) and the --r ref path (BAMtoExonBED.py:82/84/86 -> __exon/__intronJunction/__minimumIntronIntervals).
# So whenever two jobs ran for the SAME sample (a duplicate watchdog resubmit), BOTH wrote the SAME fixed
# <sample>__*.bed next to the BAM at the same time -> interleaved/truncated beds. That is the ROOT cause of
# BOTH the A549 corrupt __intronJunction beds AND the MDS_L truncated __junction beds. Fix: give EVERY job a
# PRIVATE work dir (under BED_OUT_DIR so the publish is a same-volume atomic rename) and point the tools at
# SYMLINKS of the BAM (+ its index) and the exon ref INSIDE it, so every derived output lands in the private
# dir. Two concurrent jobs can no longer touch the same file; the final publish is an atomic rename =
# last-writer-wins on a COMPLETE file, never a torn read. Symlinks only -- no BAM copy.
mkdir -p "$BED_OUT_DIR" || { echo "[bed] $LABEL: cannot mkdir BED_OUT_DIR=$BED_OUT_DIR" >&2; exit 1; }
WORK="$BED_OUT_DIR/.bedwork/${LABEL}.${LSB_JOBID:-$$}"
rm -rf "$WORK" 2>/dev/null; mkdir -p "$WORK" || { echo "[bed] $LABEL: cannot mkdir work dir $WORK" >&2; exit 1; }
trap 'cd / 2>/dev/null; rm -rf "$WORK"' EXIT
WBAM="$WORK/${LABEL}.bam"; ln -sfn "$BAM" "$WBAM"
if   [ -s "$BAM.bai" ];        then ln -sfn "$BAM.bai" "$WBAM.bai"
elif [ -s "${BAM%.bam}.bai" ]; then ln -sfn "${BAM%.bam}.bai" "$WORK/${LABEL}.bai"; fi
WREF="$WORK/$(basename "$EXON_REF")"; ln -sfn "$EXON_REF" "$WREF"
cd "$WORK" || { echo "[bed] $LABEL: cannot cd work dir $WORK" >&2; exit 1; }

# 2) junction BED  (species flag is --species) -- writes <WORK>/<LABEL>__junction.bed
echo "[bed] $LABEL: BAMtoJunctionBED (species=$SPECIES) [private workdir]"
python "$JB" --i "$WBAM" --species "$SPECIES" --r "$WREF" \
  || { echo "[bed] $LABEL: BAMtoJunctionBED FAILED" >&2; exit 1; }

# 3) exon / intron-retention BED via BAMtoExonBED (species flag is --s -- NOT --species).
#    BED_MODE picks the pass(es). chr naming (BAM '1' vs ref 'chr1') is auto-reconciled by
#    the tool, so the vendored chr-prefixed ref is correct in every mode.
#      intron (default): NO flag -> default intronRetentionOnly=True -> <sample>__intronJunction.bed
#      exon            : --intronRetentionOnly False                 -> <sample>__exon.bed
#      both            : exon pass FIRST, then the plain intron pass LAST (both rewrite
#                        __intronJunction.bed; the default-True pass is authoritative -> runs last)
echo "[bed] $LABEL: BAMtoExonBED (species=$SPECIES, mode=${BED_MODE:-intron})"
case "${BED_MODE:-intron}" in
  exon)
    python "$EB" --i "$WBAM" --r "$WREF" --s "$SPECIES" --intronRetentionOnly False \
      || { echo "[bed] $LABEL: BAMtoExonBED (exon) FAILED" >&2; exit 1; } ;;
  both)
    python "$EB" --i "$WBAM" --r "$WREF" --s "$SPECIES" --intronRetentionOnly False \
      || { echo "[bed] $LABEL: BAMtoExonBED (exon) FAILED" >&2; exit 1; }
    python "$EB" --i "$WBAM" --r "$WREF" --s "$SPECIES" \
      || { echo "[bed] $LABEL: BAMtoExonBED (intron) FAILED" >&2; exit 1; } ;;
  *)
    python "$EB" --i "$WBAM" --r "$WREF" --s "$SPECIES" \
      || { echo "[bed] $LABEL: BAMtoExonBED (intron) FAILED" >&2; exit 1; } ;;
esac

# 3b) VALIDATE in the PRIVATE workdir BEFORE the atomic publish. A mid-write kill leaves a truncated-but-
#     nonempty .bed that a bare [ -s ] would wrongly accept as DONE; validate first so the watchdog never
#     publishes a truncated BED. __intronJunction.bed may be legitimately EMPTY (hard-gated junctions), but a
#     NON-empty one is a PSI input and must be intact (bed_intron_ok), else AltAnalyze's import deadlocks.
_stage="$WORK/${LABEL}"
bed_file_ok "${_stage}__junction.bed" || {
  echo "[bed] $LABEL: __junction.bed missing/truncated -- discarding (safe to resubmit)" >&2; exit 1; }
bed_intron_ok "${_stage}__intronJunction.bed" || {
  echo "[bed] $LABEL: __intronJunction.bed present-but-corrupt -- discarding (safe to resubmit)" >&2; rm -f "${_stage}__intronJunction.bed"; exit 1; }
case "${BED_MODE:-intron}" in
  exon)
    bed_file_ok "${_stage}__exon.bed" || {
      echo "[bed] $LABEL: __exon.bed missing/truncated -- discarding (safe to resubmit)" >&2; exit 1; } ;;
  both)
    # exon is BEST-EFFORT in 'both' mode: PSI uses junction+intronJunction, not __exon.bed, and the exon
    # pass OOM-truncates on very large BAMs. Drop a bad exon but KEEP PUBLISHING junction+intronJunction
    # so the sample completes instead of resubmitting forever (LIVE A549 2026-06: 16 huge BAMs stalled BED).
    bed_file_ok "${_stage}__exon.bed" || {
      echo "[bed] $LABEL: __exon.bed missing/truncated -- DROPPING exon bed, keeping junction+intronJunction (PSI ignores exon)" >&2
      rm -f "${_stage}__exon.bed"; } ;;
esac
# ATOMIC PUBLISH: $WORK is under BED_OUT_DIR (same volume), so each mv is a rename -- last-writer-wins on a
# COMPLETE file, never a torn read, even if a duplicate job publishes the same name.
for _suf in __junction.bed __exon.bed __intronJunction.bed; do
  [ -e "${_stage}${_suf}" ] && mv -f "${_stage}${_suf}" "$BED_OUT_DIR/${LABEL}${_suf}"
done

# 4) verify both BEDs landed & are non-empty (same predicate the watchdog uses)
if ! bed_done "$LABEL"; then
  echo "[bed] $LABEL: conversion ran but a BED is missing/empty -- not done (safe to resubmit)" >&2
  exit 1
fi
echo "[bed] $LABEL: complete (mode=${BED_MODE:-intron}) -> BEDs in $BED_OUT_DIR"

# Optionally free the STAR_bams volume: the BAM is no longer needed once its BEDs are made + verified.
# Default OFF -- BAMs are the primary alignment output; re-making BEDs after this needs re-alignment.
if [ "${DELETE_BAM_AFTER_BED:-0}" = "1" ]; then
  # PARTIAL-run guard (T1.1/T1.2): if the upstream finished incomplete, the launcher dropped an
  # INCOMPLETE_UPSTREAM marker in the BED root -> KEEP the BAM (a partial run must stay re-runnable; the
  # BAM is the last recoverable tier once FASTQ/.sra are gone).
  if [ -f "$PIPELINE_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ]; then
    echo "[bed] $LABEL: run is PARTIAL (upstream incomplete) -> KEEPING BAM (deletion disabled this run)" >&2
  else
    rm -f "$BAM" "$BAM.bai" "${BAM%.bam}.bai" 2>/dev/null && echo "[bed] $LABEL: deleted BAM $BAM (BEDs are made)"
  fi
fi

# 5) wake the watchdog if this was the last live work job (pure accelerator; poll is the fallback)
bed_nudge_watchdog "$LABEL" || true
