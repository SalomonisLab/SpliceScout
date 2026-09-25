#!/usr/bin/env bash
# fetch_missing.sh — targeted recovery. For each study, finds accessions that are
# MISSING (in SraAccList.txt but have neither a .fastq.gz nor an .sra on disk),
# writes a per-study SraAccList_missing.txt, and prefetches+converts ONLY those.
# Never re-downloads runs already on disk and never double-submits (skips studies
# that already have a live re-fetch). Safe to run repeatedly (the watchdog calls it).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib.sh"
set -u
sra_require_bsub   # must run on the LSF submit host (not a login node)
cd "$STUDIES_DIR" || { echo "fetch_missing: STUDIES_DIR not found" >&2; exit 1; }
shopt -s nullglob

LIVE="$(sra_live_names)"
total=0; blocked=0
for S in */; do
  name=$(basename "$S")
  [ -f "$S/SraAccList.txt" ] || continue
  sdir="$STUDIES_DIR/$name"
  # already re-fetching this study? skip.
  sra_has_live "${JOB_TAG}_pf_${name}"  "$LIVE" && continue
  sra_has_live "${JOB_TAG}_cs_${name}"  "$LIVE" && continue

  missing=()
  while read -r acc; do
    acc=$(echo "$acc" | tr -d '\r'); [ -z "$acc" ] && continue
    sra_is_dropped "$acc" && continue                    # already gave up on this accession
    # downloaded but never flattened out of <acc>/ (its convert_study bailed out on a full queue): NOT missing.
    # Flatten it; the watchdog's stranded-.sra pass then converts it (see watchdog.sh step 1).
    for _n in "$sdir/$acc/$acc.sra" "$sdir/$acc/$acc.sralite"; do
      [ -e "$_n" ] && { mv -n "$_n" "$sdir/$acc.sra" 2>/dev/null; rmdir "$sdir/$acc" 2>/dev/null; }
    done
    # delivered? ANCHORED like sra_done_count: an unanchored "$acc"*.fastq.gz let SRR123 hide behind a sibling's
    # SRR1234.fastq.gz, so it was never re-fetched while the completion count never credited it (a permanent gap).
    # A run STAR already aligned (its FASTQ deleted, <acc>.aligned left) is delivered too -- never re-downloaded.
    if ! sra_delivered "$sdir" "$acc" && [ ! -e "$sdir/$acc.sra" ]; then
      # still missing AND this study has no live re-fetch (checked above) = the last download FAILED -> DROP after
      # MAX_FAILS real attempts so an undeliverable accession can't be re-fetched forever. The attempt is counted
      # below, only once the re-fetch is actually QUEUED (a blocked submit on a full queue is not an attempt).
      if [ "$(sra_attempts "$acc")" -ge "${MAX_FAILS:-3}" ]; then
        sra_drop_acc "$acc" "$sdir" download
        echo "  dropped $acc after $(sra_attempts "$acc") failed downloads -> logged to dropped_accessions.txt"
        continue
      fi
      missing+=("$acc")
    fi
  done < "$S/SraAccList.txt"
  [ ${#missing[@]} -eq 0 ] && continue

  printf '%s\n' "${missing[@]}" > "$sdir/SraAccList_missing.txt"
  # FAIL-FAST: if the queue is full the helper returns 124 -> STOP this pass (don't hang on each blocked bsub).
  # The final count line still prints; the next watchdog pass resumes the re-fetch where this left off.
  PF=$(sra_submit_prefetch "$sdir" "SraAccList_missing.txt"); _pfrc=$?
  if [ "$_pfrc" -eq 124 ] || [ -z "$PF" ]; then
    echo "  submit blocked (queue full) -> stopping fetch_missing this pass"; blocked=1; break
  fi
  for acc in "${missing[@]}"; do sra_bump_attempt "$acc" >/dev/null; done   # queued -> now it is an attempt
  CS=$(sra_submit_convert_study   "$sdir" "SraAccList_missing.txt" "$PF")
  echo "$name: re-fetch ${#missing[@]} missing  prefetch=$PF convert=$CS"
  total=$((total+${#missing[@]}))
done
# the LAST line is what the watchdog reads: it must say when a submit blocked (pending work, not a stall)
echo "=== fetch_missing: queued $total accession(s)$([ "$blocked" = 1 ] && echo ' -- submit blocked (queue full)') ==="
