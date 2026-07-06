# SRA → FASTQ automated pipeline (LSF)

Download a large set of SRA/ERR runs and convert them to compressed FASTQ on an
HPC cluster, **fully unattended**. You point it at a folder of accession lists,
run one command, and walk away. A self-driving "watchdog" recovers failures,
re-fetches anything missing, and writes a completion report when everything is
done.

Built from a real run that processed ~1,000 runs across ~40 studies. The hard-won
fixes (SRA-Lite handling, single-end FASTQ naming, slot-cap tuning, scratch
staging) are all baked in.

---

## What it does

For each *study* (a folder containing a `SraAccList.txt`):

1. **prefetch** all its accessions (one download job per study).
2. **convert** each downloaded `.sra` to `.fastq.gz` (one job per run): extract +
   compress on fast **scratch**, copy the final `.fastq.gz` to your **archive**
   (permanent), verify it byte-for-byte, then delete the source `.sra`.
3. A **watchdog** re-checks every 30 min: resubmits failed/orphaned conversions,
   re-fetches accessions SRA didn't deliver, and — when every run is converted —
   writes `PIPELINE_COMPLETE.txt` and stops. No babysitting.

Single-end (`<acc>.fastq`) and paired-end (`<acc>_1/_2.fastq`) runs are both
handled, as is SRA-Lite (`.sralite`) delivery.

---

## Requirements

- An **LSF** cluster (`bsub`, `bjobs`). Submit from the host where those work.
- **SRA Toolkit** (`prefetch`, `fasterq-dump`) — via an environment module or on `PATH`.
- A **scratch** filesystem (large, writable) — recommended; optional.
- `pigz` for parallel compression (optional; falls back to `gzip`).

> Not LSF? The job *bodies* (`prefetch_job.sh`, `fasterqdump_job.sh`,
> `convert_study.sh`) are scheduler-agnostic. To port to SLURM, replace the
> `bsub ...` calls in `lib.sh` + `watchdog.sh` + `run_pipeline.sh` with `sbatch`
> equivalents and swap `bjobs`/`busers` for `squeue`/`sacctmgr`.

---

## Quick start

1. **Lay out your input** under one folder, one subfolder per study, each with a
   `SraAccList.txt` (one accession per line):

   ```
   <PIPELINE_ROOT>/by_study/GSE123456/SraAccList.txt
   <PIPELINE_ROOT>/by_study/GSE789012/SraAccList.txt
   ...
   ```

2. **Edit `config.sh`** — the only file you normally touch (see next section).

3. **Copy this folder to the cluster** (somewhere under your archive is fine so
   compute nodes can read it), then from the LSF submit host:

   ```bash
   chmod +x *.sh
   ./run_pipeline.sh
   ```

   That's it. Check progress any time with `./status.sh` or
   `tail -f <PIPELINE_ROOT>/watchdog.log`. When `PIPELINE_COMPLETE.txt` appears,
   you're done.

---

## What to change for *your* cluster / directory / archive

All of it lives in **`config.sh`**:

| Setting | Change it to… |
|---|---|
| `PIPELINE_ROOT` | **Your archive path** — where the `.fastq.gz` are stored permanently and where `by_study/` lives. |
| `STUDIES_DIR` | Where your per-study folders are (default `$PIPELINE_ROOT/by_study`). |
| `SCRATCH_DIR` | **Your cluster's scratch volume** (large, writable, fast). Use `/scratch/$USER` or similar — **not** a node root fs. Set to `""` to process in-place on the archive if you have no scratch. |
| `SRATOOLKIT_MODULE`, `ASPERA_MODULE` | Your module names (e.g. `sratoolkit/3.0.0`). Set to `""` if the tools are already on `PATH`. |
| `LSF_QUEUE` | A specific queue, or `""` for the cluster default. |
| `THREADS` | Threads per conversion. **Tune to your slot cap** (see below). |
| `MEM_MB`, `WALL`, `PREFETCH_MEM_MB` | Per-job memory / wall-clock limits your cluster expects. |
| `WATCHDOG_INTERVAL_MIN` | How often the watchdog re-checks (default 30). |
| `JOB_TAG` | A short unique prefix for this project's LSF job names. Use a different tag per concurrent project. |

**Tuning `THREADS` (important).** Clusters cap how many job *slots* one user may
run at once (check with `busers $USER` → `MAX`). Each conversion uses `THREADS`
slots, so **max concurrent conversions = cap / THREADS**. Because `fasterq-dump`
is I/O-bound (little speedup above ~6–8 threads), **fewer threads per job =
more jobs at once = higher total throughput**. On a 125-slot cap, `THREADS=6`
(~20 parallel) beats `THREADS=16` (~7 parallel). Start at 6.

---

## Files

| File | Role |
|---|---|
| `config.sh` | **All settings.** Sourced by everything. Edit this. |
| `run_pipeline.sh` | One-command entry point: setup → launch → start watchdog. |
| `setup.sh` | One-time checks + SRA Toolkit `vdb-config` init. |
| `run_all.sh` | Launches prefetch + convert for every study (idempotent). |
| `prefetch_job.sh` | Job body: download one study's accessions. |
| `fasterqdump_job.sh` | Job body: convert one `.sra` (scratch → archive). |
| `convert_study.sh` | Job body: flatten downloads + submit conversions. |
| `fetch_missing.sh` | Targeted re-fetch of only the accessions still missing. |
| `watchdog.sh` | Self-driving controller; reschedules itself until done. |
| `status.sh` | One-shot progress report. |
| `lib.sh` | Shared submit helpers. |

Re-running `run_all.sh` or `./run_pipeline.sh` is safe — completed studies and
in-flight work are skipped (idempotent).

---

## How completion works

The watchdog stops and writes a report in `PIPELINE_ROOT`:

- **`PIPELINE_COMPLETE.txt`** — every accession has a `.fastq.gz`. Includes a
  per-study tally and total dataset size.
- **`PIPELINE_STALLED.txt`** — after repeated idle re-fetch passes some accessions
  still produced no FASTQ (typically withdrawn/restricted/embargoed on SRA). The
  report lists exactly which ones to check by hand.

---

## Gotchas already handled (FYI)

- **`vdb-config` init** — fresh accounts fail with "sra toolkit … not configured";
  `setup.sh` initializes it once (shared `~/.ncbi`, so all nodes inherit it).
- **`.sralite`** — `prefetch` sometimes returns SRA-Lite; the pipeline normalizes
  `.sralite → .sra` (fasterq-dump reads format from content, not extension).
- **Single-end naming** — single-end runs produce `<acc>.fastq` (no `_1`); the
  detection globs match both single- and paired-end so nothing is silently dropped.
- **Slot cap** — conversions are kept small (`THREADS`) to maximize concurrency.
- **Data safety** — a source `.sra` is deleted **only** after its `.fastq.gz` is
  confirmed on the archive; any failure keeps the `.sra` for a free retry.

### ⚠️ One operational warning
Never put a bare `0` in a `bkill` argument list — `bkill <id> 0` means **kill ALL
your jobs**. Cancel specific jobs by explicit id, or this pipeline's jobs by tag:
`bkill $(bjobs -noheader -o "jobid job_name" | awk '$2 ~ /^sra_/{print $1}')`.
