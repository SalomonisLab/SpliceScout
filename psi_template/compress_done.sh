#!/usr/bin/env bash
# =============================================================================
# compress_done.sh -- post-completion space reclaimer (LSF job body).
# Submitted by watchdog.sh ONLY after the PSI stage finalizes COMPLETE. Compresses kept, still-
# uncompressed files (BEDs + AltAnalyze text outputs: exp/counts, EventAnnotation, PSI tables) under
# COMPRESS_DIR, with gzip (pigz) or xz (LZMA2) per COMPRESS_WHEN_DONE.
#   * SKIPS already-compressed files (*.gz/.bz2/.xz/.zst/.zip) and BAM/BAI/CRAM (already compressed
#     binary -- re-compressing costs hours for ~no gain), and tiny control files (by size + name).
#   * SKIPS .sra/.sralite/.vdbcache: SRA files are compressed internally, AND they are the download stage's
#     INPUTS -- K562 (2026-08) had 4,081 downloaded-but-unconverted .sra gzipped to .sra.gz, which the download
#     scripts no longer recognized as downloads.
#   * SKIPS the pipeline's TOOLING and stage inputs (the AltAnalyze toolkit + exon reference, a STAR index, the
#     run tables and sample / BAM / group lists): gzipping them broke every later re-run -- the BED stage needs the
#     plain <SP>_Ensembl_exon.txt and STAR its SraRunTable_<line>.csv (A549 / K562, found 2026-09-22).
#   * Does NOT follow symlinks (find -type f), so a symlinked BED dir is left alone.
#   * Idempotent: a re-run only compresses what isn't compressed yet. Writes COMPRESSION_COMPLETE.txt.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
if [ -f "$HERE/lib_notify.sh" ]; then source "$HERE/lib_notify.sh"; else
  log_event(){ :; }; notify_error(){ :; }; notify_update(){ :; }; fi
set -u; shopt -s nullglob
LOG="$PIPELINE_ROOT/watchdog.log"
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "[$(ts)] [compress] $*" >> "$LOG"; }

mode="${COMPRESS_WHEN_DONE:-gzip}"
if [ "$mode" = "off" ]; then say "COMPRESS_WHEN_DONE=off -> nothing to do"; exit 0; fi

dir="${COMPRESS_DIR:-}"
[ -n "$dir" ] || dir="$(dirname "$PIPELINE_ROOT")"
if [ ! -d "$dir" ]; then say "COMPRESS_DIR not found: $dir -> skipping"; exit 0; fi
minmb="${COMPRESS_MIN_MB:-1}"
T="${COMPRESS_THREADS:-8}"

# Choose the compressor (echoes the tool actually used). xz = LZMA2.
comp() { :; }   # placeholder; redefined below (declared so set -u never trips on it)
# Robust compressor: the THREADED flag (xz -T / pigz -p) is UNSUPPORTED on older builds and makes the tool
# error on EVERY file (LIVE MDSL 2026-06-15: "compressed 0 file(s) (72 failed) with xz" was an old xz with
# no -T). Each comp() therefore falls back to the single-threaded call if the threaded one fails.
if [ "$mode" = "xz" ]; then
  if ! command -v xz >/dev/null 2>&1; then say "xz (LZMA2) not on PATH -> skipping"; exit 0; fi
  used="xz (LZMA2)"
  comp() { xz -T "$T" -f -- "$1" 2>/dev/null || xz -f -- "$1"; }
elif command -v pigz >/dev/null 2>&1; then
  used="pigz (gzip)"
  comp() { pigz -p "$T" -f -- "$1" 2>/dev/null || pigz -f -- "$1"; }
elif command -v gzip >/dev/null 2>&1; then
  used="gzip"
  comp() { gzip -f -- "$1"; }
else
  say "no gzip/pigz on PATH -> skipping"; exit 0
fi

say "START: compressing files >= ${minmb}MB under $dir with $used ..."
n=0
fail=0
while IFS= read -r -d '' f; do
  if comp "$f"; then n=$((n+1)); else fail=$((fail+1)); say "WARN: could not compress $f"; fi
done < <(find "$dir" -type f \
      ! -name '*.gz'  ! -name '*.bz2'  ! -name '*.xz'  ! -name '*.zst' ! -name '*.zip' \
      ! -name '*.bam' ! -name '*.bai'  ! -name '*.cram' ! -name '*.crai' ! -name '*.tbi' \
      ! -name '*.sra' ! -name '*.sralite' ! -name '*.vdbcache' \
      ! -name '*.sh'  ! -name '*.log'  ! -name '*.out'  ! -name '*.err' ! -name '*.json' ! -name '*.lock' \
      ! -name 'PIPELINE_*.txt' ! -name 'COMPRESSION_*.txt' ! -name 'config.sh' \
      ! -path '*/altanalyze/*' ! -path '*/altanalyze_home/*' ! -path '*/STAR-index/*' \
      ! -name 'SraRunTable*' ! -name 'SraAccList*.txt' ! -name 'sample_list.tsv' ! -name 'bam_list.tsv' \
      ! -name 'sample_groups.tsv' ! -name 'sample_comps.tsv' \
      -size +"${minmb}"M -print0 2>/dev/null)

_report() {
  echo "$1 at $(ts)"
  echo "mode      : $mode ($used)"
  echo "dir       : $dir"
  echo "files     : $n compressed${fail:+, $fail FAILED}"
  echo "threshold : >= ${minmb} MB (already-compressed + BAM/BAI/CRAM skipped)"
}
# Any failure is now MARKED + EMAILED (was silently buried in COMPLETE -- the MDSL all-72-failed case looked
# 'done'). Total failure (0 compressed) writes NO COMPLETE marker so a re-run can retry; partial failure
# still publishes COMPLETE (the compressible files are done) but flags the failures.
if [ "$fail" -gt 0 ]; then
  _report "Compression FAILED ($fail file(s))" > "$PIPELINE_ROOT/PIPELINE_COMPRESS_FAILED.txt"
  notify_error "Compression failed for $fail file(s)" "$(cat "$PIPELINE_ROOT/PIPELINE_COMPRESS_FAILED.txt")" "compress-failed"
  if [ "$n" -eq 0 ]; then
    say "DONE: 0 compressed, $fail FAILED -> compressor broken; NOT writing COMPLETE (re-runnable). See PIPELINE_COMPRESS_FAILED.txt"
    exit 1
  fi
fi
_report "Compression COMPLETE" > "$PIPELINE_ROOT/COMPRESSION_COMPLETE.txt"
say "DONE: compressed $n file(s)${fail:+ ($fail failed)} with $used -> COMPRESSION_COMPLETE.txt"
[ "$fail" -eq 0 ] && notify_update "Compression complete" "$(cat "$PIPELINE_ROOT/COMPRESSION_COMPLETE.txt")"
