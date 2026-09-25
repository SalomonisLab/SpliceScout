#!/usr/bin/env bash
# prefetch_job.sh — LSF job body. Downloads every accession in a study's list.
# Args: $1 = study directory   $2 = list file (relative to the study dir)
# Submitted by sra_submit_prefetch(). Not run by hand.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u
SDIR="$1"
LIST="${2:-SraAccList.txt}"
cd "$SDIR" || { echo "prefetch_job: cannot cd $SDIR" >&2; exit 1; }
sra_load_modules
# Each accession lands in <SDIR>/<ACC>/<ACC>.sra (or .sralite). Non-zero exit on a
# single failed accession is fine: the convert step runs on whatever downloaded
# (ended() dependency) and the watchdog re-fetches anything still missing.
# The timeout bounds EACH accession, not the whole list: a hung connection then costs one run (re-fetched later)
# instead of the study. One cap over the whole list killed every study with more than ~2 h of transfer partway
# through on EVERY attempt -- K562 GSE127062 (1,522 runs) got 76 through per 2-h attempt until MAX_FAILS dropped
# the rest. prefetch skips a run it already holds, so re-listing those costs nothing.
rc=0
while read -r acc; do
  acc=$(echo "$acc" | tr -d '\r'); [ -z "$acc" ] && continue
  # --max-size: prefetch refuses a run above its 20G default, so every deep run failed until it was DROPPED
  timeout "${PREFETCH_TIMEOUT_SEC:-7200}" prefetch --max-size "${PREFETCH_MAX_SIZE:-500G}" -O "$SDIR" "$acc" || rc=1
done < "$LIST"
exit "$rc"
