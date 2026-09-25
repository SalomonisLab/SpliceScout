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
reads, **STAR-align them to BAMs**, then **convert to AltAnalyze junction/exon BEDs** on the cluster — and because that chain lives
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
several projects at the same time. At launch the window **prompts you to name the instance** (e.g.
`A549`, `MDAMB231`); that name becomes its cluster **`JOB_TAG`** (shown as a badge in its UI header)
so concurrent cluster downloads never collide. Leave the name blank and it auto-picks the next free
`sra1`, `sra2`, `sra3`, … instead. Names are made cluster-safe automatically, and if two live
instances pick the same name the second gets a `-2` suffix. Closing an instance frees its name.

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
— delete it to wipe saved keys. Per-run `config.json` never stores AI API keys or the SSH password, but
it does record the **NCBI API key** (needed to resume) — keep `runs/` private.

---

## What you get

Outputs land in `runs/<query-slug>_<timestamp>_<instance>/`:

| File | What it is |
|---|---|
| `tables/ncbi_final_splicing.csv` | **HEADLINE** — splicing-amenable, cell-line-grouped compound table |
| `tables/ncbi_final.csv` | same grouping across **all** protocols |
| `tables/ncbi_final_truseq.csv` | TruSeq-only subset |
| `tables/ncbi_protocol_audit.csv` | per-study library-prep classification |
| `tables/skipped_no_sra.csv` | GEO studies with no SRA runs (processed-only / microarray / re-analysis) |
| `runtable/SraAccList.txt` | **MAIN deep-dive output** — flat SRR list for the best cell line (`prefetch --option-file`) |
| `runtable/SraRunTable_<line>.csv` / `.xlsx` | filtered, drug/dose-annotated Run Selector table + Excel workbook |
| `runtable/by_study/<GSE>/SraAccList.txt` | per-study run lists (each study downloaded separately) |
| `runtable/drug_annotation_review.csv` | audit of the drug / dose / control / drug-treated calls |
| `runtable/compound_funnel.tsv` | **where each compound went** — headline list → run table → replicated condition → same-study comparison (see *Why fewer compounds reach concordance*) |
| `runtable/cluster_bundle.zip` | ready-to-run LSF download bundle (when cluster handoff is on) |
| `runtable/{star,bed,psi,concordance}_bundle.zip` | ready-to-run bundles for the auto-chained cluster stages (Bulk RNA-seq module, cluster on) |

The cell-line tables carry a **three-way drug-treated** split: **Drug Treated / Not Drug Treated /
Undetermined**. On an autonomous cluster run, the cluster itself produces, under
`PIPELINE_ROOT/<instance>_<cell line>/`: the **`.fastq.gz`** reads, the STAR **`.bam`** alignments
(`STAR_bams/`), the AltAnalyze junction BEDs (`STAR_beds/`), the PSI / dPSI tables (`psi/output/`), and the
concordance results (`concordance/results/`: a ranked summary per cancer atlas, `significant_pairs.tsv`,
`all_scored_pairs.tsv`, `complete_drug_by_subtype.tsv`, `scored_pairs_with_null.tsv` and
`concordance_by_compound.tsv`).

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

A 22-stage pipeline, each stage checkpointed (resumable) in `pipeline_state.json`:

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
   --- Bulk RNA-seq module, autonomous cluster: each stage AUTO-CHAINS on the previous one ---
15 star_bundle      STAR config pointed at the download's FASTQ + organism/genome-index resolution + zip
16 star_submit      upload + arm a self-rescheduling launcher that runs STAR 2-pass once the download finishes
17 bed_bundle       AltAnalyze BAM->BED bundle (vendored toolkit + exon reference) + zip
18 bed_submit       arm a launcher that converts every BAM to junction/intron BEDs once STAR finishes
19 psi_bundle       comparison groups (each drug condition vs its OWN study's controls) + compound_funnel.tsv + zip
20 psi_submit       resolve AltAnalyze; once BEDs finish: junction prevalence filter -> ONE AltAnalyze PSI/dPSI job
21 concordance_bundle  cancer atlas for the cell line (cancer_atlas_registry.json) + vendored scorer + zip
22 concordance_submit  once PSI finishes: score every drug signature vs the atlas subtypes (analytic null + FDR)
```

**Run only part of the pipeline.** The START/END phase slider on the Run tab has 10 phases (Fetch · Extract ·
AI+tables · Select · Run table · Download · STAR · BAM→BED · PSI · Concordance). Starting later asks for the
artifacts the skipped phases would have produced (e.g. `cellline_selection.json`, a `by_study/` FASTQ folder).

**Library-prep filter (module-tied).** Each analysis module owns which protocols pass the headline
table. **Bulk RNA-seq** keeps full-length protocols (TruSeq / NEBNext / KAPA / total-RNA **and
Smart-seq**) and removes 3'-end methods (single-cell/nuclei, 10x/droplet, plate-seq, and bulk 3'-tag
such as QuantSeq / DRUG-seq / BRB-seq). Compound and cell-line cleaning come from the depositor's
structured metadata + AI canonicalization — never keyword-guessed from titles.

---

## AI cleaning

One `classify()` interface drives every provider (`llm_providers.py`):

| Provider | Default model | API key (env) |
|---|---|---|
| Anthropic (Claude) | `claude-haiku-4-5` | `ANTHROPIC_API_KEY` |
| OpenAI (ChatGPT) | `gpt-5.4-nano` | `OPENAI_API_KEY` |
| Google Gemini | `gemma-4-31b-it` | `GEMINI_API_KEY` |
| Ollama (local) | `llama3.1` | none (`OLLAMA_HOST` to point elsewhere) |

The model box is editable — type any model your account can access. Paste the key in the UI (it is saved
to the plaintext settings file above so it prefills, but never written to a run's `config.json`), set the env
var, or tick **Skip AI cleaning**. The compound pass asks the model to switch reasoning off (canonicalizing a
drug name is lookup, not reasoning); an endpoint that rejects those request fields is retried without them
automatically.

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

Each run is **isolated** in its own subfolder under `PIPELINE_ROOT` named **`<instance>_<cell line>`**
(the cell line normalized to lowercase letters/digits, e.g. instance `sra1` on MDS-L → `/data/mylab/sra/sra1_mdsl`;
an instance already named after its line is used as-is, so `A549` on A549 → `/data/mylab/sra/A549`). Every
stage (download → STAR → BED → PSI → concordance) and every re-run or phase-start of the same instance + line
shares ONE stable folder, and reusing one instance name for a different cell line gets its own folder instead
of clobbering the first. The normalization means `MDS-L` / `MDSL` / `MDS-L cells` all land in the same folder
however the AI spells the line. The resolved cell line is also recorded in a **`CELL_LINE.txt`** at the folder
root (`grep -H . …/*/CELL_LINE.txt` maps folders back to cell lines). The bundle ships **per-study**
`by_study/<GSE>/` lists (never a single combined list) so each study is downloaded and converted independently.

The cluster **`JOB_TAG`** (which namespaces this project's LSF job names) comes from the **instance
name you're prompted for at launch** (e.g. `A549`); leave it blank and it auto-picks the next free
`sra1`, `sra2`, `sra3`, … instead — so two projects downloading at the same time on the same cluster
account don't clash. You can still override it in *Advanced cluster settings*.

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
sharpens with each check**. The check is **scoped strictly to this instance's jobs** and runs
**on-demand** — click **Check cluster status** (or **Refresh now**) to update — and for a Bulk
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

**Then BAM → BED (AltAnalyze junction/exon).** After STAR finishes, a third auto-chained stage converts
each BAM into AltAnalyze BED files (the inputs for splicing analysis): `<sample>__junction.bed` always, plus —
per the **BED mode** — `__intronJunction.bed` (intron-retention, the default), `__exon.bed` (exon counts), or
both. It's **all-in-one**: the AltAnalyze BAM→BED scripts **and** the exon reference are *shipped with the
bundle* (vendored `bed_template/altanalyze/`), so the cluster needs **no AltAnalyze install** — just the stock
`python/2.7.5` (which provides `pysam`) + `samtools` modules. Like STAR it self-drives (reschedule-first
watchdog, idempotent, resubmits failures) and fires the instant STAR completes. Turn it off, pick the BED mode,
or set the species (auto-detected from the run's organism: Hs/Mm/Rn/Dr/Ss/Ma), under the Bulk RNA-seq options.

**Then AltAnalyze PSI.** Once the BEDs are done, one AltAnalyze job computes per-sample PSI and a **dPSI
comparison per drug condition**. Comparison groups are built from the annotated run table:
- **One group per drug condition** (drug × dose × time, within its study).
- **Each condition is compared with its OWN study's controls.** Only explicit vehicle/untreated samples count as
  controls; siRNA, knockout and infection samples are left out of both arms.
- **A single-replicate condition is pooled** with the same drug's other doses/timepoints *in the same study*
  rather than dropped.
- **Large cohorts get a junction prevalence filter first** (`JUNCTION_PREVALENCE_TAU`, default 1%): a junction
  is kept only if it appears in at least 1% of libraries. Without it, library-private artefacts swamp big pooled
  cohorts.

**Then drug-vs-cancer concordance.** Each drug's dPSI signature is scored against the splicing subtypes of the
cancer atlas mapped to the cell line (`cancer_atlas_registry.json`; e.g. A549 → TCGA LUAD + LUSC):
- **Score:** concordance **C** near 1 means the drug *mimics* the subtype; near 0 means it *reverses* it (a
  repositioning candidate).
- **Significance:** each pair is tested against its own analytic null — an exact binomial test on the shared
  events — with Benjamini–Hochberg FDR.
- **Cross-study contrasts are quarantined** (`STUDY_MATCHED_ONLY=1`): a drug arm compared with another study's
  controls is dominated by batch effects.
- **Per atlas** (`results/<atlas>/`):
  - `ranked_concordance_summary.txt` is a text report for reading. It lists significant reversal and mimic
    candidates (at most 3 subtypes per drug) and **every scored compound on its own line**.
  - `significant_pairs.tsv` holds the same significant pairs as a spreadsheet, with every row and no per-drug cap.
  - `all_scored_pairs.tsv` has every pair the scorer compared.
- **Across atlases** (`results/`): these are the files to share or open in Excel.
  - `complete_drug_by_subtype.tsv` has **every drug × every subtype**. Each row carries a `result`: significant
    reversal/mimic, not significant, below the 25-event overlap floor (not tested), or no shared events. Use it to
    confirm that a drug was compared against all subtypes.
  - `scored_pairs_with_null.tsv` has every tested pair with its null, p-value and FDR.
  - `concordance_by_compound.tsv` has one row per drug.
- **Atlas:** the lung atlas for A549 and other lung lines is TCGA LUAD
  (`ONCObrowser/Temporary/LUAD/DE_splicing_events/Events-dPSI_0.1_adjp`) plus LUSC
  (`ONCObrowser/Testing/LUSC/...`). An earlier A549 run scored against `TCGA_AML/Supervised_Analysis/{LUAD,LUSC}`,
  which holds AML subtype signatures.

### Why fewer compounds reach concordance than the headline count

The headline **# Unique Compounds** counts every distinct compound across *all* of the line's GEO samples. A
compound only reaches the concordance ranking if it passes every step below, and each step can lose some:
1. **It has SRA runs in the deep dive.** Some studies have no SRA data, and long-read / non-RNA-seq runs are
   filtered out.
2. **It is recognized per run.** Runs whose treatment can't be read end up Undetermined and are dropped.
3. **It has at least 2 BioSamples.** Replicates are counted after pooling doses/timepoints within the study.
4. **Its study has at least 2 recognized control samples.** Otherwise the only baseline is another study's
   controls, a batch-confounded comparison that is quarantined.
5. **Enough of its BEDs survive** STAR/BED on the cluster.
6. **AltAnalyze finds significant events for it**, and its signature shares enough events with a cancer subtype
   to be scored.

`runtable/compound_funnel.tsv` names the step each compound stopped at (steps 1–4). The cluster side adds
`psi/groups_attrition.tsv` (steps 5–6) and the per-compound section of the concordance summary.

### If whole studies are missing from the ranking

When the ranking draws on only a few studies, a whole *study* fell out at one of these points:
- **No drug call.** None of the study's samples was labelled drug-treated, so the study never gets a drug group.
  This is the biggest loss by far. On the A549 run, 557 of 766 studies had no drug call. Most of those really have
  no drug (siRNA, CRISPR, overexpression, virus infection, radiation). Some were drug studies whose treatment text
  was misread; see the 2026-09-18 fix below.
- **No usable controls of its own.** Fewer than 2 recognized control samples, so the study can only be compared
  cross-study, which is quarantined.
- **Lost at the BED step.** A comparison group dropped below 2 samples, so every comparison of that study is lost.
- **AltAnalyze never wrote its comparison files.**
- **Signatures too small to score.**

Two reports name the point for every study:
- `runtable/study_funnel.tsv`, written on your PC when the PSI bundle is built;
- the **STUDY COVERAGE** section at the end of every `ranked_concordance_summary.txt`
  (also `concordance/results/study_coverage.txt`).

For a run made before this report existed, copy `concordance_template/study_coverage.py` to the cluster and run
`python study_coverage.py --root <PIPELINE_ROOT>/<instance>_<cell line>`. Nothing is modified.

**Fixed 2026-09-18 — incomplete AltAnalyze runs were treated as finished.** The PSI stage used to count as finished
as soon as AltAnalyze wrote its per-sample table. That table is written before the per-comparison files, so a
crashed, killed or frozen AltAnalyze run was marked COMPLETE with only its first comparisons. Those belong to the
lowest-numbered studies, so concordance would rank just a couple of studies. PSI now finishes only when
AltAnalyze exits cleanly or every requested comparison file exists. `psi/PSI_COMPARISONS.tsv` lists each one, and
"Check cluster status" shows "N / M comparisons written".

**Fixed 2026-09-18: drug names cut off by the treatment-text cleaner.**
- **The bug.** When the dose came before the drug, as in "treated by 50 nM mitoxantrone for 48 hours", the cleaner
  kept only the text before the dose ("treated by"). The AI compound step only sees the cleaned text, so it could
  not name a drug, and every sample of the study was labelled not drug-treated.
  - "1 week 50nM CFI-400945 treated" was even cut to "1 week" and called a control.
- **The fix.** The cleaner now removes wording such as "treated with / by", "exposure to" and "for 48 hours" first,
  so the drug name survives.
  - Columns such as `treatment_2` or `exposure` are now read as treatment columns too.
- **New checks.**
  - `runtable/study_funnel.tsv` has a `treatments` column with each study's own treatment text.
  - The PSI bundle step prints a NOTE naming any study that has a treatment with a dose but no drug call.

**Applying this fix to a finished run.** A resume skips every stage already marked done, and the cluster launchers
stop at an existing `PIPELINE_COMPLETE.txt`. Downloads, STAR and BED are not affected and stay as they are.

1. **On the cluster, in the run folder:**
   - rename `psi/PIPELINE_COMPLETE.txt` and `concordance/PIPELINE_COMPLETE.txt` (for example, add `.old`);
   - move `concordance/results` aside.
2. **On the PC:** remove these entries from the run folder's `pipeline_state.json`:
   - `prep`, `ai_compounds`, `merge`, `build`, `runtable_annotate`
   - `psi_bundle`, `psi_submit`, `concordance_bundle`, `concordance_submit`
3. **Resume:** `python pipeline.py --run-dir <run folder> --resume`.

Only the AI batches that contain a newly cleaned name go back to the model.

**Re-running PSI on an older run's BEDs.** After a PSI run finishes, "compress when done" gzips the whole project
folder, BEDs included. The PSI stage now reads `*.bed.gz`: it unpacks private copies into its own
`psi/junction_beds/` folder and never changes the originals. It only includes samples listed in its
`sample_groups.tsv` (`PSI_GROUPED_BEDS_ONLY=1`), which keeps a re-run on a 6,000-library cohort to the samples that
are actually compared.

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

**Flags:** `--query --cap (int|unlimited) --ncbi-key --provider anthropic|openai|gemini|ollama
--anthropic-key --openai-key --gemini-key --model --openai-base-url --disable-reasoning --concurrency
--module --run-dir --resume --skip-ai --yes --start-stage STAGE --end-stage STAGE` (default: the whole
22-stage chain); deep-dive `--no-deep-dive --pick auto|manual --cell-line NAME --validate-runtable`; cluster
`--cluster-mode off|manual|autonomous --cluster-root PATH --ssh-host --ssh-user --ssh-port --ssh-key
--cluster-retry` (SSH password via `$CLUSTER_SSH_PASSWORD`); STAR `--star-genome-dir --star-gtf
--star-index-root --star-organism`. On a resume, `--concurrency` overrides the saved value. From Git Bash,
prefix `MSYS_NO_PATHCONV=1` so `--cluster-root /data/...` isn't rewritten into a Windows path (a mangled path is
also repaired automatically, with a warning).

---

## Project layout

```
launch_Win.bat / launch_Mac.command   one-click launchers (install deps, start the UI)
server.py            web front end (HTTP server + single-page UI + live progress/ETA + Assistant chat)
pipeline.py          orchestrator — run_pipeline(cfg, P, reporter) is the shared 22-stage DAG
progress.py          thread-safe per-run progress / ETA / log + pause-for-input hooks
llm_providers.py     one classify() / chat() for Anthropic / OpenAI / Gemini / Ollama
fetch_5000_ncbi.py   stage 1   structured_extract.py  stage 2   prep_ai.py        stage 3
ai_clean.py          stages 4-5  merge_ai.py           stage 6   build_final.py    stage 7
deepdive_select.py   stage 8   runtable_fetch.py       stage 9   runtable_build.py stage 10
cellline_match.py    stage 11  runtable_annotate.py    stage 12  cluster_deploy.py stages 13-14
build_final.py       stage 7 + the per-module library-prep filter (MODULES)
star_deploy.py       stages 15-16  STAR alignment handoff (Bulk RNA-seq module), auto-chained
bed_deploy.py        stages 17-18  AltAnalyze BAM->BED handoff, auto-chained after STAR
psi_deploy.py        stages 19-20  AltAnalyze PSI handoff: comparison groups + compound funnel report
concordance_deploy.py  stages 21-22  drug-vs-cancer-atlas concordance handoff
group_assign.py      optional user-defined comparison groups (the UI "Comparison groups" editor)
normalize_v2.py / cell_utils.py   shared cleaning helpers (dose / control / cell-line normalization)
pipeline_paths.py    single source of truth for every output path
cluster_template/    vendored LSF download pipeline (only config.sh is regenerated per run)
star_template/       vendored STAR 2-pass alignment pipeline (consumes the download's fastq.gz)
bed_template/        vendored AltAnalyze BAM->BED stage (incl. altanalyze/ toolkit + gzipped exon reference)
psi_template/        AltAnalyze PSI stage (+ junction_whitelist.py / whitelist_job.sh prevalence filter)
concordance_template/  concordance scorer + ranker + score_with_null.py (analytic null, BH-FDR, Mann-Whitney)
diagnose_ai/         optional on-cluster CPU LLM that diagnoses a STALLED stage and emails the cause
star_index_registry.json   organism -> prebuilt STAR index / build-once reference URLs
cancer_atlas_registry.json cell line -> cancer-subtype splicing atlas(es) for the concordance stage
manuscript/          Nature Methods draft + figure scripts        knowledge_graph/  code-structure graph
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
