# SpliceScout

**Submit an NCBI GEO search query -> get cleaned, splicing-amenable, cell-line-grouped compound
tables, plus a download-ready SRA run list for the single best cell line and an optional one-click
handoff to an LSF cluster.**

SpliceScout automates a workflow that's otherwise done by hand: scrape GEO for a query, pull
per-sample structured metadata (cell line / treatment compound / read counts) and the library-prep
protocol from SRA, AI-clean the messy free text (canonical drug names, recovered cell lines,
drug-treated vs not), filter to library preps appropriate for the chosen **analysis module**, and emit
the tables. It then "deep-dives" the most promising cell line into the exact SRA Run Selector metadata
and a flat `SraAccList.txt` ready for `prefetch`.

With the **Bulk RNA-seq (STAR)** module + an autonomous cluster, it goes all the way: download the
reads, then **automatically STAR-align them to BAMs** on the cluster — and because that chain lives
entirely on the cluster, you can close SpliceScout afterward (downloads can take days). The analysis
module is a pluggable concept: it drives both the library-prep filter and the downstream aligner, so
more assays (single-cell, etc.) can be added later.

It's a single self-contained program with a local web UI (pure-Python stdlib HTTP server + inline
browser JS) and an equivalent command-line interface. No Node.js, no web framework, no database.

---

## Quick start

### Windows
Double-click **`launch_Win.bat`**. On first run it finds Python (installing the latest via `winget`
if missing), `pip install`s the dependencies, then starts the server and opens your browser.

### macOS
Double-click **`launch_Mac.command`** (in Finder). On first run it finds `python3` (installing via
Homebrew if present, else pointing you to python.org), installs the dependencies, then starts the
server and opens your browser.

> First time on a Mac you may need to make it runnable / clear the download quarantine:
> ```bash
> chmod +x launch_Mac.command
> xattr -d com.apple.quarantine launch_Mac.command   # only if Gatekeeper blocks it
> ```
> ...or right-click the file in Finder and choose **Open** once.

### Run directly (any OS)
```bash
pip install -r requirements.txt      # anthropic, openai, openpyxl, paramiko
python server.py                     # web UI on http://127.0.0.1:8765 (opens a browser)
python server.py --port 9000 --no-open
```

Requires **Python 3**. Dependencies (`requirements.txt`): `anthropic`, `openai` (also drives Gemini),
`openpyxl` (Excel workbook), `paramiko` (optional — only for SSH *password* auth on the autonomous
cluster upload; key/agent auth works without it). The launchers also fetch the vendored **Plotly**
library (`vendor/plotly.min.js`, for the Plots tab) on first run if it's missing.

### Running concurrent projects (multiple instances)
**Launch a launch file again — or run `python server.py` again — to start another instance.** Each
instance grabs its own free port (8765, 8766, 8767, …) and opens its own browser tab, so you can run
several projects at the same time. Each one is also assigned a cluster **`JOB_TAG`** of `sra1`,
`sra2`, `sra3`, … (shown as a badge in its UI header) so concurrent cluster downloads never collide.
Closing an instance frees its number for the next launch to reuse.

---

## What you provide

The single setup page (or the CLI) collects everything a run needs:

- **GEO search query** — Entrez syntax, e.g. `rna-seq[Description] AND human[Organism] AND drug`.
- **Scope** — scan **all** matching studies, or **cap at N** (start at ~25 to validate, then scale up).
- **AI provider + key** — Anthropic (Claude), OpenAI (ChatGPT), or Google Gemini — or tick
  **Skip AI cleaning** to run the deterministic stages only (no key needed). The OpenAI provider also
  accepts a **custom base URL**, so you can point it at any OpenAI-compatible host (MiMo, Qwen, a local
  vLLM/LM-Studio server, OpenRouter, …). A bad key or model name is caught up front and you're asked to
  fix it or turn AI off — it won't silently grind.
- **Analysis module** — currently **Bulk RNA-seq (STAR)**. The module sets which library-prep protocols
  pass the headline table *and*, on the cluster, which aligner runs. When chosen with an autonomous
  cluster, it adds a STAR genome-index field (leave blank to auto-resolve / build one by organism).
- **Deep-dive pick** — **auto** (top real cell line by # unique compounds, then total reads) or
  **manual** (the run pauses after the scan so you can choose from the ranked list).
- **Cluster handoff** — **off**, **download a bundle** (manual), or **autonomous** (upload + launch).
- *Advanced:* AI concurrency (up to 99), optional NCBI E-utilities API key (raises the rate limit
  3 → 10 req/s — and the metadata fetch is parallelized, so the key genuinely speeds it up).

Your entries (including API keys and cluster info) are saved locally at
`~/.geo_pipeline_settings.json` so they prefill next time. **That file is plaintext on your machine**
— delete it to wipe saved keys. Per-run `config.json` stores only non-secret settings (never API
keys or the SSH password).

---

## What you get

Outputs land in `runs/<query-slug>_<timestamp>/`:

| File | What it is |
|---|---|
| `tables/ncbi_final_splicing.csv` | **HEADLINE** — splicing-amenable, cell-line-grouped compound table |
| `tables/ncbi_final.csv` | same grouping across **all** protocols |
| `tables/ncbi_final_truseq.csv` | TruSeq-only subset |
| `tables/ncbi_protocol_audit.csv` | per-study library-prep classification |
| `runtable/SraAccList.txt` | **MAIN deep-dive output** — flat SRR list for the best cell line (`prefetch --option-file`) |
| `runtable/SraRunTable_<line>.csv` / `.xlsx` | filtered, drug/dose-annotated Run Selector table + Excel workbook |
| `runtable/by_study/<GSE>/SraAccList.txt` | per-study run lists (each study downloaded separately) |
| `runtable/drug_annotation_review.csv` | audit of the drug / dose / control / drug-treated calls |
| `runtable/cluster_bundle.zip` | ready-to-run LSF download bundle (when cluster handoff is on) |
| `runtable/star_bundle.zip` | ready-to-run STAR alignment bundle (Bulk RNA-seq module, cluster on) |

The cell-line tables carry a **three-way drug-treated** split: **Drug Treated / Not Drug Treated /
Undetermined**. On an autonomous cluster run, the cluster itself produces the **`.fastq.gz`** reads and
(Bulk RNA-seq module) the STAR **`.bam`** alignments + splice junctions — under your `PIPELINE_ROOT`.

---

## In the browser UI

The web UI's **Run** tab shows a live stepper while the pipeline runs. Two things to know:

- **Click any step** (the ⓘ next to its name) to open a panel explaining exactly what that stage does,
  with its inputs and outputs.
- A **Plots** tab appears once the deep dive has matched the cell line (after the *match cell-line names*
  step). It uses Plotly (vendored locally — works offline) and shows **only the picked cell line's runs**
  (sourced from the filtered run table, so no other cell lines leak in):
  - a **study list** (the picked line's studies) — click a study to chart it;
  - **read depth** and **spot length (avg read length)** per run, each a horizontal IQR box-with-dots;
  - a **custom plot** builder — pick X / Y / color variables and a chart type (box-with-dots, violin,
    scatter, bar, histogram, **heatmap**, **2D density**). Variables are the run table's fields: read
    depth, spot length, bases, drug, drug-treated, dose, instrument, platform, …

A **User Guide** link sits at the bottom of every page.

---

## How it works

A 16-stage pipeline, each stage checkpointed (resumable) in `pipeline_state.json`:

```
1  fetch            GEO esearch + esummary
2  extract          per-sample SRA metadata {cell line, treatments, reads} + library protocol (parallel)
3  prep             build the AI batches
4  ai_compounds     canonicalize drug/compound names           (skipped with Skip-AI)
5  ai_samples       classify cell line / sample type / treated  (skipped with Skip-AI)
6  merge            assemble the AI lookup maps
7  build            emit the module's headline table + reference tables
   --- deep dive: the single best cell line ---
8  select           pick the top real cell line (auto or manual)
9  runtable_fetch   full SRA XML for that line's studies (parallel)
10 runtable_build   byte-exact Run Selector reconstruction
11 cellline_match   AI disambiguation (A549 ~ A-549, excludes BEAS-2B) -> SraAccList.txt
12 runtable_annotate  drug / dose / control + 3-way drug-treated + Excel workbook
   --- cluster handoff (optional) ---
13 cluster_bundle   fill config.sh + per-study lists + zip
14 cluster_submit   (autonomous) upload over SSH + launch the download (./run_pipeline.sh)
   --- STAR alignment (Bulk RNA-seq module, autonomous cluster) ---
15 star_bundle      fill STAR config.sh pointed at the download's FASTQ + organism/index resolution + zip
16 star_submit      upload + arm a self-rescheduling launcher that runs STAR once the download finishes
```

**Library-prep filter (module-tied).** Each analysis module owns which protocols pass the headline
table. **Bulk RNA-seq** keeps full-length protocols (TruSeq / NEBNext / KAPA / total-RNA **and
Smart-seq**) and removes 3'-end methods (single-cell/nuclei, 10x/droplet, plate-seq, and bulk 3'-tag
such as QuantSeq / DRUG-seq / BRB-seq). Compound and cell-line cleaning come from the depositor's
structured metadata + AI canonicalization — never keyword-guessed from titles.

---

## AI cleaning

One `classify()` interface drives all three providers (`llm_providers.py`):

| Provider | Default model | API key (env) |
|---|---|---|
| Anthropic (Claude) | `claude-haiku-4-5` | `ANTHROPIC_API_KEY` |
| OpenAI (ChatGPT) | `gpt-5.4-nano` | `OPENAI_API_KEY` |
| Google Gemini | `gemma-4-31b-it` | `GEMINI_API_KEY` |

The model box is editable — type any model your account can access. Paste the key in the UI (held in
memory only, never written to `config.json`), set the env var, or tick **Skip AI cleaning**.

- **Custom OpenAI-compatible endpoint.** Pick the OpenAI provider and fill the **Base URL** field to run
  any OpenAI-format model on another host (MiMo `https://api.xiaomimimo.com/v1`, a local vLLM/LM-Studio
  server, OpenRouter, …). Blank = `api.openai.com`. The endpoint must support function/tool calls
  (most do; there's a JSON fallback for those that don't).
- **Preflight, fix-or-disable.** Before the long stages a one-item validation call checks the
  provider/model/key. If it's wrong (bad key, unknown model, …) the run **pauses** and lets you correct
  it or turn AI off — instead of failing 30 minutes in. A **rate-limit (429)** isn't treated as a
  misconfig: it auto-retries every 30 s and keeps going (lower concurrency to avoid them).

---

## The deep dive

After the tables are built, SpliceScout takes the **single best real cell line** all the way to a
download-ready run list:

- **Selection** considers only real cell lines (Sample Type "Cell line"), ranked by # unique
  compounds then total reads. Auto picks #1; manual pauses for your choice.
- The Run Selector "Metadata" table is **reconstructed byte-for-byte** from SRA XML (validated against
  the official export — run `python pipeline.py --validate-runtable`).
- A **disambiguation agent** decides which of the many cell-line spellings in the run table are the
  target line (`A549` ~ `A-549` ~ `A 549` ~ `A549 cells`, while excluding `BEAS-2B`). With Skip-AI it
  falls back to deterministic normalized-equality matching.
- `SraAccList.txt` (combined + per-study) is the main artifact for `prefetch`.

---

## Cluster handoff

Hands the per-study accession lists to an LSF download/convert pipeline (vendored in
`cluster_template/`), configured only through a generated `config.sh`. Three modes:

- **off** — no cluster step.
- **manual** — builds `cluster_bundle.zip` for you to download and run yourself.
- **autonomous** — uploads the bundle over SSH and runs `./run_pipeline.sh` on the cluster.

Each run is **isolated** in its own per-cell-line subfolder under `PIPELINE_ROOT` (e.g.
`/data/mylab/sra/A549`), so runs never mix. The bundle ships **per-study** `by_study/<GSE>/` lists
(never a single combined list) so each study is downloaded and converted independently.

The cluster **`JOB_TAG`** (which namespaces this project's LSF job names) is set automatically per
running instance — `sra1`, `sra2`, `sra3`, … — so two projects downloading at the same time on the
same cluster account don't clash. You can still override it in *Advanced cluster settings*.

**Cleanup on success.** When the cluster pipeline finishes, it deletes the transient clutter (job
logs, generated `.lsf` scripts, leftover `.sra`/temp files, empty folders, and — by default — even its
own scripts), leaving a clean **data-only** folder. It always keeps the `.fastq.gz` outputs,
`SraAccList.txt`, the `PIPELINE_COMPLETE.txt` report, and `watchdog.log`. This runs only on success
(never when stalled, so logs survive for debugging) and is controlled by `CLEANUP_ON_COMPLETE` /
`CLEANUP_SCRIPTS_ON_COMPLETE` in `config.sh`.

**If an autonomous upload fails, you don't re-run the pipeline.** SpliceScout reads the ssh/scp/remote
log and explains the cause (DNS, connection refused, timeout, auth, host-key, bad key file, wrong
submit host [e.g. `bsub: command not found`], no write permission on `PIPELINE_ROOT`, missing
paramiko, ...), then **pauses and asks for corrected SSH/cluster details** — a prefilled form in the
UI, or a prompt on the CLI. It rebuilds the (small) bundle and retries just the upload, looping until
it succeeds or you skip (the bundle stays downloadable either way). The results banner reports **what
actually happened** — uploaded & launched, or "grab the bundle and run it manually."

Re-trigger the upload for an already-finished run without redoing anything:
```bash
python pipeline.py --run-dir runs/<existing> --cluster-retry
```

**Check cluster progress on demand** — from the results banner after a launch, *and* from a **Check
cluster status** button at the top of the form (it appears whenever cluster settings are saved, so you
can check **even after closing and relaunching** the server). It SSHes to the submit host and finds the
running pipeline by **discovering its folder from this instance's live `sraN_*` LSF jobs** (so it works
with no active run / a fresh server), then reports, per study, how many runs are **downloaded** (`.sra`
fetched — including SRA-toolkit's per-accession subfolders) and **converted** (`.fastq.gz`) — so a study
still downloading no longer reads as 0 — plus overall percent, active-job count, and an **ETA that
sharpens with each check**. After the first check it **auto-refreshes every 2 minutes** (until the
pipeline completes/stalls), the check is **scoped strictly to this instance's jobs**, and for a Bulk
RNA-seq run it also shows the **STAR alignment** progress once the download finishes.

> The cluster scripts in `cluster_template/` are vendored from your own LSF pipeline; see
> [`cluster_template/DOWNLOAD_PIPELINE_GUIDE.md`](cluster_template/DOWNLOAD_PIPELINE_GUIDE.md) for what runs on the cluster.

---

## STAR alignment (Bulk RNA-seq module)

With the **Bulk RNA-seq** module and an **autonomous** cluster, SpliceScout adds two stages that turn
the downloaded reads into aligned BAMs — fully **auto-chained on the cluster**:

- It uploads a STAR bundle (vendored `star_template/`) configured to read the download's `*.fastq.gz`
  and merge runs of the same BioSample into one BAM (using the deep-dive's `SraRunTable`).
- It arms a **self-rescheduling LSF launcher** that waits for the download to finish, then runs STAR
  2-pass alignment → sorted, indexed `.bam` + `SJ.out.tab` splice junctions. Because the whole chain is
  LSF jobs on the cluster, **you can close SpliceScout right after the upload** — downloads can take
  days and STAR still fires itself when they complete.

**Genome index.** Fill the **STAR genome index** field with your prebuilt index path and it's used
directly. Leave it blank and SpliceScout resolves one by organism (auto-detected from the run table):
a registry (`star_index_registry.json`) → a previously built index → a one-time `genomeGenerate` build
job. Fill the registry's `organisms` entry (or the field) with your reference's index to skip the
~1–2 h build for the common case.

---

## Command-line usage

```bash
# interactive (prompts for query, cap, keys)
python pipeline.py

# unattended run
python pipeline.py --query "rna-seq[Description] AND human[Organism] AND drug" --cap unlimited --yes

# deterministic only, no API key
python pipeline.py --cap 25 --skip-ai --yes

# resume a stopped run
python pipeline.py --run-dir runs/<existing> --resume

# prove the Run Selector reconstruction is byte-exact, then exit
python pipeline.py --validate-runtable

# re-run ONLY the cluster upload for an existing run (asks for corrected info)
python pipeline.py --run-dir runs/<existing> --cluster-retry
```

**Flags:** `--query --cap (int|unlimited) --ncbi-key --provider anthropic|openai|gemini
--anthropic-key --openai-key --gemini-key --model --openai-base-url --concurrency --module
--run-dir --resume --skip-ai --yes`; deep-dive `--no-deep-dive --pick auto|manual --cell-line NAME
--validate-runtable`; cluster `--cluster-mode off|manual|autonomous --cluster-root PATH --ssh-host
--ssh-user --ssh-port --ssh-key --cluster-retry` (SSH password via `$CLUSTER_SSH_PASSWORD`); STAR
`--star-genome-dir --star-gtf --star-index-root --star-organism`.

---

## Project layout

```
launch_Win.bat / launch_Mac.command   one-click launchers (install deps, start the UI)
server.py            web front end (HTTP server + single-page UI + live progress/ETA)
pipeline.py          orchestrator — run_pipeline(cfg, P, reporter) is the shared 16-stage DAG
progress.py          thread-safe per-run progress / ETA / log + pause-for-input hooks
llm_providers.py     one classify() for Anthropic / OpenAI / Gemini
fetch_5000_ncbi.py   stage 1   structured_extract.py  stage 2   prep_ai.py        stage 3
ai_clean.py          stages 4-5  merge_ai.py           stage 6   build_final.py    stage 7
deepdive_select.py   stage 8   runtable_fetch.py       stage 9   runtable_build.py stage 10
cellline_match.py    stage 11  runtable_annotate.py    stage 12  cluster_deploy.py stages 13-14
build_final.py       stage 7 + the per-module library-prep filter (MODULES)
star_deploy.py       stages 15-16  STAR alignment handoff (Bulk RNA-seq module), auto-chained
normalize_v2.py / cell_utils.py   shared cleaning helpers
pipeline_paths.py    single source of truth for every output path
cluster_template/    vendored LSF download pipeline (only config.sh is regenerated per run)
star_template/       vendored STAR 2-pass alignment pipeline (consumes the download's fastq.gz)
star_index_registry.json   organism -> prebuilt STAR index / build-once reference URLs
runs/                output (one folder per query run)
```

---

## Notes & troubleshooting

- **No API key?** Tick *Skip AI cleaning* (or pass `--skip-ai`). Tables still build, but without
  canonical drug names / recovered cell lines.
- **Small caps give noisy picks.** A cap below ~10 studies can select a junk "cell line"; use 25+.
- **Large queries cost time and tokens.** Extraction fetches SRA metadata per study (rate-limited),
  so an NCBI API key helps; Using Gemma keeps AI cost low.
- **One run at a time.** The server runs a single pipeline; resume with `--run-dir ... --resume`.
- **Excel lock (Windows):** if a target CSV/XLSX is open in Excel, SpliceScout writes a `*_v2` copy.
