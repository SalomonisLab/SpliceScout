#!/usr/bin/env bash
# =============================================================================
# rerun_submit.sh -- TARGETED RE-RUN of one SpliceScout cell line. Built by rerun_deploy.py (see DEVELOPER_GUIDE,
#   "Targeted re-runs"): download -> STAR -> BED for exactly the samples a NEW PSI plan needs, then the new PSI +
#   concordance stages, and (optional) promotion of the results into the cell line's ORIGINAL folders.
#
# Unzip the kit into <run root>/rerun/ and run it ONCE on the LSF submit host (the OnDemand shell):
#     DRY_RUN=1 bash <run root>/rerun/rerun_submit.sh      # shows what it would do, changes nothing
#     bash <run root>/rerun/rerun_submit.sh
#
#  1. REFUSES while any job of this cell line is live, or if a required file is missing.
#  2. PLAN STATUS, from the live files: every sample of the new plan (rerun_plan_runs.tsv) is either BED-ready,
#     BAM-ready (BED stage converts it) or NEEDS DELIVERY -- its runs are then re-delivered: FASTQ already on disk,
#     .sra on disk, downloaded-but-nested (<acc>/<acc>.sra[.gz], unpacked by an LSF job array), or re-downloaded.
#  3. DOWNLOAD STAGE: installs the current scripts; un-drops the runs to deliver (fresh MAX_FAILS attempts) and clears
#     their <acc>.aligned markers; marks every OTHER unconverted run as not needed (runs aligned before STAR left
#     .aligned markers had their FASTQs deleted -- without this the watchdog would re-download them); resets its state.
#  4. STAR + BED: moves their finished-run markers, lists, attempt counters and watchdog state into the backup, so
#     the auto-chain re-runs them on the NEW samples only (finished BAMs / BEDs are skipped); installs re-run-safe
#     list builders and the STAR -> BED hand-off; keeps tools and inputs (CLEANUP_TOOLS_WHEN_DONE=0); restores tool
#     files an older compress pass gzipped (the exon reference, STAR's run table).
#  5. Unpacks the new PSI + concordance bundles into their NEW folders and arms their launchers. They wait for the
#     BED stage, then the PSI. Nothing already published is overwritten; every moved file is in rerun_backup_<time>/.
#     With FINAL_PSI_DIR / FINAL_CONC_DIR set, promote_launch.sh then moves the finished results into those folders
#     (their previous contents are kept as <folder>.old_<date>).
# Most of the state it resets is handled by the stage templates themselves since 2026-09-25 (drop markers count only
# listed items, a re-armed watchdog/launcher starts a fresh wait window, aligned runs are never re-downloaded); the
# resets stay because a cell line deployed with OLDER scripts still carries the old state.
# Chain: unpack jobs -> download watchdog -> (finalize) STAR -> (finalize) BED -> PSI launcher -> concordance launcher.
# =============================================================================
set -u
shopt -s nullglob
KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY="${DRY_RUN:-0}"
die() { echo "ERROR: $*" >&2; exit 1; }
say() { echo ">> $*"; }
[ -f "$KIT/rerun.env" ] || die "rerun.env missing next to $0 -- unzip the whole kit"
# shellcheck disable=SC1091
source "$KIT/rerun.env"                       # CELL_LINE RUN_ROOT PSI_DIR CONC_DIR PSI_TAG CONC_TAG FINAL_*_DIR
ROOT="$RUN_ROOT"
for f in rerun_plan_runs.tsv recover_nested.sh promote_launch.sh psi_bundle.zip concordance_bundle.zip bed_launch.sh \
         download/watchdog.sh download/fetch_missing.sh download/prefetch_job.sh download/fasterqdump_job.sh \
         download/lib.sh star/build_sample_list.sh star/make_sample_list.py bed/build_bam_list.sh; do
  [ -f "$KIT/$f" ] || die "kit file missing: $f"
done
[ -f "$ROOT/config.sh" ] || die "no download-stage config.sh in $ROOT"
command -v bsub >/dev/null 2>&1 || die "bsub not found -- run this on the LSF submit host (the OnDemand shell)"

# ---- configs (read in subshells: each stage's config.sh derives its own paths) ------------------------------------
cfgval() { ( set +u; cd "$(dirname "$1")" 2>/dev/null; source "$1" >/dev/null 2>&1; eval "printf '%s' \"\${$2:-}\"" ); }
DL_CFG="$ROOT/config.sh"
STUDIES_DIR="$(cfgval "$DL_CFG" STUDIES_DIR)"; DL_TAG="$(cfgval "$DL_CFG" JOB_TAG)"
DL_WD_MIN="$(cfgval "$DL_CFG" WATCHDOG_INTERVAL_MIN)"; LSF_QUEUE="$(cfgval "$DL_CFG" LSF_QUEUE)"
STAR_DIR="$ROOT/star"; STAR_CFG="$STAR_DIR/config.sh"
[ -f "$STAR_CFG" ] || die "no STAR stage at $STAR_DIR"
BAM_OUT="$(cfgval "$STAR_CFG" BAM_OUT)"; STAR_TAG="$(cfgval "$STAR_CFG" JOB_TAG)"; RUNTABLE="$(cfgval "$STAR_CFG" RUNTABLE)"
BED_SCRIPTS=""
for d in "$BAM_OUT/bed" "$ROOT/bed"; do [ -f "$d/run_bed_pipeline.sh" ] && { BED_SCRIPTS="$d"; break; }; done
[ -n "$BED_SCRIPTS" ] || die "no BED stage scripts (run_bed_pipeline.sh) under $BAM_OUT/bed or $ROOT/bed"
BED_CFG="$BED_SCRIPTS/config.sh"
BED_ROOT="$(cfgval "$BED_CFG" PIPELINE_ROOT)"; BED_OUT="$(cfgval "$BED_CFG" BED_OUT_DIR)"
BED_TAG="$(cfgval "$BED_CFG" JOB_TAG)"; EXON_REF="$(cfgval "$BED_CFG" EXON_REF)"
# older BED configs leave these to derived defaults -- the same defaults the current template derives
[ -n "$BED_ROOT" ] || BED_ROOT="$BAM_OUT/bed"
[ -n "$BED_OUT" ] || BED_OUT="$(dirname "$BAM_OUT")/STAR_beds"
if [ -z "$EXON_REF" ]; then
  _sp="$(cfgval "$BED_CFG" SPECIES)"; _sp="${_sp:-Hs}"; _ad="$(cfgval "$BED_CFG" ALTANALYZE_DIR)"
  EXON_REF="${_ad:-$BED_SCRIPTS/altanalyze}/refs/$_sp/${_sp}_Ensembl_exon.txt"
fi
[ -d "$BED_OUT" ] || die "BED output folder not found: $BED_OUT"
for v in STUDIES_DIR DL_TAG BAM_OUT STAR_TAG BED_ROOT BED_OUT BED_TAG EXON_REF; do
  [ -n "${!v}" ] || die "could not read $v from the stage configs"
done
QOPT=(); [ -n "$LSF_QUEUE" ] && QOPT=(-q "$LSF_QUEUE")
echo "=============================================================================="
echo " SpliceScout targeted re-run: $CELL_LINE    ($( [ "$DRY" = "1" ] && echo DRY RUN -- nothing is changed || echo LIVE ))"
echo "   run root $ROOT   | STAR $BAM_OUT ($STAR_TAG) | BED $BED_ROOT ($BED_TAG, scripts $BED_SCRIPTS)"
echo "   new PSI  $PSI_DIR ($PSI_TAG) | new concordance $CONC_DIR ($CONC_TAG)"
[ -n "${FINAL_CONC_DIR:-}" ] && \
  echo "   when finished, both move into $FINAL_PSI_DIR + $FINAL_CONC_DIR (previous contents kept as .old_<date>)"
echo "=============================================================================="

# ---- 1. nothing of this cell line may be running --------------------------------------------------------------------
LIVE="$(timeout 60 bjobs -noheader -o job_name 2>/dev/null)"; rc=$?
if [ "$rc" -ne 0 ] && [ -n "$(printf '%s' "$LIVE" | tr -d '[:space:]')" ]; then die "bjobs failed (rc=$rc) -- retry in a minute"; fi
BUSY="$(printf '%s\n' "$LIVE" | grep -E "^(${DL_TAG}_|${STAR_TAG}_|${BED_TAG}_|${PSI_TAG}_|${CONC_TAG})" | head -5)"
[ -z "$BUSY" ] || die "jobs of this cell line are still live (bjobs -w | grep ${DL_TAG}_), e.g.: $(echo $BUSY)"
if [ -f "$PSI_DIR/PIPELINE_COMPLETE.txt" ] || [ -f "$CONC_DIR/PIPELINE_COMPLETE.txt" ]; then
  die "$PSI_DIR or $CONC_DIR already holds a finished run -- not overwriting it"
fi

# ---- 2. plan status ----------------------------------------------------------------------------------------------------
declare -A LSTATE=() WANT_RUN=()
n_lab=0; n_bed=0; n_bam=0; n_want=0; n_runs=0
while IFS=$'\t' read -r lab run study; do
  case "$lab" in ""|\#*) continue ;; esac
  n_runs=$((n_runs+1))
  st="${LSTATE[$lab]:-}"
  if [ -z "$st" ]; then
    n_lab=$((n_lab+1))
    if [ -e "$BED_OUT/${lab}__junction.bed.gz" ] || [ -s "$BED_OUT/${lab}__junction.bed" ]; then st=bed; n_bed=$((n_bed+1))
    elif [ -s "$BAM_OUT/$lab.bam" ]; then st=bam; n_bam=$((n_bam+1))
    else st=want; n_want=$((n_want+1)); fi
    LSTATE["$lab"]="$st"
  fi
  [ "$st" = "want" ] && WANT_RUN["$run"]="$study"
done < "$KIT/rerun_plan_runs.tsv"
has_fastq() { compgen -G "$1/$2.fastq.gz" >/dev/null 2>&1 || compgen -G "$1/${2}_[0-9].fastq.gz" >/dev/null 2>&1; }
declare -A UNPACK=()
n_fq=0; n_sra=0; n_nested=0; n_fetch=0
if [ "${#WANT_RUN[@]}" -gt 0 ]; then
  for run in "${!WANT_RUN[@]}"; do
    sdir="$STUDIES_DIR/${WANT_RUN[$run]}"
    if has_fastq "$sdir" "$run"; then n_fq=$((n_fq+1))
    elif [ -e "$sdir/$run.sra" ]; then n_sra=$((n_sra+1))
    elif [ -e "$sdir/$run/$run.sra" ] || [ -e "$sdir/$run/$run.sralite" ] || [ -e "$sdir/$run/$run.sra.gz" ] \
         || [ -e "$sdir/$run/$run.sralite.gz" ]; then
      n_nested=$((n_nested+1)); UNPACK["$sdir"]="${UNPACK[$sdir]:-}$run "
    else n_fetch=$((n_fetch+1)); fi
  done
fi
echo "PLAN: $n_lab samples ($n_runs runs) in the new plan"
echo "   BED-ready: $n_bed | BAM awaiting its BEDs: $n_bam | NEED delivery + STAR + BED: $n_want samples (${#WANT_RUN[@]} runs)"
echo "   runs to deliver: FASTQ on disk $n_fq | .sra on disk $n_sra | downloaded-but-nested $n_nested (${#UNPACK[@]} studies) | to download $n_fetch"
free_tb="$(df -Pk "$ROOT" | awk 'NR==2{printf "%.1f", $4/1073741824}')"
echo "   free space on $ROOT: ${free_tb} TB"

# tool files an earlier compress-when-done pass gzipped (the BED stage needs the plain exon reference; STAR its run table)
RESTORE=()
[ ! -s "$EXON_REF" ] && [ -s "$EXON_REF.gz" ] && RESTORE+=("$EXON_REF")
[ -n "$RUNTABLE" ] && [ ! -s "$RUNTABLE" ] && [ -s "$RUNTABLE.gz" ] && RESTORE+=("$RUNTABLE")
[ -s "$EXON_REF" ] || [ -s "$EXON_REF.gz" ] || die "BED exon reference missing: $EXON_REF(.gz)"
echo "   tool files to restore from .gz: ${#RESTORE[@]} ${RESTORE[*]+${RESTORE[*]}}"
if [ "$DRY" = "1" ]; then echo; echo "DRY RUN -- nothing changed. Run again without DRY_RUN=1 to launch."; exit 0; fi

# ================================= from here on: LIVE ===================================================================
ts="$(date +%Y%m%d-%H%M%S)"
BK="$ROOT/rerun_backup_$ts"
mkdir -p "$BK"/{download,star,bed,scripts} "$BK/download/aligned_markers" || die "cannot create $BK"
bk() { local dest="$1"; shift; local f; for f in "$@"; do [ -e "$f" ] && mv -f "$f" "$BK/$dest/"; done; return 0; }
keep_tools() {                                # CLEANUP_TOOLS_WHEN_DONE=0 in a stage config (set or append)
  if grep -q '^CLEANUP_TOOLS_WHEN_DONE=' "$1"; then
    sed -i "s/^CLEANUP_TOOLS_WHEN_DONE=.*/CLEANUP_TOOLS_WHEN_DONE=0   # rerun $ts: keep inputs + tools/" "$1"
  else
    printf '\nCLEANUP_TOOLS_WHEN_DONE=0   # rerun %s: keep inputs + tools\n' "$ts" >> "$1"
  fi
}
for f in ${RESTORE[@]+"${RESTORE[@]}"}; do
  gzip -dc "$f.gz" > "$f.part" && mv -f "$f.part" "$f" && say "restored $f (kept $f.gz)"
done

# ---- 3. download stage ---------------------------------------------------------------------------------------------------
say "download stage: installing the fixed scripts (old ones -> $BK/scripts/)"
for s in watchdog.sh fetch_missing.sh prefetch_job.sh fasterqdump_job.sh lib.sh; do
  [ -f "$ROOT/$s" ] && cp -p "$ROOT/$s" "$BK/scripts/download_$s"
  cp -f "$KIT/download/$s" "$ROOT/$s" && chmod +x "$ROOT/$s"
done
ATT="$ROOT/.attempts"; DROPPED="$ROOT/dropped_accessions.txt"
mkdir -p "$ATT"
[ -d "$ATT" ] && tar -C "$ROOT" -cf "$BK/download/attempts.tar" .attempts 2>/dev/null
[ -f "$DROPPED" ] && cp -p "$DROPPED" "$BK/download/"
# every run to deliver must be listed in its study's SraAccList.txt (a purged by_study gets its folders back)
declare -A LISTED=()
if [ "${#WANT_RUN[@]}" -gt 0 ]; then
  for run in "${!WANT_RUN[@]}"; do
    sdir="$STUDIES_DIR/${WANT_RUN[$run]}"
    mkdir -p "$sdir"
    if ! { [ -f "$sdir/SraAccList.txt" ] && tr -d '\r' < "$sdir/SraAccList.txt" | grep -qxF "$run"; }; then
      echo "$run" >> "$sdir/SraAccList.txt"
    fi
  done
fi
n_undrop=0; n_mark=0; n_unlisted=0
now="$(date '+%Y-%m-%d %H:%M:%S')"
for L in "$STUDIES_DIR"/*/SraAccList.txt; do
  sdir="$(dirname "$L")"; study="$(basename "$sdir")"
  while read -r acc; do
    acc="${acc%$'\r'}"; [ -n "$acc" ] || continue
    LISTED["$acc"]=1
    if [ -n "${WANT_RUN[$acc]:-}" ]; then
      [ -e "$ATT/$acc.dropped" ] && n_undrop=$((n_undrop+1))
      rm -f "$ATT/$acc.dropped" "$ATT/$acc.n"
      [ -e "$sdir/$acc.aligned" ] && mv -f "$sdir/$acc.aligned" "$BK/download/aligned_markers/" 2>/dev/null
    elif has_fastq "$sdir" "$acc" || [ -e "$sdir/$acc.aligned" ]; then
      rm -f "$ATT/$acc.dropped"              # counted as delivered -- a drop marker too would count it twice
    elif [ ! -e "$ATT/$acc.dropped" ]; then
      : > "$ATT/$acc.dropped"
      printf '%s\t%s\t%s\t%s\t%s\n' "$acc" "$study" "not-needed" "rerun $ts: already aligned or outside the new plan" \
        "$now" >> "$DROPPED"
      n_mark=$((n_mark+1))
    fi
  done < "$L"
done
mkdir -p "$BK/download/attempts_unlisted"
for f in "$ATT"/*.dropped; do                 # drops of runs no list names any more would inflate the done+dropped gate
  a="$(basename "$f" .dropped)"
  [ -n "${LISTED[$a]:-}" ] || { mv -f "$f" "$BK/download/attempts_unlisted/"; n_unlisted=$((n_unlisted+1)); }
done
if [ -f "$DROPPED" ]; then                    # the log keeps only runs that are still dropped
  awk -F'\t' -v d="$ATT" '{ f = d "/" $1 ".dropped"; if ((getline x < f) >= 0) { close(f); print } }' "$DROPPED" \
    > "$DROPPED.tmp" && mv -f "$DROPPED.tmp" "$DROPPED"
fi
bk download "$ROOT/PIPELINE_STALLED.txt" "$ROOT/PIPELINE_COMPLETE.txt" "$ROOT/PIPELINE_ORPHANED.txt" "$ROOT"/.watchdog.state*
rmdir "$ROOT/.finalized.lock" 2>/dev/null
echo "   un-dropped $n_undrop run(s) to deliver | marked $n_mark other unconverted run(s) not needed | moved $n_unlisted stale drop marker(s)"

# ---- 4. STAR + BED stages -------------------------------------------------------------------------------------------------
say "STAR stage ($BAM_OUT): finished-run state -> $BK/star/"
bk star "$BAM_OUT"/PIPELINE_*.txt "$BAM_OUT/UPSTREAM_DOWNLOAD_STALLED.txt" "$BAM_OUT"/sample_list.tsv* \
        "$BAM_OUT/star_dropped.txt" "$BAM_OUT/.attempts" "$BAM_OUT"/.watchdog.state* "$STAR_DIR/PIPELINE_LAUNCH_TIMEOUT.txt"
rmdir "$BAM_OUT/.finalized.lock" 2>/dev/null
rm -f "$STAR_DIR/.launch_first_seen"
for s in build_sample_list.sh make_sample_list.py; do    # re-run-safe list + <run>_3/_4 read files not taken as samples
  cp -p "$STAR_DIR/$s" "$BK/scripts/star_$s" 2>/dev/null
  cp -f "$KIT/star/$s" "$STAR_DIR/$s" && chmod +x "$STAR_DIR/$s"
done
cp -p "$STAR_CFG" "$BK/scripts/star_config.sh"
keep_tools "$STAR_CFG"
echo "targeted re-run started $ts by rerun_submit.sh ($CELL_LINE): only samples without a BAM are aligned" \
  > "$BAM_OUT/RERUN_TARGETED.txt"

say "BED stage ($BED_ROOT): finished-run state -> $BK/bed/"
bk bed "$BED_ROOT"/PIPELINE_*.txt "$BED_ROOT/UPSTREAM_STAR_STALLED.txt" "$BED_ROOT/bam_list.tsv" "$BED_ROOT/bed_dropped.txt" \
       "$BED_ROOT/.attempts" "$BED_ROOT"/.watchdog.state* "$BAM_OUT/bed/PIPELINE_LAUNCH_TIMEOUT.txt"
rmdir "$BED_ROOT/.finalized.lock" 2>/dev/null
rm -f "$BED_SCRIPTS/.launch_first_seen" "$BAM_OUT/bed/.launch_first_seen"
cp -p "$BED_SCRIPTS/build_bam_list.sh" "$BK/scripts/bed_build_bam_list.sh" 2>/dev/null
cp -f "$KIT/bed/build_bam_list.sh" "$BED_SCRIPTS/build_bam_list.sh" && chmod +x "$BED_SCRIPTS/build_bam_list.sh"
cp -p "$BED_CFG" "$BK/scripts/bed_config.sh"
keep_tools "$BED_CFG"
[ -f "$BED_ROOT/bed_launch.sh" ] && cp -p "$BED_ROOT/bed_launch.sh" "$BK/scripts/bed_launch.sh"
cp -f "$KIT/bed_launch.sh" "$BED_ROOT/bed_launch.sh" && chmod +x "$BED_ROOT/bed_launch.sh"   # STAR's finalize kicks it
echo "targeted re-run started $ts by rerun_submit.sh ($CELL_LINE): only BAMs without BEDs are converted" \
  > "$BED_ROOT/RERUN_TARGETED.txt"

# ---- download: unpack job array, then the watchdog ------------------------------------------------------------------------
DEP=()
if [ "${#UNPACK[@]}" -gt 0 ]; then
  IDX="$KIT/unpack_index_$ts.txt"; : > "$IDX"
  for sdir in "${!UNPACK[@]}"; do printf '%s\t%s\n' "$sdir" "${UNPACK[$sdir]}" >> "$IDX"; done
  n_idx="${#UNPACK[@]}"
  mkdir -p "$KIT/logs"
  if bsub -L /bin/bash -n 1 -M 4000 -W 24:00 -J "${DL_TAG}_recover[1-${n_idx}]%40" ${QOPT[@]+"${QOPT[@]}"} \
       -o "$KIT/logs/recover_%I.out" -e "$KIT/logs/recover_%I.err" \
       bash "$KIT/recover_nested.sh" "$IDX"; then
    DEP=(-w "ended(${DL_TAG}_recover)")
    say "unpack: job array ${DL_TAG}_recover[1-${n_idx}] ($n_nested runs, up to 40 at once; logs $KIT/logs/)"
  else
    say "WARNING: the unpack array did not submit -- the watchdog starts now; the nested runs are re-downloaded instead"
  fi
fi
if bsub -L /bin/bash -n 1 -M 1000 -W "$(( ${DL_WD_MIN:-30} - 5 ))" -J "${DL_TAG}_watchdog" ${DEP[@]+"${DEP[@]}"} \
     ${QOPT[@]+"${QOPT[@]}"} -o "$ROOT/watchdog.out" -e "$ROOT/watchdog.err" "$ROOT/watchdog.sh"; then
  say "download watchdog armed${DEP[*]+ (starts when the unpack array has ended)}: converts, re-fetches, then kicks STAR"
else
  die "the download watchdog did not submit -- arm it by hand: bsub -L /bin/bash -n 1 -M 1000 -W 25 -J ${DL_TAG}_watchdog $ROOT/watchdog.sh"
fi

# ---- 5. new PSI + concordance --------------------------------------------------------------------------------------------
say "PSI -> $PSI_DIR   concordance -> $CONC_DIR"
mkdir -p "$PSI_DIR" "$CONC_DIR" || die "cannot create $PSI_DIR / $CONC_DIR"
unzip -o -q "$KIT/psi_bundle.zip" -d "$PSI_DIR" && unzip -o -q "$KIT/concordance_bundle.zip" -d "$CONC_DIR" \
  || die "could not unzip the PSI / concordance bundles"
chmod +x "$PSI_DIR"/*.sh "$CONC_DIR"/*.sh
bash "$PSI_DIR/setup.sh" > "$PSI_DIR/setup.out" 2>&1 || true
bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J "${PSI_TAG}_launch" ${QOPT[@]+"${QOPT[@]}"} \
     -o "$PSI_DIR/launch.out" -e "$PSI_DIR/launch.err" "$PSI_DIR/psi_launch.sh" >/dev/null \
  && say "PSI launcher armed (waits for $BED_ROOT/PIPELINE_COMPLETE|STALLED)"
bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J "${CONC_TAG}_launch" ${QOPT[@]+"${QOPT[@]}"} \
     -o "$CONC_DIR/launch.out" -e "$CONC_DIR/launch.err" "$CONC_DIR/concordance_launch.sh" >/dev/null \
  && say "concordance launcher armed (waits for $PSI_DIR/PIPELINE_COMPLETE)"
if [ -n "${FINAL_CONC_DIR:-}" ]; then
  chmod +x "$KIT/promote_launch.sh"; mkdir -p "$KIT/logs"
  bsub -L /bin/bash -n 1 -M 500 -W 60 -J "${CONC_TAG}_promote" ${QOPT[@]+"${QOPT[@]}"} \
       -o "$KIT/logs/promote.out" -e "$KIT/logs/promote.err" "$KIT/promote_launch.sh" >/dev/null \
    && say "promote step armed: when the concordance finishes, results move into $FINAL_PSI_DIR + $FINAL_CONC_DIR"
fi

cat <<EOF

==============================================================================
 LAUNCHED. Backups of everything moved: $BK
 Watch:   bjobs -w | grep -E "${DL_TAG}_|${PSI_TAG}|${CONC_TAG}"
          tail -3 $ROOT/watchdog.log                 (download: converted / dropped / live)
          tail -3 $BAM_OUT/watchdog.log         (STAR, once the download finalizes)
          tail -3 $BED_ROOT/watchdog.log     (BED, once STAR finalizes)
          cat $PSI_DIR/launch.out ; tail -3 $PSI_DIR/watchdog.log
 Done:    $CONC_DIR/PIPELINE_COMPLETE.txt  (results in $CONC_DIR/results/)
          ${FINAL_CONC_DIR:+then moved into $FINAL_CONC_DIR + $FINAL_PSI_DIR -- see $KIT/promote.log}
==============================================================================
EOF
