# Self-driving STAR alignment pipeline (template)

Point this at a folder of FASTQ files and an output location, run **one command**, and
walk away. It formats the FASTQs into a STAR sample list, submits one STAR 2-pass
alignment per sample on an LSF cluster, and a **watchdog drives it to completion** —
resubmitting anything that fails — then writes `PIPELINE_COMPLETE.txt` (or
`PIPELINE_STALLED.txt`) when it's done. No babysitting.

It is the alignment counterpart to the SRA-download pipeline in
`SpliceScout/cluster_template/` and follows the same philosophy: **`config.sh` is the
only file you edit.**

---

## 1. What it does (the flow)

```
FASTQ tree ──► make_sample_list.py ──► sample_list.tsv ──► one STAR job per row ──► <label>.bam
                  (format step)          (3-col TSV)         (run_star_job.sh)        + .bai
                                                                   ▲
                                                          watchdog.sh resubmits
                                                          failures every N min until
                                                          COMPLETE or STALLED
```

- **Format:** recursively scans `FASTQ_INPUT_DIR`, groups files into samples (paired vs
  single-end auto-detected per run), and — if you give it a run→BioSample table — merges
  multiple runs of the same biological sample into one alignment.
- **Align:** one STAR 2-pass job per sample, producing a sorted, indexed BAM.
- **Self-drive:** a watchdog re-checks on an interval, resubmits any sample whose BAM is
  missing/invalid, and stops itself when everything is done (or when a sample is
  genuinely stuck).

## 2. Quick start

```bash
# on the LSF SUBMIT host (e.g. bmiclusterp-head), with this folder copied over:
cd star_align_template
# 1) edit the EDIT-THESE block in config.sh (paths + resources) -- see section 4
nano config.sh
# 2) make scripts executable
chmod +x *.sh
# 3) launch -- runs unattended from here
./run_star_pipeline.sh
# 4) check in whenever you like (optional)
bash status.sh
tail -f <BAM_OUT>/watchdog.log
# done when <BAM_OUT>/PIPELINE_COMPLETE.txt appears
```

> **Strongly recommended:** do the section 9 smoke test (1 sample) before the first full launch.

## 3. Requirements

- An LSF cluster (`bsub`, `bjobs`) — run from the **submit** host.
- `STAR` (>= 2.7) and `samtools` available (via `module load` or already on `PATH`).
- `python3` >= 3.6 (the sample-list builder is pure standard library — no pandas/openpyxl).
- A prebuilt STAR genome index.

## 4. config.sh — the only file you edit

| Variable | What to put |
|---|---|
| `FASTQ_INPUT_DIR` | Folder with your FASTQs. Scanned **recursively**, so flat or nested (`by_study/<X>/*.fastq.gz`) both work. |
| `BAM_OUT` | Where results go. One `<label>.bam` (+`.bai`) per sample; logs + junctions in `BAM_OUT/logs/`. Use a volume with ~1-3 GB free per sample. |
| `GENOME_DIR` | Prebuilt STAR index dir (`STAR --runMode genomeGenerate ...`). |
| `SJDB_GTF` | Leave **empty** if the index was built with a GTF (usual case). Set a GTF path only if it wasn't. |
| `SJDB_OVERHANG` | Only used when `SJDB_GTF` is set (typically read length - 1, e.g. `100`). |
| `RUNTABLE` | *Optional.* A **CSV/TSV** with `Run` + `BioSample` (+ optional `LibraryLayout`) columns. Merges runs of one BioSample into one BAM. Empty = one BAM per run. **xlsx not supported — export to CSV.** |
| `SCRATCH` | Fast, **reliable** workspace for staging + STAR temp (not a flaky/full archive). |
| `THREADS` | STAR threads **and** the LSF slot request (`-n`) — kept equal. |
| `SORT_RAM` | `--limitBAMsortRAM` in **bytes**. Keep well under `MEM_MB`. |
| `STAR_EXTRA_ARGS` | Raw extra args appended to the STAR command (power users). |
| `MEM_MB` | LSF `-M` per job. Account for the ~29 GB GRCh38 genome load + sort + headroom. |
| `MEM_RUSAGE` | `rusage[mem=]` per slot; x `THREADS` ~= total reserved. |
| `WALL` | LSF `-W` walltime per job (`HH:MM`). |
| `LSF_QUEUE` | Empty = default queue; else e.g. `normal`/`long`. |
| `JOB_TAG` | Prefix that namespaces this run's LSF job names. **Use a unique tag per concurrent run.** |
| `WATCHDOG_INTERVAL_MIN` | How often the watchdog re-checks (e.g. 30). |
| `MAX_STALL_PASSES` | Consecutive no-progress passes before giving up (`STALLED`). |
| `STAR_MODULE`, `SAMTOOLS_MODULE` | Module names, or `""` if already on `PATH`. |

Everything below `END EDIT THESE` is derived automatically — don't touch it.

## 5. What you get (output layout)

```
$BAM_OUT/
  <label>.bam   <label>.bam.bai        # one verified, sorted BAM per sample
  sample_list.tsv                       # the generated list (fixed once the run starts)
  PIPELINE_COMPLETE.txt | PIPELINE_STALLED.txt
  watchdog.log   .watchdog.state(.stall)
  logs/
    <label>.SJ.out.tab                  # STAR splice junctions  (kept for splicing analysis)
    <label>.Log.final.out               # STAR alignment-rate QC
    star_<label>.out  star_<label>.err  # the LSF job's stdout/stderr
    watchdog.out  watchdog.err
```

## 6. The sample list & the runtable

`sample_list.tsv` is a 3-column, tab-separated file with **absolute** paths:

```
<label>	<R1[,R1...]>	<R2[,R2...]>     # paired-end
<label>	<R1[,R1...]>	NA               # single-end (NA in column 3)
```

- **Mate detection (per run):** `_R1/_R2`, `_1/_2`, or a bare `<run>.fastq.gz` (single-end).
  A study mixing paired and single runs is handled correctly.
- **With a `RUNTABLE`:** label = BioSample; all runs of one BioSample are comma-joined onto
  one row -> one BAM per biological sample. Column names are auto-detected
  (case-insensitive): Run (`run`/`run_accession`/`accession`/`srr`), BioSample
  (`biosample`/`sample_name`/`sample_accession`/...), LibraryLayout (optional).
- **Without a `RUNTABLE`:** every run becomes its own sample (no merge).
- **Side reports** (written next to `sample_list.tsv`; nothing is silently dropped):
  `.orphans` (a mate missing on disk), `.unmapped` (a run with no BioSample in the table —
  still emitted, labelled by its run accession), `.mixed` (a run with conflicting files, or
  a BioSample mixing paired+single).

Build/inspect it by hand if you want:
```bash
python3 make_sample_list.py --input-dir <FASTQ_DIR> [--runtable table.csv] --out /tmp/list.tsv
python3 make_sample_list.py --inspect-runtable --runtable table.csv   # show detected columns
```

## 7. How "done" is decided

- A sample is **done** when its BAM exists **and** `samtools quickcheck` passes. The job body
  and the watchdog use the *same* test, so they never disagree.
- **COMPLETE:** every row done **and** no live work jobs -> `PIPELINE_COMPLETE.txt`, watchdog stops.
- **STALLED:** the watchdog resubmits a missing sample every pass; if the `done` count doesn't
  move for `MAX_STALL_PASSES` consecutive idle passes, it writes `PIPELINE_STALLED.txt` listing
  the stuck labels (with a pointer to each `logs/star_<label>.err`) and stops — so a genuinely
  bad sample converges instead of looping forever.

## 8. Gotchas already handled

- **Single-end safe:** `NA` is never passed to STAR.
- **Flaky/over-full filesystems:** each sample's FASTQs are staged to a roomy local workspace
  (`$TMPDIR` -> `$SCRATCH` -> `BAM_OUT` volume) with `cp`+`gzip -t` **retry**; if there isn't
  room it reads direct and a resubmit fixes any one-off read error. STAR temp never lands on a
  full archive. (A bulk `gzip -t` pre-scan on a flaky volume gives false failures — that's why
  the retry lives in the job, not a pre-flight.)
- **Idempotent:** finished BAMs are skipped, so re-running `submit_all.sh`/`run_star_pipeline.sh`
  only does what's left. A half-written BAM fails quickcheck and is redone.
- **Right-sized resources:** `THREADS == -n`, and `SORT_RAM` is kept under `MEM_MB` (a common
  STAR OOM cause).
- **No redundant GTF:** if the index already has the GTF, it isn't re-supplied at align time
  (saves RAM); set `SJDB_GTF` only if your index lacks it.
- **Long/odd sample labels:** LSF job names are sanitised + length-capped (with a stable hash),
  while logs keep the raw label.
- **Stable target:** the sample list is built **once**; new FASTQs appearing mid-run won't move
  the goalposts (delete `sample_list.tsv` and rerun to intentionally add samples).

## 9. Smoke test (do this once before the first full run)

```bash
bash setup.sh                       # everything should be OK/green
bash build_sample_list.sh           # inspect sample_list.tsv + the .orphans/.unmapped/.mixed reports
# align ONE sample to confirm the STAR command + resources are right:
head -1 <BAM_OUT>/sample_list.tsv > /tmp/one.tsv
SAMPLE_LIST=/tmp/one.tsv bash submit_all.sh
bjobs -J "<JOB_TAG>_star_*"; tail -f <BAM_OUT>/logs/star_*.err
# verify the BAM, index, junctions, and QC log exist:
samtools quickcheck <BAM_OUT>/<label>.bam && ls <BAM_OUT>/logs/<label>.SJ.out.tab
```
If that one sample produces a valid BAM, launch the whole set with `./run_star_pipeline.sh`.

## 10. Operating it

- **Status:** `bash status.sh` (done/total, live jobs, real failures, free disk).
- **Logs:** `tail -f <BAM_OUT>/watchdog.log`; per-sample `logs/star_<label>.err`.
- **Stop everything:** `bkill -J "<JOB_TAG>_*"` (kills the work jobs and the watchdog).
- **Resume / mop up failures:** just re-run `./run_star_pipeline.sh` (or `bash submit_all.sh`) —
  done samples are skipped.
- **Add more FASTQs later:** drop them under `FASTQ_INPUT_DIR`, `rm <BAM_OUT>/sample_list.tsv`,
  then re-run `./run_star_pipeline.sh`.
- **Line endings (if copied from Windows):** ensure LF — `sed -i 's/\r$//' *.sh` or `dos2unix *.sh`.

## 11. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `setup.sh` says STAR/samtools/bsub missing | Set `STAR_MODULE`/`SAMTOOLS_MODULE`, or run on the submit host. |
| Jobs EXIT immediately, `.err` mentions memory | Lower `SORT_RAM` and/or raise `MEM_MB`; ensure `SORT_RAM` < `MEM_MB`. |
| `.err` says "no workspace with >= N G free" | `SCRATCH` (and the BAM_OUT volume) are full — free space or point `SCRATCH` elsewhere. |
| `STALLED` with some labels listed | Read those `logs/star_<label>.err` — usually a truncated FASTQ or persistent OOM. |
| Single-end runs not aligning | They should show `NA` in column 3 of `sample_list.tsv`; the job drops R2 automatically. |
| Runtable ignored | Check `setup.sh`'s "runtable column mapping"; ensure `Run` + `BioSample` headers exist. |

## 12. File reference

| File | Role |
|---|---|
| `config.sh` | **The only file you edit** — all settings. |
| `run_star_pipeline.sh` | One command: setup -> build list -> submit -> start watchdog. |
| `setup.sh` | Preflight checks (paths, tools, genome, scratch, memory sanity). |
| `make_sample_list.py` | FASTQ tree -> 3-col sample list (+ side reports). Pure stdlib. |
| `build_sample_list.sh` | Wrapper around the builder; build-once gate. |
| `run_star_job.sh` | Per-sample 2-pass STAR job body (staging, verify, publish). |
| `submit_all.sh` | One `bsub` per sample row (idempotent). |
| `watchdog.sh` | Self-driving controller (resubmit / complete / stall / reschedule). |
| `status.sh` | On-demand progress report. |
| `lib_star.sh` | Shared LSF + accounting helpers. |

## 13. Later: folding into SpliceScout

This is intentionally standalone. To integrate it as the stage after SpliceScout's FASTQ
download, drop this folder in as a sibling of `cluster_template/` and have a deploy step fill
`config.sh` the way `cluster_deploy.py::fill_config` fills the download `config.sh`
(`FASTQ_INPUT_DIR` = the download run's `by_study` root, `RUNTABLE` = the run's SraRunTable
exported to CSV). The handoff is clean: download writes `*.fastq.gz`, this reads them.
