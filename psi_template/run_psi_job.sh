#!/usr/bin/env bash
# =============================================================================
# run_psi_job.sh -- the SINGLE AltAnalyze splicing (PSI) job (LSF job body).
# Runs ONE AltAnalyze pass over the whole BED dir -> per-sample PSI table, plus a
# differential (dPSI) comparison when groups.txt/comps.txt exist. Sources config.sh.
#   * idempotent: skips if the PSI table is already present (psi_done)
#   * verifies the PSI table landed (else exits non-zero so the watchdog resubmits)
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_psi.sh"
set -u; shopt -s nullglob

psi_load_modules
command -v python >/dev/null 2>&1 || { echo "[psi] python (2.7) not on PATH" >&2; exit 1; }
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$PSI_OUT" || { echo "[psi] cannot mkdir state/output dirs" >&2; exit 1; }

AA="$ALTANALYZE_HOME/AltAnalyze.py"
[ -s "$AA" ] || { echo "[psi] AltAnalyze.py not found at $AA (deployer should have resolved/uploaded it)" >&2; exit 1; }

# CURATED bedDir: AltAnalyze merges files by sample via the `__<suffix>.bed` double-underscore convention --
# RNASeq.py importBEDFile strips BOTH `__junction.bed` AND `__intronJunction.bed` to the SAME `<sample>.bed`,
# so the two beds for one sample become ONE column, NOT duplicates. We must include BOTH:
#   *__junction.bed        -- exon-exon junctions
#   *__intronJunction.bed  -- intron-retention junctions (e.g. ENSG..:I13.1-E14.1). REQUIRED: omitting it makes
#                             EVERY intron-retention event silently vanish from the PSI table (the file the
#                             annotated `I<n>.<m>` reads live in -- they are NOT in __junction.bed).
# We still EXCLUDE *__exon.bed: an 8-sample A549 smoke left junction PSI byte-identical with/without it
# (89616/89616 cells) while ~doubling import -- it only adds exon-coverage events, not these IR junctions.
# Symlink into a private dir so the quarantine preflight moves SYMLINKS, never the real BED under STAR_beds.
# GZIPPED inputs: this stage's own COMPRESS_WHEN_DONE gzips the whole project tree after a run, BEDs included, so
# a re-run found no *__junction.bed at all (A549: 6,270 of 6,363 were .bed.gz; the intronJunction symlinks of a
# filtered set point at originals that became .bed.gz). A gzipped BED is DECOMPRESSED into a private plain copy
# here (the original is never touched; a copy from an earlier attempt is reused).
# PSI_GROUPED_BEDS_ONLY=1 (default): only samples listed in the shipped sample_groups.tsv enter the bedDir --
# AltAnalyze's dPSI comparisons use nothing else, and a 6,000-library bedDir costs days and hundreds of GB.
JXN_DIR="$PIPELINE_ROOT/junction_beds"
mkdir -p "$JXN_DIR" || { echo "[psi] cannot mkdir $JXN_DIR" >&2; exit 1; }
find "$JXN_DIR" -maxdepth 1 -name '*.bed' -type l -delete 2>/dev/null || true   # refresh stale links
declare -A _want=()
if [ "${PSI_GROUPED_BEDS_ONLY:-1}" = "1" ] && [ -s "${SAMPLE_GROUPS:-}" ]; then
  while IFS=$'\t' read -r _bs _rest; do [ -n "$_bs" ] && _want["$_bs"]=1; done < "$SAMPLE_GROUPS"
fi
_psi_wanted(){ [ "${#_want[@]}" -eq 0 ] || [ -n "${_want[$1]:-}" ]; }
# _psi_link_bed <input path without .gz> <dest name>: symlink a readable plain BED, else decompress its .gz (also
# the .gz of a dangling symlink's target) into a private plain copy. Sets _PSI_GZ=1 when it decompressed; returns 1
# if neither form exists.
_psi_link_bed(){
  local src="$1" dest="$JXN_DIR/$2" gz=""
  _PSI_GZ=0
  if [ -e "$src" ]; then ln -sf "$src" "$dest"; return $?; fi
  if [ -e "$src.gz" ]; then gz="$src.gz"
  elif [ -L "$src" ] && [ -e "$(readlink -f "$src" 2>/dev/null).gz" ]; then gz="$(readlink -f "$src").gz"
  else return 1; fi
  _PSI_GZ=1
  [ -s "$dest" ] && [ ! -L "$dest" ] && return 0                     # decompressed by an earlier attempt
  gzip -dc "$gz" > "$dest.part" 2>/dev/null && mv -f "$dest.part" "$dest" || { rm -f "$dest.part"; return 1; }
}
_nj=0; _ni=0; _nz=0
declare -A _seen=()
for _f in "$BED_INPUT_DIR"/*__junction.bed "$BED_INPUT_DIR"/*__junction.bed.gz \
          "$BED_INPUT_DIR"/*__intronJunction.bed "$BED_INPUT_DIR"/*__intronJunction.bed.gz; do
  _b="$(basename "${_f%.gz}")"; _s="${_b%%__*}"
  [ -n "${_seen[$_b]:-}" ] && continue
  _seen["$_b"]=1
  _psi_wanted "$_s" || continue
  _psi_link_bed "$BED_INPUT_DIR/$_b" "$_b" || continue
  [ "$_PSI_GZ" = "1" ] && _nz=$((_nz+1))
  case "$_b" in *__junction.bed) _nj=$((_nj+1)) ;; *) _ni=$((_ni+1)) ;; esac   # intronJunction: AA merges it
done                                                                           # into the same sample column
if [ "${#_want[@]}" -gt 0 ]; then                  # a private copy left by an earlier plan that no longer lists it
  for _f in "$JXN_DIR"/*.bed; do _b="$(basename "$_f")"; _psi_wanted "${_b%%__*}" || rm -f "$_f"; done
fi
[ "$_nj" -gt 0 ] || { echo "[psi] no *__junction.bed(.gz) under $BED_INPUT_DIR" >&2; exit 1; }
_scope=""
[ "${#_want[@]}" -gt 0 ] && _scope="; grouped samples only (${#_want[@]} in sample_groups.tsv)"
echo "[psi] bedDir: $JXN_DIR ($_nj junction + $_ni intronJunction BEDs, $_nz decompressed from .gz; exon excluded$_scope)"
BED_INPUT_DIR="$JXN_DIR"; export BED_INPUT_DIR PSI_BEDDIR="$JXN_DIR"   # all downstream + build_groups use this
# run_psi_pipeline.sh built groups.txt against the RAW input dir, where gzipped BEDs are invisible -> rebuild it on
# the curated bedDir (and again below if the preflight quarantines anything)
if ! bash "$SCRIPTS_DIR/build_groups.sh" >"$LOG_DIR/build_groups.curated.out" 2>&1; then
  echo "[psi] WARNING: build_groups.sh FAILED on the curated bedDir (see $LOG_DIR/build_groups.curated.out)" >&2
fi

# PRE-FLIGHT: quarantine any truncated/corrupt BED so AltAnalyze can't wedge on it (its bad-line handler is
# broken). If anything was dropped, rebuild groups on the clean set so groups.txt has no dangling samples.
_q="$(psi_check_beds)"
if [ "${_q:-0}" -gt 0 ] 2>/dev/null; then
  echo "[psi] preflight dropped $_q truncated sample(s) -> rebuilding groups on the clean set"
  # LOG the rebuild instead of swallowing it (`|| true` hid a build_groups failure -> stale groups.txt that
  # then silently mis-grouped or groupless-failed the PSI run with no trace).
  if ! bash "$SCRIPTS_DIR/build_groups.sh" >"$LOG_DIR/build_groups.out" 2>&1; then
    echo "[psi] WARNING: build_groups.sh FAILED after preflight quarantine -- groups.txt may be stale (see $LOG_DIR/build_groups.out)" >&2
  fi
fi

# need at least one junction BED to analyze
beds=("$BED_INPUT_DIR"/*__junction.bed)
[ "${#beds[@]}" -gt 0 ] || { echo "[psi] no *__junction.bed under $BED_INPUT_DIR" >&2; exit 1; }

# idempotent: PSI table already there -> skip
if psi_done; then echo "[psi] PSI output already present -> skip"; exit 0; fi

# JUNCTION PREVALENCE FILTER (manuscript Methods): keep junction j only if it is detected in >= round(tau*N) of
# the N libraries (JUNCTION_PREVALENCE_TAU, default 0.01; 0 = off). Artefactual junctions are library-private and
# ACCUMULATE with cohort size (A549 ~6,300 libraries: 125M distinct junctions, 3.2% annotated; AltAnalyze ran 50 h /
# 509 GB and produced nothing) while biological ones recur. Filtered COPIES go to junction_beds_filtered/ (the
# originals are untouched); a small cohort (tau*N <= 1) is left as-is. Same sample names -> groups.txt still matches.
_wdir="$(bash "$SCRIPTS_DIR/whitelist_job.sh" "$BED_INPUT_DIR" "$PIPELINE_ROOT/junction_beds_filtered" \
         2>>"$LOG_DIR/whitelist.log" | tail -n 1)"
if [ -n "$_wdir" ] && [ -d "$_wdir" ] && [ "$_wdir" != "$BED_INPUT_DIR" ]; then
  echo "[psi] junction prevalence filter applied -> bedDir $_wdir (see $_wdir/junction_prevalence_summary.tsv)"
  BED_INPUT_DIR="$_wdir"; export BED_INPUT_DIR PSI_BEDDIR="$_wdir"
  beds=("$BED_INPUT_DIR"/*__junction.bed)
else
  echo "[psi] junction prevalence filter not applied (off / cohort too small / failed -- see $LOG_DIR/whitelist.log)"
fi

# differential-comparison args (build_groups.sh wrote these only if a usable 2-group split exists)
GOPT=()
GO_FLAG=(--runGOElite no)
if [ -s "$GROUPS_FILE" ] && [ -s "$COMPS_FILE" ]; then
  GOPT=(--groupdir "$GROUPS_FILE" --compdir "$COMPS_FILE")
  [ "${RUN_GOELITE:-0}" = "1" ] && GO_FLAG=(--runGOElite yes --GEelitefold 1.5 --GEeliteptype rawp)
  echo "[psi] differential comparison: groups=$GROUPS_FILE comps=$COMPS_FILE"
else
  # AltAnalyze's RNASeq workflow is NOT groupless-capable -- with no groups.<expname>.txt/comps it exits at
  # the splicing step ("No groups or comps files found ... exiting"). Fail loudly so the watchdog STALLS with
  # a clear cause instead of burning resubmits on a doomed run. (SpliceScout always assigns groups upstream.)
  echo "[psi] ERROR: no groups/comps at $GROUPS_FILE -- AltAnalyze cannot run groupless. Ship a sample_groups.tsv." >&2
  exit 1
fi

echo "[psi] AltAnalyze: species=$SPECIES bedDir=$BED_INPUT_DIR out=$PSI_OUT expname=$EXPNAME (${#beds[@]} junction BEDs, $(psi_comparisons_expected) comparison(s) requested)"
# a new attempt invalidates any earlier clean-exit marker: DONE needs THIS run's clean exit (or every comparison file)
rm -f "$PIPELINE_ROOT/ALTANALYZE_OK.txt" 2>/dev/null
python "$AA" --species "$SPECIES" --platform RNASeq \
    --bedDir "$BED_INPUT_DIR" --output "$PSI_OUT" --expname "$EXPNAME" \
    --multiProcessing yes \
    ${GOPT[@]+"${GOPT[@]}"} "${GO_FLAG[@]}"
_aa_rc=$?
_cmp="$(psi_comparison_report)"                   # PSI_COMPARISONS.tsv: which requested comparisons were written
if [ "$_aa_rc" -ne 0 ]; then
  echo "[psi] AltAnalyze FAILED (rc=$_aa_rc) after writing $_cmp comparison file(s) -- see $PIPELINE_ROOT/PSI_COMPARISONS.tsv" >&2
  exit 1
fi
{ echo "AltAnalyze exited 0 at $(date '+%Y-%m-%d %H:%M:%S')"
  echo "comparisons produced/requested: $_cmp"; } > "$PIPELINE_ROOT/ALTANALYZE_OK.txt"
echo "[psi] AltAnalyze finished: $_cmp requested comparison(s) have a dPSI file (details: PSI_COMPARISONS.tsv)"

if ! psi_done; then
  echo "[psi] AltAnalyze ran but no PSI table under $PSI_OUT/AltResults/AlternativeOutput -- not done (safe to resubmit)" >&2
  exit 1
fi
echo "[psi] complete -> PSI results in $PSI_OUT/AltResults"

# wake the watchdog now (pure accelerator; the timed poll is the fallback)
psi_nudge_watchdog "altanalyze" || true
