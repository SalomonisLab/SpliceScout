#!/usr/bin/env bash
# =============================================================================
#  config.sh  —  THE ONLY FILE YOU NORMALLY NEED TO EDIT.
#  Every other script reads its settings from here. Adjust the values in the
#  "EDIT THESE" block for your cluster, directory, and archive, then run
#  ./run_pipeline.sh once and walk away.
# =============================================================================

# ------------------------------- EDIT THESE ----------------------------------

# 1) WHERE RESULTS LIVE (your ARCHIVE / permanent storage).
#    Root of this project. The final .fastq.gz are written here permanently.
#    >>> CHANGE to your own path. <<<
PIPELINE_ROOT="/data/CHANGE_ME/A_PROJECT_FOLDER"

# 2) INPUT LAYOUT. Each immediate subdirectory of STUDIES_DIR is one "study" and
#    must contain a SraAccList.txt (one SRA/ERR accession per line). YOU provide
#    these. (Default keeps everything self-contained under PIPELINE_ROOT.)
STUDIES_DIR="$PIPELINE_ROOT/by_study"

# 3) SCRATCH (fast/large transient workspace for extraction).
#    Heavy temp I/O goes here; only the final .fastq.gz is copied to the archive.
#    Use a LARGE, writable scratch volume — NOT a node root filesystem.
#    Leave EMPTY ("") to process in-place under PIPELINE_ROOT (simplest, but it
#    puts all transient I/O on your archive).
SCRATCH_DIR="/scratch/$USER"

# 4) SOFTWARE. If your cluster uses environment modules, name them. If prefetch
#    and fasterq-dump are already on PATH, set BOTH to "" to skip 'module load'.
SRATOOLKIT_MODULE="sratoolkit/3.0.0"
ASPERA_MODULE="aspera/3.9.1"

# 5) SCHEDULER (LSF). Tune to your cluster's limits.
LSF_QUEUE=""            # "" = cluster default queue; else e.g. "normal","long"
THREADS=5               # threads per conversion (also the job's slot request).
                        # IMPORTANT: under a per-user SLOT CAP, FEWER threads =
                        # MORE concurrent conversions = higher throughput
                        # (fasterq-dump is I/O-bound, ~flat above 6-8 threads).
                        # Find your cap with:  busers $USER   (MAX column).
MEM_MB=32000            # memory (-M) per conversion job
WALL="1108:00"          # -W per job (HH:MM) — normal-queue MAX (66480 min) so jobs never hit walltime
PREFETCH_MEM_MB=132000  # memory (-M) per prefetch (download) job
PREFETCH_MAX_SIZE="500G" # prefetch --max-size: its own default (20G) refuses every larger run, which then fails
                        # MAX_FAILS times and is DROPPED; deep RNA-seq runs are often 20-60 GB

# 6) AUTOMATION.
WATCHDOG_INTERVAL_MIN=30   # how often the self-driving watchdog re-checks. The watchdog WALLTIME is
                           # DERIVED as (this - 5) min, so it is ALWAYS < this interval (dead-man's-
                           # switch: a hung pass is killed ~5 min before its successor starts, so the
                           # chain can never overlap). A very large run whose passes legitimately need
                           # more time should RAISE this (e.g. 65 -> ~60-min passes); the walltime
                           # follows automatically. Keep >= 15.
ABSOLUTE_MAX_PASSES=960    # HARD backstop: STALL after this many watchdog passes no matter what
MAX_WALL_HOURS=336         # HARD backstop: STALL after this many wall-clock hours (generous; ~14 days)
BACKSTOP_RESET_GAP_HOURS=12 # a gap this long since the last pass = a RE-ARMED chain -> new backstop window
JOB_TAG="sra"              # short prefix that namespaces this project's LSF job
                           # names (set something unique if you run >1 project).
ALERT_EMAIL=""             # baked at deploy from the PC settings -> cluster jobs email the user on error/milestone ('' = off)
DIAGNOSE_ON_STALL=1        # on a STALL, ask the cluster CPU LLM (if installed) for a diagnosis + email it
DIAGNOSE_AUTOFIX=0         # 1 = also let the AI APPLY a SAFE whitelisted fix (quarantine bed / re-arm), budget-capped
DIAGNOSE_MAX_REARMS=2      # cap on AI auto re-arms before it stops and leaves the stall for a human
DIAGNOSE_AI_HOME="/data/salomonis-archive/LabFiles/SpliceScout_AI"   # self-contained CPU LLM (conda env + GGUF model)
DIAGNOSE_MODEL_PATH=""     # optional: full path to a specific .gguf to use (overrides the model search)
DIAGNOSE_MODEL_DIR=""      # optional: dir to CACHE the model so future runs reuse it ('' = <PIPELINE_ROOT>/.splicescout_ai/models)
CLEANUP_ON_COMPLETE="yes"  # after a SUCCESSFUL run, delete transient clutter:
                           # per-job .err/.out logs, generated .lsf scripts,
                           # *_missing lists, empty accession subdirs, state files.
                           # KEEPS: .fastq.gz, SraAccList.txt, PIPELINE_COMPLETE.txt,
                           # watchdog.log. Never runs on STALLED (logs kept to debug).
                           # Set "no" to keep all.
CLEANUP_SCRIPTS_ON_COMPLETE="yes"  # ALSO delete the pipeline's own scripts on a
                           # successful run, leaving a clean data-only folder. Only
                           # deletes scripts that live INSIDE PIPELINE_ROOT (a shared
                           # tools dir used by other projects is never touched). After
                           # this, re-copy the template to run again. Set "no" to keep.

# ----------------------------- END EDIT THESE --------------------------------


# ============ derived / internal — normally no need to edit below =============
# Directory containing these scripts (auto-detected). Must be on shared storage
# reachable from the compute nodes (it is, if it lives under PIPELINE_ROOT).
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export PIPELINE_ROOT STUDIES_DIR SCRATCH_DIR SRATOOLKIT_MODULE ASPERA_MODULE \
       LSF_QUEUE THREADS MEM_MB WALL PREFETCH_MEM_MB WATCHDOG_INTERVAL_MIN \
       ABSOLUTE_MAX_PASSES MAX_WALL_HOURS \
       JOB_TAG CLEANUP_ON_COMPLETE CLEANUP_SCRIPTS_ON_COMPLETE SCRIPTS_DIR

# Make the 'module' command available even in a non-login job shell.
sra_init_modules() {
  command -v module >/dev/null 2>&1 && return 0
  local f
  for f in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh \
           /usr/share/Modules/init/bash /usr/share/lmod/lmod/init/bash; do
    [ -r "$f" ] && . "$f" 2>/dev/null && command -v module >/dev/null 2>&1 && return 0
  done
  return 0   # no module system found -> assume tools are already on PATH
}

# Load the configured tool modules (no-op if MODULE vars are empty / tools on PATH).
sra_load_modules() {
  sra_init_modules
  [ -n "$SRATOOLKIT_MODULE" ] && module load "$SRATOOLKIT_MODULE" 2>/dev/null
  [ -n "$ASPERA_MODULE" ]     && module load "$ASPERA_MODULE"     2>/dev/null
  return 0
}
export -f sra_init_modules sra_load_modules
