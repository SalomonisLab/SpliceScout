#!/usr/bin/env bash
# =============================================================================
#  config.sh  --  THE ONLY FILE YOU NORMALLY NEED TO EDIT.
#
#  Self-driving BAM -> BED stage (AltAnalyze junction/exon BED extraction). Runs
#  AFTER STAR: one job per BAM converts it to <sample>__junction.bed plus, per
#  BED_MODE, an __exon.bed and/or __intronJunction.bed (the inputs AltAnalyze
#  splicing analysis consumes). Fill in the "EDIT THESE" block, then:
#
#       chmod +x *.sh
#       ./run_bed_pipeline.sh
#
#  ...and walk away. A watchdog job resubmits any failures and writes
#  PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into PIPELINE_ROOT when done.
#
#  ALL-IN-ONE: the AltAnalyze scripts + reference are VENDORED beside this file
#  (altanalyze/), so the cluster needs NO AltAnalyze install -- only the stock
#  python/2.7.5 (which provides pysam) and samtools modules.
# =============================================================================

# ------------------------------- EDIT THESE ----------------------------------

# 1) WHERE THE STAR BAMs ARE (normally STAR's BAM_OUT).
BAM_INPUT_DIR="/data/CHANGE_ME/STAR_bams"

# 1b) WHERE THE BED OUTPUTS GO -- kept SEPARATE from the BAMs. Blank => a sibling 'STAR_beds' folder
#     next to BAM_INPUT_DIR. The AltAnalyze tools write each .bed beside its BAM (no output-dir option);
#     run_bed_job.sh MOVES the results here so the BAM folder stays clean.
BED_OUT_DIR=""

# 2) VENDORED AltAnalyze toolkit. Blank = use the altanalyze/ folder shipped beside
#    these scripts (import_scripts/BAMto*BED.py + export.py + unique.py + refs/).
ALTANALYZE_DIR=""

# 3) ORGANISM -> SPECIES code (Hs/Mm/Rn/Dr/Ss/Ma). SPECIES auto-derived from ORGANISM
#    below; override only if you must.
ORGANISM="Homo sapiens"
SPECIES=""                        # "" => auto from ORGANISM (default Hs); e.g. "Mm" to force

# 4) Ensembl exon reference. Blank => derived from ALTANALYZE_DIR + SPECIES
#    (altanalyze/refs/<SP>/<SP>_Ensembl_exon.txt). Override only for a custom ref.
EXON_REF=""

# 4b) WHICH BEDs to produce: intron (default) | exon | both. <sample>__junction.bed is
#     ALWAYS produced; this picks the BAMtoExonBED pass:
#       intron => <sample>__intronJunction.bed  (intron-retention; the AltAnalyze default)
#       exon   => <sample>__exon.bed            (per-exon read counts)
#       both   => both of the above
BED_MODE="intron"

# 5) RESOURCES. BAMtoBED is a single Python process (I/O+parse bound, not threaded).
THREADS=1                         # -n (LSF slots per job)
MEM_MB=32000                      # -M per job
WALL="1108:00"                    # -W per job (HH:MM) — queue MAX (66480 min) so jobs never hit walltime
LSF_QUEUE=""                      # "" = cluster default queue

# 6) AUTOMATION.
JOB_TAG="bed"                     # namespaces this run's LSF job names (make it unique per run)
ALERT_EMAIL=""                    # baked at deploy from the PC settings -> cluster jobs email the user on error/milestone ('' = off)
DIAGNOSE_ON_STALL=1               # on a STALL, ask the cluster CPU LLM (if installed) for a diagnosis + email it
DIAGNOSE_AUTOFIX=0                # 1 = also let the AI APPLY a SAFE whitelisted fix (quarantine bed / re-arm), budget-capped
DIAGNOSE_MAX_REARMS=2             # cap on AI auto re-arms before it stops and leaves the stall for a human
DIAGNOSE_AI_HOME="/data/salomonis-archive/LabFiles/SpliceScout_AI"   # self-contained CPU LLM (conda env + GGUF model)
DIAGNOSE_MODEL_PATH=""            # optional: full path to a specific .gguf to use (overrides the model search)
DIAGNOSE_MODEL_DIR=""             # optional: dir to CACHE the model so future runs reuse it ('' = <PIPELINE_ROOT>/.splicescout_ai/models)
WATCHDOG_INTERVAL_MIN=30          # how often the self-driving watchdog re-checks
MAX_STALL_PASSES=2                # consecutive no-progress passes before giving up (STALLED)
ABSOLUTE_MAX_PASSES=960           # HARD backstop: STALL after this many watchdog passes no matter what
MAX_WALL_HOURS=336                # HARD backstop: STALL after this many wall-clock hours (generous; ~14d)
STRICT_BED_CHECK=1               # a "done" BED must be complete (trailing newline + parseable last row), not just non-empty
CLEANUP_TOOLS_WHEN_DONE=1         # on COMPLETE, remove the uploaded tooling (AltAnalyze toolkit + 100MB ref, STAR bundle, download scripts); keeps BAMs + BED outputs
DELETE_BAM_AFTER_BED=0            # delete each BAM once its BEDs are made+verified (frees the STAR_bams volume; default OFF)

# 7) SOFTWARE MODULES.  python/2.7.5 supplies BOTH python2 AND pysam (the AltAnalyze
#    import scripts are Python 2). Set to "" if a tool is already on PATH.
PYTHON_MODULE="python/2.7.5"
SAMTOOLS_MODULE="samtools"

# ----------------------------- END EDIT THESE --------------------------------


# ============ derived / internal -- normally no need to edit below ============
# Directory holding these scripts (must be on storage the compute nodes can see).
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Vendored AltAnalyze toolkit beside these scripts unless overridden above.
[ -n "$ALTANALYZE_DIR" ] || ALTANALYZE_DIR="$SCRIPTS_DIR/altanalyze"

# State + markers live in a 'bed' subdir of BAM_INPUT_DIR so they NEVER collide with
# STAR's PIPELINE_COMPLETE.txt / watchdog.log already sitting in BAM_INPUT_DIR.
PIPELINE_ROOT="$BAM_INPUT_DIR/bed"
LOG_DIR="$PIPELINE_ROOT/logs"
BAM_LIST="$PIPELINE_ROOT/bam_list.tsv"

# ORGANISM (NCBI scientific name) -> AltAnalyze species code. Default Hs.
bed_species_from_organism() {
  local o
  o="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  case "$o" in
    *"homo sapiens"*|"hs"|"human")        echo "Hs" ;;
    *"mus musculus"*|"mm"|"mouse")        echo "Mm" ;;
    *"rattus norvegicus"*|"rn"|"rat")     echo "Rn" ;;
    *"danio rerio"*|"dr"|"zebrafish")     echo "Dr" ;;
    *"sus scrofa"*|"ss"|"pig")            echo "Ss" ;;
    *"macaca"*|"ma"|"macaque"|"rhesus")   echo "Ma" ;;
    *)                                    echo "Hs" ;;
  esac
}
# Declared on SEPARATE statements (bash 4.2 + set -u can't see a var set earlier in the SAME line).
[ -n "$SPECIES" ]  || SPECIES="$(bed_species_from_organism "$ORGANISM")"
[ -n "$EXON_REF" ] || EXON_REF="$ALTANALYZE_DIR/refs/$SPECIES/${SPECIES}_Ensembl_exon.txt"
case "$BED_MODE" in intron|exon|both) ;; *) BED_MODE="intron" ;; esac   # normalize unknown -> default
# BED outputs go to a sibling 'STAR_beds' folder by default (kept OUT of the BAM folder).
[ -n "$BED_OUT_DIR" ] || BED_OUT_DIR="$(dirname "$BAM_INPUT_DIR")/STAR_beds"

export BAM_INPUT_DIR BED_OUT_DIR ALTANALYZE_DIR ORGANISM SPECIES EXON_REF BED_MODE CLEANUP_TOOLS_WHEN_DONE DELETE_BAM_AFTER_BED \
       THREADS MEM_MB WALL LSF_QUEUE JOB_TAG WATCHDOG_INTERVAL_MIN MAX_STALL_PASSES ABSOLUTE_MAX_PASSES MAX_WALL_HOURS STRICT_BED_CHECK \
       PYTHON_MODULE SAMTOOLS_MODULE SCRIPTS_DIR PIPELINE_ROOT LOG_DIR BAM_LIST

# Make the 'module' command available even in a non-login job shell.
bed_init_modules() {
  command -v module >/dev/null 2>&1 && return 0
  local f
  for f in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh \
           /usr/share/Modules/init/bash /usr/share/lmod/lmod/init/bash; do
    [ -r "$f" ] && . "$f" 2>/dev/null && command -v module >/dev/null 2>&1 && return 0
  done
  return 0   # no module system found -> assume the tools are already on PATH
}

# Load the configured tool modules (no-op if a MODULE var is empty / tool on PATH).
bed_load_modules() {
  bed_init_modules
  [ -n "$PYTHON_MODULE" ]   && module load "$PYTHON_MODULE"   2>/dev/null
  [ -n "$SAMTOOLS_MODULE" ] && module load "$SAMTOOLS_MODULE" 2>/dev/null
  return 0
}
export -f bed_init_modules bed_load_modules
