#!/usr/bin/env bash
# =============================================================================
#  config.sh  --  THE ONLY FILE YOU NORMALLY NEED TO EDIT.
#
#  Self-driving splicing-CONCORDANCE stage. Runs AFTER AltAnalyze PSI: it gathers
#  the per-drug PSI signatures (the Events-dPSI PSI.<drug>_vs_control.txt tables),
#  scores each against one or more cancer-subtype atlases with the vendored
#  splicingConcordance_advanced.py, and writes a RANKED reversal-candidate summary
#  per atlas (concordance + overlapping events + patient counts).
#
#       chmod +x *.sh
#       ./run_concordance_pipeline.sh
#
#  ...and walk away. A watchdog resubmits a failed run and writes
#  PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into PIPELINE_ROOT when done.
#
#  Concordance value: 1 = the drug MIMICS the cancer subtype (bad); 0 = the drug
#  REVERSES it (therapeutic). Candidates are concordance < CONC_THRESHOLD, ranked
#  by overlapping splicing events and the subtype's patient count.
# =============================================================================

# ------------------------------- EDIT THESE ----------------------------------

# 1) UPSTREAM PSI pipeline root (holds the drug signatures AND the PIPELINE_COMPLETE.txt
#    that the launcher waits on). Normally a sibling 'psi' folder next to STAR_bams.
PSI_ROOT="/data/CHANGE_ME/psi"

# 1b) Where the per-drug PSI signatures live. BLANK => the AltAnalyze dPSI dir under PSI_ROOT
#     ($PSI_ROOT/output/AltResults/AlternativeOutput/Events-dPSI_0.1_rawp). These PSI.*_vs_*.txt
#     files are the "ref" set scored against the cancer atlases.
PSI_EVENTS_DIR=""

# 1c) STATE + MARKERS + results live here (this is also where this bundle was uploaded).
PIPELINE_ROOT="/data/CHANGE_ME/concordance"

# 2) AltAnalyze install -- ONLY its export/UI/unique modules are needed (put on PYTHONPATH so the
#    vendored scorer's `import export, UI, unique` resolve). BLANK => $PSI_ROOT/altanalyze_home
#    (the same install the PSI stage resolved/uploaded).
ALTANALYZE_HOME=""

# 3) The vendored concordance scorer. BLANK => $SCRIPTS_DIR/splicingConcordance_advanced.py.
CONCORD_SCRIPT=""
#    The cancer atlases to score against are listed in QUERIES_FILE (shipped beside this config):
#    one row per atlas =  name<TAB>query_dir<TAB>counts_kind(tsv|mergedresult|none)<TAB>counts_path

# 4) CONCORDANCE thresholds (passed to the scorer).
RAWP=0.05                         # keep query/ref events with rawp <= this
DPSI=0.1                          # keep events with |dPSI| >= this
REMOVE_IR=0                       # 1 => also pass --removeIR True (drop intron-retention events)
MIN_OVERLAP=25                    # shared events a pair needs to be significance-tested (manuscript: |E| >= 25).
                                  # The scorer itself scores every pair with >= 5 (all_scored_pairs.tsv lists them).
CONC_THRESHOLD=0.3                # LEGACY cut-point: only used when pair_stats.tsv is absent (older scorer)
FDR_ALPHA=0.05                    # analytic null: a pair is a candidate at Benjamini-Hochberg q < this
SUMMARY_ROWS_PER_DRUG=3           # ranked summary shows at most this many subtypes per drug (every drug still listed)
ENRICH_AGENTS=""                  # optional, e.g. "indisulam,gsk591": Mann-Whitney positioning of these agents per
                                  # subtype -> results/mechanism_enrichment.tsv (the manuscript's Table 4)
STUDY_MATCHED_ONLY=1              # 1 = quarantine CROSS-study contrasts (drug and control arms from different GSEs,
                                  # or a MULTISTUDY pooled baseline) before scoring: they are batch-dominated
                                  # (~5x larger signatures) and would monopolise the ranking. 0 = score them too.
EXCLUDE_REAGENTS=1                # 1 = leave out contrasts whose 'drug' is a lab reagent (4sU/EdU labels, dox/tet
                                  # induction, dTAG/auxin degrons, puromycin/G418 selection) -- they report the
                                  # experimental system, not a drug. Moved to drug_signatures_reagents/. 0 = keep.

# 5) Cancer atlas label (informational; the actual queries are in QUERIES_FILE, auto-selected by the deployer
#    from the cell line, e.g. MDS-L -> AML/MDS, A549 -> lung LUAD+LUSC).
CANCER_ATLAS="auto"

# 6) ORGANISM -> SPECIES code (Hs/Mm/Rn/Dr/Ss/Ma). Mostly informational for this stage.
ORGANISM="Homo sapiens"
SPECIES=""                        # "" => auto from ORGANISM (default Hs)
EXPNAME="splicing"                # cell-line slug, for labeling the summary

# 7) RESOURCES. Concordance is ONE light job (set intersections over text tables).
THREADS=1                         # -n
MEM_MB=8000                       # -M
WALL="1108:00"                      # -W (HH:MM)
LSF_QUEUE=""                      # "" = cluster default queue

# 8) AUTOMATION.
JOB_TAG="concordance"             # namespaces this run's LSF job names (make it unique per run)
ALERT_EMAIL=""                    # baked at deploy from the PC settings -> cluster jobs email the user on error/milestone ('' = off)
DIAGNOSE_ON_STALL=1               # on a STALL, ask the cluster CPU LLM (if installed) for a diagnosis + email it
DIAGNOSE_AUTOFIX=0                # 1 = also let the AI APPLY a SAFE whitelisted fix (quarantine bed / re-arm), budget-capped
DIAGNOSE_MAX_REARMS=2             # cap on AI auto re-arms before it stops and leaves the stall for a human
DIAGNOSE_AI_HOME="/data/salomonis-archive/LabFiles/SpliceScout_AI"   # self-contained CPU LLM (conda env + GGUF model)
DIAGNOSE_MODEL_PATH=""            # optional: full path to a specific .gguf to use (overrides the model search)
DIAGNOSE_MODEL_DIR=""             # optional: dir to CACHE the model so future runs reuse it ('' = <PIPELINE_ROOT>/.splicescout_ai/models)
WATCHDOG_INTERVAL_MIN=30          # how often the self-driving watchdog re-checks
MAX_RESUBMITS=2                   # resubmit the single concordance job at most this many times -> STALLED
IDLE_STALL_PASSES=3               # a RUN job whose cpu_used is frozen this many passes = DEADLOCKED -> kill+resubmit+email
ABSOLUTE_MAX_PASSES=960           # HARD backstop: STALL after this many watchdog passes no matter what
MAX_WALL_HOURS=336                # HARD backstop: STALL after this many wall-clock hours (~14d)
BACKSTOP_RESET_GAP_HOURS=12       # a gap this long since the last pass = a RE-ARMED chain -> new backstop window

# 9) SOFTWARE MODULE (the scorer + ranker run under python2 with AltAnalyze's modules on PYTHONPATH).
PYTHON_MODULE="python/2.7.5"

# ----------------------------- END EDIT THESE --------------------------------


# ============ derived / internal -- normally no need to edit below ============
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PSI_ROOT="${PSI_ROOT%/}"
PIPELINE_ROOT="${PIPELINE_ROOT%/}"
LOG_DIR="$PIPELINE_ROOT/logs"
[ -n "$PSI_EVENTS_DIR" ] || PSI_EVENTS_DIR="$PSI_ROOT/output/AltResults/AlternativeOutput/Events-dPSI_0.1_rawp"
DRUG_SIG_DIR="$PIPELINE_ROOT/drug_signatures"      # the gathered "ref" set (also where the scorer writes)
RESULTS_DIR="$PIPELINE_ROOT/results"               # per-atlas concordance.txt + ranked summary land here
QUERIES_FILE="$SCRIPTS_DIR/queries.tsv"            # shipped: name<TAB>query_dir<TAB>counts_kind<TAB>counts_path
[ -n "$CONCORD_SCRIPT" ] || CONCORD_SCRIPT="$SCRIPTS_DIR/splicingConcordance_advanced.py"

# ORGANISM (NCBI scientific name) -> AltAnalyze species code. Default Hs.
concord_species_from_organism() {
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
[ -n "$SPECIES" ]         || SPECIES="$(concord_species_from_organism "$ORGANISM")"
[ -n "$ALTANALYZE_HOME" ] || ALTANALYZE_HOME="$PSI_ROOT/altanalyze_home"

export PSI_ROOT PSI_EVENTS_DIR PIPELINE_ROOT ALTANALYZE_HOME CONCORD_SCRIPT QUERIES_FILE \
       RAWP DPSI REMOVE_IR MIN_OVERLAP CONC_THRESHOLD FDR_ALPHA SUMMARY_ROWS_PER_DRUG ENRICH_AGENTS \
       STUDY_MATCHED_ONLY EXCLUDE_REAGENTS CANCER_ATLAS ORGANISM SPECIES EXPNAME \
       THREADS MEM_MB WALL LSF_QUEUE JOB_TAG WATCHDOG_INTERVAL_MIN MAX_RESUBMITS \
       ABSOLUTE_MAX_PASSES MAX_WALL_HOURS PYTHON_MODULE \
       SCRIPTS_DIR LOG_DIR DRUG_SIG_DIR RESULTS_DIR

# Make the 'module' command available even in a non-login job shell.
concord_init_modules() {
  command -v module >/dev/null 2>&1 && return 0
  local f
  for f in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh \
           /usr/share/Modules/init/bash /usr/share/lmod/lmod/init/bash; do
    [ -r "$f" ] && . "$f" 2>/dev/null && command -v module >/dev/null 2>&1 && return 0
  done
  return 0
}
concord_load_modules() {
  concord_init_modules
  [ -n "$PYTHON_MODULE" ] && module load "$PYTHON_MODULE" 2>/dev/null
  return 0
}
export -f concord_init_modules concord_load_modules
