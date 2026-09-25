#!/usr/bin/env bash
# =============================================================================
# rescore_submit.sh -- concordance-only RE-SCORE of one SpliceScout cell line over its EXISTING, finished PSI stage
# (kit built by `rerun_deploy.py --mode rescore`). Use it when only the concordance side changed (a new atlas, the
# reagent filter, the scorer) -- no download / STAR / BED / PSI work.
#
# Unzip the kit into <run root>/rescore/ and run it ONCE on the LSF submit host (the OnDemand shell):
#     DRY_RUN=1 bash <run root>/rescore/rescore_submit.sh      # shows what it would do, changes nothing
#     bash <run root>/rescore/rescore_submit.sh
#
#  * REFUSES unless the PSI it reads (PSI_DIR) has finished, or while jobs of the new concordance are live, or if the
#    new folder (CONC_DIR) already holds a finished run.
#  * Unpacks concordance_bundle.zip into CONC_DIR (a NEW folder -- the current results stay untouched) and arms its
#    launcher, which starts at once (the PSI is already finished).
#  * With FINAL_CONC_DIR set, also arms promote_launch.sh: when the re-score finishes, its results move into
#    FINAL_CONC_DIR (the original concordance folder); the previous contents are kept as <folder>.old_<date>.
# =============================================================================
set -u
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY="${DRY_RUN:-0}"
die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo ">> $*"; }
[ -f "$KIT/rerun.env" ] || die "rerun.env missing next to $0 -- unzip the whole kit"
# shellcheck disable=SC1091
source "$KIT/rerun.env"                       # CELL_LINE PSI_DIR CONC_DIR CONC_TAG FINAL_CONC_DIR
for f in concordance_bundle.zip promote_launch.sh; do [ -f "$KIT/$f" ] || die "kit file missing: $f"; done
command -v bsub >/dev/null 2>&1 || die "bsub not found -- run this on the LSF submit host (the OnDemand shell)"
QOPT=(); [ -n "${LSF_QUEUE:-}" ] && QOPT=(-q "$LSF_QUEUE")
echo "=============================================================================="
echo " SpliceScout concordance re-score: $CELL_LINE    ($( [ "$DRY" = "1" ] && echo DRY RUN -- nothing is changed || echo LIVE ))"
echo "   reads the PSI in   $PSI_DIR"
echo "   new concordance    $CONC_DIR ($CONC_TAG)"
[ -n "${FINAL_CONC_DIR:-}" ] && echo "   when finished, it moves into $FINAL_CONC_DIR (previous contents kept as .old_<date>)"
echo "=============================================================================="
[ -f "$PSI_DIR/PIPELINE_COMPLETE.txt" ] || [ -f "$PSI_DIR/PIPELINE_STALLED.txt" ] \
  || die "the PSI in $PSI_DIR has not finished (no PIPELINE_COMPLETE.txt) -- nothing to re-score yet"
[ -f "$CONC_DIR/PIPELINE_COMPLETE.txt" ] && die "$CONC_DIR already holds a finished run -- not overwriting it"
LIVE="$(timeout 60 bjobs -noheader -o job_name 2>/dev/null)"; rc=$?
if [ "$rc" -ne 0 ] && [ -n "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then die "bjobs failed (rc=$rc) -- retry in a minute"; fi
BUSY="$(printf '%s\n' "$LIVE" | grep -E "^${CONC_TAG}" | head -3)"
[ -z "$BUSY" ] || die "jobs of $CONC_TAG are still live, e.g.: $(echo $BUSY)"
if [ "$DRY" = "1" ]; then echo; echo "DRY RUN -- nothing changed. Run again without DRY_RUN=1 to launch."; exit 0; fi

mkdir -p "$CONC_DIR" || die "cannot create $CONC_DIR"
unzip -o -q "$KIT/concordance_bundle.zip" -d "$CONC_DIR" || die "could not unzip the concordance bundle"
chmod +x "$CONC_DIR"/*.sh
bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J "${CONC_TAG}_launch" ${QOPT[@]+"${QOPT[@]}"} \
     -o "$CONC_DIR/launch.out" -e "$CONC_DIR/launch.err" "$CONC_DIR/concordance_launch.sh" >/dev/null \
  && say "concordance launcher armed -> $CONC_DIR" || die "the concordance launcher did not submit"
if [ -n "${FINAL_CONC_DIR:-}" ]; then
  chmod +x "$KIT/promote_launch.sh"; mkdir -p "$KIT/logs"
  bsub -L /bin/bash -n 1 -M 500 -W 60 -J "${CONC_TAG}_promote" ${QOPT[@]+"${QOPT[@]}"} \
       -o "$KIT/logs/promote.out" -e "$KIT/logs/promote.err" "$KIT/promote_launch.sh" >/dev/null \
    && say "promote step armed: when the re-score finishes, its results move into $FINAL_CONC_DIR"
fi
cat <<EOF

==============================================================================
 LAUNCHED.  Watch:  bjobs -w | grep ${CONC_TAG} ;  tail -3 $CONC_DIR/watchdog.log
 Done:      $CONC_DIR/PIPELINE_COMPLETE.txt  (results in $CONC_DIR/results/)
            ${FINAL_CONC_DIR:+then moved into $FINAL_CONC_DIR -- see $KIT/promote.log}
==============================================================================
EOF
