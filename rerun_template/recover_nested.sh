#!/usr/bin/env bash
# =============================================================================
# recover_nested.sh -- LSF job-array body (submitted by rerun_submit.sh): puts back runs that were DOWNLOADED but
# never converted. prefetch writes <study>/<acc>/<acc>.sra; the study's converter flattens it to <study>/<acc>.sra,
# but when it bailed out on a full queue the copy stayed nested. The old watchdog/fetch_missing saw only the flat
# name, counted the run as a failed download, re-prefetched it (prefetch finds it valid and exits 0) and finally
# DROPPED it -- and a later PSI compress-when-done pass gzipped it to <acc>/<acc>.sra.gz (K562: 4,081 such runs).
# (The templates flatten nested downloads and never gzip .sra since 2026-09-22; this recovers OLDER deployments.)
#   bash recover_nested.sh <index file>      element $LSB_JOBINDEX = line N: <study dir><TAB><acc> <acc> ...
# Each listed accession ends up as <study>/<acc>.sra; a source is removed only after its replacement is complete.
# =============================================================================
set -u
IDX="${1:?usage: recover_nested.sh <index file>}"
N="${LSB_JOBINDEX:-${2:-}}"
[ -n "$N" ] || { echo "recover: no LSB_JOBINDEX (run as an LSF array element, or pass the line number)" >&2; exit 1; }
LINE="$(sed -n "${N}p" "$IDX")"
SDIR="${LINE%%$'\t'*}"
ACCS="${LINE#*$'\t'}"
cd "$SDIR" || { echo "recover: cannot cd '$SDIR'" >&2; exit 1; }
shopt -s nullglob
UNZ="gzip -dc"
command -v pigz >/dev/null 2>&1 && UNZ="pigz -dc"
ok=0; moved=0; bad=0; skipped=0; none=0
for acc in $ACCS; do
  if [ -e "$acc.sra" ]; then                         # a flat copy already exists -> the watchdog converts it
    skipped=$((skipped+1)); continue
  fi
  src=""
  for f in "$acc/$acc.sra" "$acc/$acc.sralite" "$acc/$acc.sra.gz" "$acc/$acc.sralite.gz"; do
    if [ -e "$f" ]; then src="$f"; break; fi
  done
  if [ -z "$src" ]; then none=$((none+1)); continue; fi
  case "$src" in
    *.gz)
      if $UNZ "$src" > ".$acc.sra.part" 2>/dev/null && [ -s ".$acc.sra.part" ]; then
        mv -f ".$acc.sra.part" "$acc.sra" && rm -f "$src" && ok=$((ok+1))
      else
        rm -f ".$acc.sra.part"; bad=$((bad+1))
        echo "  $acc: could not decompress $src -- left as is (the watchdog re-downloads the run)" >&2
      fi ;;
    *)                                               # .sralite is converted by content, not by its extension
      mv -n "$src" "$acc.sra" && moved=$((moved+1)) ;;
  esac
  for v in "$acc"/*.vdbcache; do mv -n "$v" "$acc.sra.vdbcache"; done
  rmdir "$acc" 2>/dev/null
done
echo "recover $(basename "$SDIR"): decompressed=$ok moved=$moved already_flat=$skipped not_found=$none failed=$bad"
exit 0                                               # a failed unpack is re-downloaded, never fatal
