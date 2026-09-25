#!/usr/bin/env bash
# =============================================================================
#  config.sh  --  THE ONLY FILE YOU NORMALLY NEED TO EDIT.
#
#  Self-driving STAR 2-pass alignment pipeline. Every other script reads its
#  settings from here. Fill in the "EDIT THESE" block, then:
#
#       chmod +x *.sh
#       ./run_star_pipeline.sh
#
#  ...and walk away. A watchdog job resubmits any failures and writes
#  PIPELINE_COMPLETE.txt (or PIPELINE_STALLED.txt) into BAM_OUT when finished.
# =============================================================================

# ------------------------------- EDIT THESE ----------------------------------

# 1) WHERE THE FASTQs ARE.  Scanned RECURSIVELY for *.fastq.gz / *.fq.gz, so this
#    may be a flat folder OR a nested tree (e.g. by_study/<GSE>/*.fastq.gz).
FASTQ_INPUT_DIR="/data/CHANGE_ME/fastqs"

# 2) WHERE RESULTS GO.  One <label>.bam (+ .bai) per sample is published here;
#    per-sample STAR logs + splice junctions land in $BAM_OUT/logs/. Use a
#    volume with room (~1-3 GB per sample).
BAM_OUT="/data/CHANGE_ME/STAR_bams"

# 3) STAR GENOME INDEX  (prebuilt with `STAR --runMode genomeGenerate`).
#    May be left empty: if this isn't a real index, the pipeline resolves one by ORGANISM via the
#    registry (star_index_registry.json), a previously built index, or a BUILD-ONCE job (see 3b).
GENOME_DIR="/data/CHANGE_ME/STAR-index"

# 3b) GENOME-INDEX RESOLUTION (auto-filled by SpliceScout; safe to edit). Used only when GENOME_DIR
#     above is not a valid index. ORGANISM drives the registry lookup + which reference gets built.
ORGANISM="Homo sapiens"
STAR_INDEX_ROOT=""                # where a build-once index is written (default: $PIPELINE_ROOT/STAR-index)
REF_FASTA_URL=""                  # genome FASTA(.gz) URL for ORGANISM (filled from the registry)
REF_GTF_URL=""                    # annotation GTF(.gz) URL for ORGANISM (filled from the registry)
BUILD_THREADS=16                  # genomeGenerate threads / LSF slots (heavy)
BUILD_MEM_MB=64000                # genomeGenerate -M (GRCh38 needs ~32-40 GB)
BUILD_MEM_RUSAGE=4000             # rusage[mem=] per slot for the build
BUILD_WALL="1108:00"              # genomeGenerate -W (HH:MM) — queue MAX (66480 min); never hits walltime

# 4) SPLICE JUNCTIONS.  Leave SJDB_GTF EMPTY if the index was built WITH a GTF
#    (the usual case -- re-supplying it at align time only wastes RAM). Set it to
#    a GTF path ONLY if your index has no junctions baked in.
SJDB_GTF=""
SJDB_OVERHANG="100"               # used ONLY when SJDB_GTF is non-empty

# 5) SAMPLE LABELS (optional).  A CSV or TSV with at least a Run column and a
#    BioSample column (header names auto-detected, case-insensitive; an optional
#    LibraryLayout column is used as a cross-check). When given, runs of the same
#    BioSample are MERGED into one alignment (one BAM per biological sample).
#    Leave EMPTY to make one BAM per run (labelled by run/file accession).
#    NOTE: .xlsx is NOT supported -- export it to .csv first.
RUNTABLE=""

# 6) SCRATCH -- a fast, RELIABLE workspace for staging + STAR temp (NOT a slow or
#    flaky archive volume). The default suits most clusters.
SCRATCH="/scratch/$USER"

# 7) STAR / RESOURCES.
THREADS=5                         # --runThreadN AND the LSF slot request (-n); keep equal. 5 packs 16-core nodes 3-up & divides a 125-slot cap evenly (25 jobs).
SORT_RAM=20000000000              # --limitBAMsortRAM in BYTES; keep WELL under MEM_MB
STAR_EXTRA_ARGS=""                # raw extra args appended to the STAR command line

# 8) LSF SCHEDULER.
MEM_MB=64000                      # -M per job (genome ~29 GB + sort + headroom)
MEM_RUSAGE=10000                  # rusage[mem=] per slot; x THREADS ~= total reserved
WALL="1108:00"                    # -W per job (HH:MM) — queue MAX (66480 min) so jobs never hit walltime
LSF_QUEUE=""                      # "" = cluster default queue; else e.g. "normal"/"long"

# 9) AUTOMATION.
JOB_TAG="star"                    # namespaces this run's LSF job names (make it unique per run)
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
BACKSTOP_RESET_GAP_HOURS=12       # a gap this long since the last pass = a RE-ARMED chain -> new backstop window

# 9b) RELIABILITY + CLEANUP (free disk as the pipeline progresses; cleanup defaults ON, set 0 to keep).
DELETE_FASTQ_AFTER_BAM=1          # delete a sample's source FASTQ(s) once its BAM is published+verified
CLEANUP_TOOLS_WHEN_DONE=1         # on COMPLETE, remove the uploaded tooling (download scripts, empty by_study)
STRICT_BAM_CHECK=1               # a "done" BAM must also have >0 mapped reads + a QC log (not just quickcheck)
MIN_MAPPED_FRAC=0.01            # refuse to delete a source FASTQ if uniquely-mapped fraction < this (suspect)
VERIFY_FASTQ_DIRECT=1           # gzip -t the source FASTQ on the read-in-place path (catch a truncated NFS read)

# 10) SOFTWARE MODULES.  Set to "" if the tool is already on PATH (skips 'module load').
STAR_MODULE="STAR/2.7.10b"
SAMTOOLS_MODULE="samtools"

# ----------------------------- END EDIT THESE --------------------------------


# ============ derived / internal -- normally no need to edit below ============
# Directory holding these scripts (must be on storage the compute nodes can see).
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# State + outputs all live under the BAM output dir.
PIPELINE_ROOT="$BAM_OUT"
LOG_DIR="$BAM_OUT/logs"
SAMPLE_LIST="$PIPELINE_ROOT/sample_list.tsv"
STAR_INDEX_ROOT="${STAR_INDEX_ROOT:-$PIPELINE_ROOT/STAR-index}"   # build-once indexes live here
REGISTRY_FILE="$SCRIPTS_DIR/star_index_registry.json"            # organism -> prebuilt index / refs

export FASTQ_INPUT_DIR BAM_OUT GENOME_DIR SJDB_GTF SJDB_OVERHANG RUNTABLE SCRATCH \
       THREADS SORT_RAM STAR_EXTRA_ARGS MEM_MB MEM_RUSAGE WALL LSF_QUEUE \
       JOB_TAG WATCHDOG_INTERVAL_MIN MAX_STALL_PASSES ABSOLUTE_MAX_PASSES MAX_WALL_HOURS \
       DELETE_FASTQ_AFTER_BAM CLEANUP_TOOLS_WHEN_DONE STRICT_BAM_CHECK MIN_MAPPED_FRAC VERIFY_FASTQ_DIRECT \
       STAR_MODULE SAMTOOLS_MODULE \
       ORGANISM STAR_INDEX_ROOT REF_FASTA_URL REF_GTF_URL \
       BUILD_THREADS BUILD_MEM_MB BUILD_MEM_RUSAGE BUILD_WALL REGISTRY_FILE \
       SCRIPTS_DIR PIPELINE_ROOT LOG_DIR SAMPLE_LIST

# Make the 'module' command available even in a non-login job shell.
star_init_modules() {
  command -v module >/dev/null 2>&1 && return 0
  local f
  for f in /etc/profile.d/modules.sh /etc/profile.d/lmod.sh \
           /usr/share/Modules/init/bash /usr/share/lmod/lmod/init/bash; do
    [ -r "$f" ] && . "$f" 2>/dev/null && command -v module >/dev/null 2>&1 && return 0
  done
  return 0   # no module system found -> assume the tools are already on PATH
}

# Load the configured tool modules (no-op if a MODULE var is empty / tool on PATH).
star_load_modules() {
  star_init_modules
  [ -n "$STAR_MODULE" ]     && module load "$STAR_MODULE"     2>/dev/null
  [ -n "$SAMTOOLS_MODULE" ] && module load "$SAMTOOLS_MODULE" 2>/dev/null
  return 0
}
export -f star_init_modules star_load_modules
