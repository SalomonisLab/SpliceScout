#!/usr/bin/env bash
# =============================================================================
#  config.sh  --  THE ONLY FILE YOU NORMALLY NEED TO EDIT.
#
#  Self-driving AltAnalyze splicing (PSI) stage. Runs AFTER BAM->BED: ONE
#  AltAnalyze job consumes the whole directory of <sample>__junction.bed files
#  and produces a per-sample PSI (percent-spliced-in) table, plus -- when a
#  groups.txt/comps.txt is present -- a differential (dPSI) comparison.
#
#       chmod +x *.sh
#       ./run_psi_pipeline.sh
#
#  ...and walk away. A watchdog job resubmits a failed run and writes
#  PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into PIPELINE_ROOT when done.
#
#  AltAnalyze itself is NOT vendored in this bundle (it is multi-GB with its
#  species database). The deployer either points ALTANALYZE_HOME at an install
#  already on the cluster, or uploads one to PIPELINE_ROOT/altanalyze_home; either
#  way ALTANALYZE_HOME below is pre-filled to the resolved location.
# =============================================================================

# ------------------------------- EDIT THESE ----------------------------------

# 1) WHERE THE BEDs ARE (the BAM->BED stage's BED_OUT_DIR; normally a sibling
#    'STAR_beds' folder next to STAR_bams). AltAnalyze scans this for *__junction.bed.
BED_INPUT_DIR="/data/CHANGE_ME/STAR_beds"

# 1b) STATE + MARKERS live here (this is also where this bundle was uploaded).
PIPELINE_ROOT="/data/CHANGE_ME/psi"

# 2) AltAnalyze install on the cluster. AltAnalyze.py lives at $ALTANALYZE_HOME; its
#    species database is $ALTANALYZE_HOME/AltDatabase unless ALTANALYZE_DB overrides it.
#    BLANK => $PIPELINE_ROOT/altanalyze_home (the portable default, where the deployer uploads a
#    copy when none is found on the cluster). Point it at an existing cluster install to reuse that.
ALTANALYZE_HOME=""
ALTANALYZE_DB=""                  # "" => $ALTANALYZE_HOME/AltDatabase ; else an external
                                  #       AltDatabase path (setup.sh symlinks it into HOME)

# 3) ORGANISM -> SPECIES code (Hs/Mm/Rn/Dr/Ss/Ma). SPECIES auto-derived from ORGANISM.
ORGANISM="Homo sapiens"
SPECIES=""                        # "" => auto from ORGANISM (default Hs); e.g. "Mm" to force

# 4) OUTPUT + experiment name. PSI_OUT blank => $PIPELINE_ROOT/output.
PSI_OUT=""
EXPNAME="splicing"

# 5) DIFFERENTIAL COMPARISON. groups.txt/comps.txt are BUILT cluster-side by
#    build_groups.sh from sample_groups.tsv (shipped) intersected with the BEDs that
#    are actually present. AltAnalyze's RNASeq workflow CANNOT run groupless: with no usable
#    2-group split run_psi_job.sh fails loudly (-> STALL with a clear cause). RUN_GOELITE is
#    honored only when a comparison runs.
RUN_GOELITE=0                     # 1 => --runGOElite yes (needs the GO-Elite DB + R module)
GROUP_KEY_SUFFIX=".bed"           # how a sample is keyed in groups.txt (AltAnalyze: <sample>.bed)
JUNCTION_PREVALENCE_TAU=0.01      # junction whitelist: keep a junction only if seen in >= round(tau*N) of the N
                                  # libraries (manuscript: tau=1%; restores the annotated fraction on large pooled
                                  # cohorts). 0 = off. A no-op below ~150 libraries (tau*N rounds to <= 1).
PSI_GROUPED_BEDS_ONLY=1           # 1 = only BEDs of samples listed in sample_groups.tsv enter AltAnalyze's bedDir
                                  # (the dPSI comparisons use nothing else); 0 = every BED. A gzipped BED
                                  # (*.bed.gz, e.g. after COMPRESS_WHEN_DONE) is decompressed into a private copy.

# 6) RESOURCES. AltAnalyze is ONE multi-process job (not per-sample).
THREADS=4                         # -n (LSF slots; AltAnalyze --multiProcessing yes)
MEM_MB=128000                     # -M
WALL="1108:00"                    # -W (HH:MM) — queue MAX (66480 min) so the job never hits walltime
LSF_QUEUE=""                      # "" = cluster default queue

# 7) AUTOMATION.
JOB_TAG="psi"                     # namespaces this run's LSF job names (make it unique per run)
ALERT_EMAIL=""                    # baked at deploy from the PC settings -> cluster jobs email the user on error/milestone ('' = off)
DIAGNOSE_ON_STALL=1               # on a STALL, ask the cluster CPU LLM (if installed) for a diagnosis + email it
DIAGNOSE_AUTOFIX=0                # 1 = also let the AI APPLY a SAFE whitelisted fix (quarantine bed / re-arm), budget-capped
DIAGNOSE_MAX_REARMS=2             # cap on AI auto re-arms before it stops and leaves the stall for a human
DIAGNOSE_AI_HOME="/data/salomonis-archive/LabFiles/SpliceScout_AI"   # self-contained CPU LLM (conda env + GGUF model)
DIAGNOSE_MODEL_PATH=""            # optional: full path to a specific .gguf to use (overrides the model search)
DIAGNOSE_MODEL_DIR=""             # optional: dir to CACHE the model so future runs reuse it ('' = <PIPELINE_ROOT>/.splicescout_ai/models)
WATCHDOG_INTERVAL_MIN=30          # how often the self-driving watchdog re-checks
MAX_RESUBMITS=2                   # resubmit the single AltAnalyze job at most this many times -> STALLED
IDLE_STALL_PASSES=3               # a RUN job whose cpu_used is frozen this many passes = DEADLOCKED -> kill+resubmit+email
ABSOLUTE_MAX_PASSES=960           # HARD backstop: STALL after this many watchdog passes no matter what
MAX_WALL_HOURS=336                # HARD backstop: STALL after this many wall-clock hours (~14d)
BACKSTOP_RESET_GAP_HOURS=12       # a gap this long since the last pass = a RE-ARMED chain -> new backstop window
CLEANUP_TOOLS_WHEN_DONE=0         # remove an UPLOADED altanalyze_home on COMPLETE (default OFF; never
                                  # touches a found-on-cluster ALTANALYZE_HOME)

# 7b) COMPRESS REMAINING DATA once the PSI stage COMPLETEs (reclaim space -- the archive fills up fast).
#     Compresses kept, still-uncompressed files (BEDs + AltAnalyze text outputs) under COMPRESS_DIR.
#     Already-compressed files and BAM/BAI/CRAM are SKIPPED (re-compressing them costs time for ~no gain).
COMPRESS_WHEN_DONE="gzip"         # gzip | xz | off   (xz = LZMA2: smaller but slower). off = leave as-is.
COMPRESS_DIR=""                   # "" => parent of PIPELINE_ROOT (the whole project: BEDs + PSI output)
COMPRESS_MIN_MB=1                 # only compress files at least this big (skips tiny markers/scripts/logs)
COMPRESS_THREADS=8                # parallelism for pigz / xz

# 8) SOFTWARE MODULES (match the lab's AltAnalyze.sh: python2 + samtools + R).
PYTHON_MODULE="python/2.7.5"
SAMTOOLS_MODULE="samtools"
R_MODULE="R"

# ----------------------------- END EDIT THESE --------------------------------


# ============ derived / internal -- normally no need to edit below ============
# Directory holding these scripts (must be on storage the compute nodes can see).
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Normalize + derive. Strip a trailing slash so ${VAR%/*} math is correct.
BED_INPUT_DIR="${BED_INPUT_DIR%/}"
PIPELINE_ROOT="${PIPELINE_ROOT%/}"
LOG_DIR="$PIPELINE_ROOT/logs"
SAMPLE_GROUPS="$SCRIPTS_DIR/sample_groups.tsv"     # shipped: BioSample<TAB>group_num<TAB>group_label
[ -n "$PSI_OUT" ] || PSI_OUT="$PIPELINE_ROOT/output"
[ -n "$COMPRESS_DIR" ] || COMPRESS_DIR="$(dirname "$PIPELINE_ROOT")"   # default compress scope: the whole project tree
# AltAnalyze is NOT groupless-capable: its RNASeq workflow looks for groups.<expname>.txt + comps.<expname>.txt
# under <output>/ExpressionInput/ and EXITS the splicing step if they are absent. Build them at exactly that
# path/name so --groupdir/--compdir (and AltAnalyze's own default lookup) both resolve them.
GROUPS_FILE="$PSI_OUT/ExpressionInput/groups.$EXPNAME.txt"   # built cluster-side (sample_groups ∩ present BEDs)
COMPS_FILE="$PSI_OUT/ExpressionInput/comps.$EXPNAME.txt"

# ORGANISM (NCBI scientific name) -> AltAnalyze species code. Default Hs.
psi_species_from_organism() {
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
[ -n "$SPECIES" ]         || SPECIES="$(psi_species_from_organism "$ORGANISM")"
[ -n "$ALTANALYZE_HOME" ] || ALTANALYZE_HOME="$PIPELINE_ROOT/altanalyze_home"   # portable default: under PIPELINE_ROOT
[ -n "$ALTANALYZE_DB" ]   || ALTANALYZE_DB="$ALTANALYZE_HOME/AltDatabase"

export BED_INPUT_DIR PIPELINE_ROOT ALTANALYZE_HOME ALTANALYZE_DB ORGANISM SPECIES PSI_OUT EXPNAME \
       RUN_GOELITE GROUP_KEY_SUFFIX JUNCTION_PREVALENCE_TAU PSI_GROUPED_BEDS_ONLY THREADS MEM_MB WALL LSF_QUEUE JOB_TAG WATCHDOG_INTERVAL_MIN \
       MAX_RESUBMITS ABSOLUTE_MAX_PASSES MAX_WALL_HOURS CLEANUP_TOOLS_WHEN_DONE \
       PYTHON_MODULE SAMTOOLS_MODULE R_MODULE SCRIPTS_DIR LOG_DIR SAMPLE_GROUPS GROUPS_FILE COMPS_FILE \
       COMPRESS_WHEN_DONE COMPRESS_DIR COMPRESS_MIN_MB COMPRESS_THREADS

# Make the 'module' command available even in a non-login job shell.
psi_init_modules() {
  command -v module >/dev/null 2>&1 && return 0
  local f
  for f in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh \
           /usr/share/Modules/init/bash /usr/share/lmod/lmod/init/bash; do
    [ -r "$f" ] && . "$f" 2>/dev/null && command -v module >/dev/null 2>&1 && return 0
  done
  return 0   # no module system found -> assume the tools are already on PATH
}

# Load the configured tool modules (no-op if a MODULE var is empty / tool on PATH).
psi_load_modules() {
  psi_init_modules
  [ -n "$PYTHON_MODULE" ]   && module load "$PYTHON_MODULE"   2>/dev/null
  [ -n "$SAMTOOLS_MODULE" ] && module load "$SAMTOOLS_MODULE" 2>/dev/null
  [ -n "$R_MODULE" ]        && module load "$R_MODULE"        2>/dev/null
  return 0
}
export -f psi_init_modules psi_load_modules
