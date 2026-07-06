# SpliceScout — Complete Handoff

**Read this first in a new chat.** It's the map + design decisions + gotchas + status. Full source lives in
`C:\Users\krog5w\.gemini\antigravity\scratch\SpliceScout\` — `Read` the modules for detail; this doc tells you
where things are and what NOT to break.

**The four docs (renamed for clarity):** `USER_GUIDE.md` (how to install & run — the end-user readme,
served in the UI at `/readme`), `DEVELOPER_GUIDE.md` (this file — architecture, gotchas, status),
`cluster_template/DOWNLOAD_PIPELINE_GUIDE.md` (the vendored SRA→FASTQ download pipeline), and
`star_template/ALIGNMENT_PIPELINE_GUIDE.md` (the vendored STAR alignment pipeline).

---

## 1. What it is

A standalone, parameterized program: **submit an NCBI GEO query → get cleaned, cell-line-grouped compound
tables**, then **deep-dive the single best cell line** into a download-ready SRA run list, and (optionally)
**hand off to the CCHMC LSF cluster** to download the reads and **STAR-align** them.

- Pure-Python stdlib web UI (no Node, no framework, no DB) + an equivalent CLI; both call the same
  `pipeline.run_pipeline`.
- AI cleaning is multi-provider (**Anthropic / OpenAI / Gemini**) behind one `llm_providers.classify()`; the
  OpenAI provider also takes a custom base URL for any OpenAI-compatible host (MiMo/Qwen/local/OpenRouter).
- **Analysis modules** make it extensible beyond bulk RNA-seq: the chosen module drives both the library-prep
  filter and the cluster aligner. Only **`bulk_rna_seq` (STAR)** exists today; the framework is built for more.

---

## 2. Run it

**Web UI:** double-click `launch_Win.bat` (Windows) or `launch_Mac.command` (macOS) — finds/installs Python,
`pip install -r requirements.txt` (anthropic, openai, openpyxl, paramiko; + vendored Plotly), starts the server,
opens the browser. Or `python server.py` (`--port N --no-open`). One run at a time per instance; **launch again
for a concurrent project** (each gets its own port + `sraN` job tag). The single page collects everything; a
live stepper shows progress/ETA/log; outputs are downloadable when done.

**CLI (`pipeline.py`):**
```
python pipeline.py                                         # interactive
python pipeline.py --query "..." --cap unlimited --yes     # unattended
python pipeline.py --cap 25 --skip-ai --yes                # deterministic only (no key)
python pipeline.py --run-dir runs/<existing> --resume      # resume
python pipeline.py --validate-runtable                     # prove the reconstruction (hits NCBI), exit
python pipeline.py --run-dir runs/<existing> --cluster-retry   # re-do ONLY the cluster upload
```
**Flags:** base `--query --cap (int|unlimited) --ncbi-key --provider anthropic|openai|gemini
--anthropic-key --openai-key --gemini-key --model --openai-base-url --disable-reasoning --concurrency --module --run-dir --resume
--skip-ai --yes`; deep-dive `--no-deep-dive --pick auto|manual --cell-line NAME --validate-runtable`; cluster
`--cluster-mode off|manual|autonomous --cluster-root PATH --ssh-host --ssh-user --ssh-port --ssh-key
--cluster-retry`; STAR `--star-genome-dir --star-gtf --star-index-root --star-organism`. SSH password via
`$CLUSTER_SSH_PASSWORD`.

**Output** → `runs/<slug>_<timestamp>_<sraN>/`: `tables/ncbi_final_splicing.csv` (HEADLINE),
`runtable/SraAccList.txt` (deep-dive MAIN output, for `prefetch --option-file`), `SraRunTable_<line>.csv`/`.xlsx`,
`by_study/<GSE>/SraAccList.txt`, `cluster_bundle.zip`, `star_bundle.zip`.

---

## 3. File inventory

| File | Role |
|---|---|
| `server.py` | Web front end — stdlib HTTP server; setup form + live progress/ETA dashboard; **Assistant** chat tab + `/api/chat`; runs `pipeline.run_pipeline` in a thread, tees stdout→log, serves outputs; instance slot/port; all `/api/*` endpoints. **STALL-alert email**: `#alertemail` field + Send-test button → `settings.alert_email`; `_alert_poller` daemon (20-min `remote_alerts` scan → `send_alert_email` on new stalls, deduped in `~/.geo_pipeline_alert_seen.json`); `/api/alert_test`; cluster-status panel shows a RED alert banner. **UI = "SpliceSCOUT" (rebranded 2026-06-24; MINIMALIST FUTURISTIC, matte black)**: the `PAGE` string holds the inline `<style>` — matte-black flat palette (`:root`, `--bg:#0a0a0b`), ONE restrained muted-teal accent `--accent:#5fb6c0` (`--accent2`==`--accent`, no purple/gradient), hairline borders, NO neon/glow/gradient/blur. Google fonts Orbitron (`.brand` wordmark — "Splice" txt + "SCOUT" accent) / Exo 2 (body) / Share Tech Mono (`.tagline`/`.ibadge`); the `MINIMALIST FUTURISTIC` block at the end of `<style>` flat-overrides the base rules (solid accent fills, flat panels). The 🧬 DNA canvas (`#dnafx`) is desaturated to a faint monochrome texture (`ctx.filter='grayscale(1)…'`). `PAGE` is a RAW string (`r"""`) so CSS unicode escapes use a SINGLE backslash. Restart the server to pick up PAGE edits. (User pref: matte-black minimalist, no neon/gradients/purple, keep the fonts.) |
| `tray.py` | Windows system-tray launcher (pythonw, pystray/Pillow; falls back to console `server.main()`) |
| `progress.py` | `RunReporter` (thread-safe progress + ETA + log; `snapshot()` for the UI; pause/await hooks for selection / cluster-fix / AI-fix) + `NULL` no-op reporter |
| `pipeline.py` | **Orchestrator** — `run_pipeline(cfg, P, reporter, ...)` is the shared 22-stage DAG (CLI `main()` + server both call it); `RunConfig` → config.json; resume via pipeline_state.json |
| `fetch_5000_ncbi.py` | Stage 1 — esearch+esummary GEO → ncbi_raw.json |
| `structured_extract.py` | Stage 2 — GEO→SRA elink+efetch → per-sample {cell_line, treatments_raw, spots} + study_protocol.json, **parallel** behind a thread-safe NCBI pacer |
| `prep_ai.py` | Stage 3 — `build_batches(P)` → compound/sample batches + sample_index.json |
| `llm_providers.py` | provider abstraction — `classify()` (structured), `chat()` (general tool-calling for the Assistant), `make_client(provider, max_retries, timeout, base_url)`, `classify_ai_error`, `MODELS`/`KEY_ENV`/`DEFAULT_MODEL` |
| `chat_assist.py` | **Assistant** brain — `run_turn(messages, settings, save_settings, instance_tag)` agentic model↔tool loop + system prompt + 15 read-mostly tools (get/update_settings, test_geo_query, list_runs, get_run_status, read_local_log, **list_data**, run_sql, **make_chart**, **data_funnel**, fetch_cluster_log, **cluster_health** [STALL scan], explain, skipped_studies). Prepare-only; secrets redacted; SELECT-only SQL. Charts ANY run data (not just the cell-line table) via `run_data`+`chart_engine` |
| `run_data.py` | Assistant **universal data layer** — `build_db(run_dir)`/`query()`/`inventory()`/`funnel_rows()`: loads EVERY artifact into in-memory SQLite as queryable tables (`studies`←ncbi_raw, `study_protocol`, `samples`←structured_samples.jsonl, `pipeline_stages`←progress, synthesized `data_funnel`, + every tables/* & runtable/* CSV). Read-only |
| `chart_engine.py` | Assistant **universal Plotly builder** — `build_figure(rows, spec)` turns any row-dicts+spec into a `{data,layout}` figure: bar/hbar/line/area/scatter/histogram/box/violin/pie/funnel/waterfall/heatmap, auto numeric-detect + optional agg. Pure/stdlib |
| `ai_clean.py` | Stages 4-5 — `run_pass()` (resumable, concurrency-capped). `_process_unit` recovers the MOST samples from a flaky model: jittered-retry → **reasoning-OFF auto-retry on an empty reply** (the usual cause of "no output"; reverts if the endpoint rejects it) → **SPLIT the batch in half on persistent incompleteness** (smaller requests don't truncate) → **SALVAGE at the floor** (keep every classified sample, `Unknown`-fill only the rest). A batch the PROVIDER can't answer at all (`_HardFail`) is dropped to Unknown so the run continues, UNLESS >`DROP_CEILING` (~10%) fail = provider-down → RAISE for resume. `dropped_batches.json` audits drops; per-model `max_tokens`; `preflight()` |
| `merge_ai.py` | Stage 6 — glob-based merge → compound_map.json, sample_map.json |
| `build_final.py` | Stage 7 — `build_all(P, module=)` emits tables + protocol audit + `cellline_index.json` + **`skipped_no_sra.csv`** (`skipped_no_sra(P)`: studies fetched from GEO but with 0 SRA runs — microarray/processed-only/re-analysis — i.e. why a GEO study can show 0 downloadable samples); `MODULES` = the per-module library-prep filter. **`_merge_variants` collapses cell-line spelling variants (`MDS-L`==`MDSL`, `A549`==`A-549`) in the ranking + index BEFORE writing (`_cl_norm` = lowercase-alnum), keeping the most-sampled spelling — so one line never splits into two rows (the deep-dive consolidate only merges AFTER selection)** |
| `deepdive_select.py` | Stage 8 — `consolidate()` merges cell-line NAME variants (deterministic → cellline_merge.json) FIRST, then rank/select the best REAL cell line → cellline_selection.json |
| `runtable_common.py` | ported NCBI E-utilities client (validated path); `configure(ncbi_key)`; thread-safe `_throttle` |
| `runtable_fetch.py` | Stage 9 — full SRA XML per selected study (parallel) → runtable/xml_cache/ (+ENA fallback). NCBI intermittently splices an HTTP-502 HTML error page INTO a large study's XML stream (a 5000+ experiment study can pick up several) → `runtable_common.clean_sra_xml` strips them (lossless; salvages complete `EXPERIMENT_PACKAGE`s if a record was truncated), applied in `efetch_sra_full`/`efetch_sra_xml` AND at the `runtable_build` cached-read |
| `runtable_build.py` | Stage 10 — byte-exact Run Selector reconstruction → SraRunTable_all.csv + match_candidates.json; `validate()` |
| `cellline_match.py` | Stage 11 — AI disambiguation agent (A549≈A-549, drops BEAS-2B); skip-ai = deterministic; hybrid GSM∪value keep → **SraAccList.txt** + filtered CSV. Splicing module (`assay_keep={"rnaseq"}`) also arms `_splice_drop_reason`: a RUN-level gate dropping non-RNA-Seq strategy, **long-read (Nanopore/PacBio — STAR-incompatible)**, non-splicing LibrarySelection (CAGE/RACE/size-frac), and single-cell stragglers |
| `runtable_annotate.py` | Stage 12 — drug/dose/is_control + 3-way `drug_treated` on the filtered table + `.xlsx` |
| `cluster_deploy.py` | Stages 13-14 — `build_bundle` + `submit_over_ssh`; `diagnose_failure` + fix/retry; `remote_status`/`remote_star_status`/`remote_bed_status`/`remote_psi_status` (on-demand progress over SSH) + **`remote_alerts`** (ONE `find` for any stage's STALLED/ORPHANED/LAUNCH_TIMEOUT marker across all runs → the silent-stall heads-up) + **`send_alert_email`** (emails via the cluster's own `mail`, base64 body, no PC SMTP); reads `cluster_template/`. `fill_config`/`_shval` + `_submit_systemssh`/`_submit_paramiko` reused by star/bed/psi_deploy |
| `cluster_template/` | Vendored LSF **download** pipeline (source of truth: `Downloads/SRA_pipeline_template/`); only config.sh is regenerated. **Drop-after-`MAX_FAILS` (=3):** `lib.sh` tracks per-accession attempts in `$PIPELINE_ROOT/.attempts/<acc>.n`; `fetch_missing.sh` (download) + `watchdog.sh` §1 (conversion) bump it and, past 5 fails, `sra_drop_acc` writes a `.dropped` marker + appends to `dropped_accessions.txt` + clears the stranded `.sra`; the completion gate counts `total_done + dropped` so one undeliverable run can't stall the study forever (user 2026-06-17) |
| `star_deploy.py` | Stages 15-16 (bulk_rna_seq) — `build_star_bundle` + `submit_star_over_ssh` (self-rescheduling launcher); `detect_organism` |
| `star_template/` | Vendored STAR 2-pass aligner (consumes the download's fastq.gz). Adds `lib_index.sh`/`resolve_index.sh`/`build_star_index.sh` for genome-index resolution + build-once. **Drop-after-`STAR_MAX_FAILS` (=3)** (2026-06-22): a sample that fails alignment 3× (or whose FASTQ is gone) is dropped → `star_dropped.txt`, completion = `done_n+dropped_n>=exp_n`, + a >10% meltdown→STALL guard — parity with BED/download |
| `star_index_registry.json` | organism → prebuilt index path (`organisms`, read on the cluster) + build-once FASTA/GTF URLs (`reference_urls`, read in Python) |
| `bed_deploy.py` | Stages 17-18 (bulk_rna_seq) — `build_bed_bundle` + `submit_bed_over_ssh` (self-rescheduling launcher waits on STAR); `_upload_ref_idempotent`; `organism_to_species` |
| `bed_template/` | Vendored BAM→BED (AltAnalyze junction/exon) stage incl. the `altanalyze/` toolkit + ~100 MB exon ref. **Exon-OPTIONAL in `BED_MODE=both`** (2026-06-22): `run_bed_job.sh` drops a truncated `__exon.bed` but still publishes junction+intronJunction, and `bed_done`'s `both` case no longer requires exon (PSI uses junction+intronJunction; the exon pass OOM-truncates on huge BAMs). **Drop-after-`BED_MAX_FAILS` (=3):** per-sample attempts in `$PIPELINE_ROOT/.attempts/<label>.n`; `watchdog.sh` drops a BAM that fails conversion 3× (or is BAM-gone) → `bed_dropped.txt`, completion gate = `done_n + dropped_n >= exp_n`, so a few bad BAMs can't STALL the stage. (BED `finalize()` does NOT kick PSI — `psi_launch.sh` polls for BED's `PIPELINE_COMPLETE.txt`.) |
| `psi_deploy.py` | Stages 19-20 (bulk_rna_seq) — `build_psi_bundle` + `submit_psi_over_ssh` (ONE AltAnalyze job; **find-or-upload** AltAnalyze; writes `sample_groups.tsv` from the run table); launcher waits on BAM→BED |
| `psi_template/` | Vendored AltAnalyze splicing (PSI) stage — **single-job** self-driving watchdog; `build_groups.sh` builds groups.txt/comps.txt cluster-side (shipped map ∩ present BEDs). AltAnalyze.py itself is NOT shipped (resolved on the cluster) |
| `concordance_deploy.py` | Stages 21-22 (bulk_rna_seq) — `build_concordance_bundle` + `submit_concordance_over_ssh`; auto-selects a cancer atlas from the cell line (`cancer_atlas_registry.json`), reads `concordance_template/`; launcher waits on PSI |
| `concordance_template/` | Vendored splicing-concordance stage — **single-job** self-driving watchdog; the VENDORED `splicingConcordance_advanced.py` scorer + `rank_concordance.py` ranker score per-drug PSI signatures vs cancer-subtype atlases (AML-OncoSplice / LUAD+LUSC) → ranked reversal candidates. Reuses the PSI-resolved AltAnalyze on PYTHONPATH |
| `cancer_atlas_registry.json` | cell line → cancer atlas (query dirs + patient-count source) for the concordance stage; GUI `cancer atlas` field overrides |
| `group_assign.py` | Phase B — user-defined comparison groups: deterministic (keyword on the full row + `is_control`/compound map) then AI for the rest → writes the **additive** `group` column + `group_assignment_audit.csv` |
| `plot_data.py` | Plots-tab data — per-RUN dataset from the picked line's filtered run table; dynamic numeric/categorical field lists |
| `stage_docs.py` | `STAGE_DOCS` for the UI's clickable step-doc modal (injected as `__STAGE_DOCS__`) |
| `vendor/plotly.min.js` | vendored Plotly (offline; served at `/plotly.js`, lazy-loaded) |
| `normalize_v2.py` | shared dose/control normalizer (`clean_compound`/`normalize_compound`/`is_control`) — the ONE signed file. `is_control` also catches `STRONG_CONTROL_TOKENS` (mock/uninfected/sham/untreated/parental/…) when underscore/hyphen-joined to a model prefix (e.g. `SARS2_Mock`), which the residue logic missed; solvent words (DMSO/vehicle) stay residue-based so a drug-in-DMSO isn't mislabeled control |
| `cell_utils.py` | shared `clean_struct_cell`/`extract_cell_line` |
| `pipeline_paths.py` | `Paths(run_dir)` — single source of truth for every output path |

Deep-dive modules were ported from `Downloads/GEO_SRA_Metadata_Pipeline/` (its 01/02/04 + common.py); the
`--validate-runtable` harness proves the port is byte-exact vs the official SRP189165 export.

---

## 4. The 22-stage DAG (each checkpointed in pipeline_state.json)

```
1  fetch            -> ncbi_raw.json, unique_titles.json
2  extract          -> structured_samples.jsonl, structured_done.json, study_protocol.json   (PARALLEL)
3  prep             -> ai_work/{compound_batches,sample_batches}/, sample_index.json
4  ai_compounds     -> ai_work/compound_results/*.json     (skipped with --skip-ai)
5  ai_samples       -> ai_work/sample_results/*.json        (skipped with --skip-ai)
6  merge            -> compound_map.json, sample_map.json
7  build            -> tables/ncbi_final{_splicing,_truseq,}.csv/.md, ncbi_protocol_audit.csv, cellline_index.json
   --- DEEP DIVE (skipped with --no-deep-dive; skips gracefully if no real cell line) ---
8  select           -> cellline_merge.json (MERGE name variants first) + runtable/cellline_selection.json   (auto: top real line by #compounds→reads; or manual)
9  runtable_fetch   -> runtable/xml_cache/<GSE>.full.xml   (PARALLEL; +ENA fallback)
10 runtable_build   -> runtable/SraRunTable_all.csv, match_candidates.json
11 cellline_match   -> runtable/SraAccList.txt (MAIN), by_study/<GSE>/SraAccList.txt, SraRunTable_<line>.csv
12 runtable_annotate-> drug/dose/is_control + 3-way drug_treated + SraRunTable_<line>.xlsx + drug_annotation_review.csv
   --- CLUSTER DOWNLOAD (cluster_mode != off; needs the deep dive) ---
13 cluster_bundle   -> runtable/cluster/ (LSF scripts + filled config.sh + PER-STUDY by_study/) + cluster_bundle.zip
14 cluster_submit   -> (autonomous) scp + ./run_pipeline.sh; on failure: diagnose -> PAUSE for SSH fix -> retry upload
   --- STAR ALIGNMENT (module==bulk_rna_seq AND cluster_mode!=off; AUTO-CHAINED after the download) ---
15 star_bundle      -> runtable/star/ (STAR scripts + config.sh pointed at the download's FASTQ + organism/index) + zip
16 star_submit      -> (autonomous) scp + arm a SELF-RESCHEDULING launcher that runs STAR once the download finishes
   --- BAM->BED (module==bulk_rna_seq AND cluster_mode!=off AND bed enabled; AUTO-CHAINED after STAR) ---
17 bed_bundle       -> runtable/bed/ (vendored AltAnalyze BAMto*BED + exon ref + config.sh) + bed_bundle.zip
18 bed_submit       -> (autonomous) scp + arm a launcher that converts each BAM to <sample>__junction.bed once STAR finishes
   --- ALTANALYZE SPLICING / PSI (module==bulk_rna_seq AND cluster_mode!=off AND psi enabled; AUTO-CHAINED after BED) ---
19 psi_bundle       -> runtable/psi/ (psi scripts + config.sh + sample_groups.tsv from the run table) + psi_bundle.zip
20 psi_submit       -> (autonomous) resolve AltAnalyze (find-on-cluster / upload-if-missing) + arm a launcher that runs ONE AltAnalyze job over the BED dir once BAM->BED finishes -> per-sample PSI (+ dPSI when a 2-group split exists)
   --- DRUG CONCORDANCE (module==bulk_rna_seq AND cluster_mode!=off AND concordance enabled; AUTO-CHAINED after PSI) ---
21 concordance_bundle -> runtable/concordance/ (vendored scorer+ranker + config.sh + queries.tsv from the cell line's cancer atlas) + concordance_bundle.zip
22 concordance_submit -> (autonomous) arm a launcher that, once PSI finishes, scores each per-drug PSI signature vs the cancer-subtype atlas(es) -> results/<atlas>/ranked_concordance_summary.txt (reversal candidates: conc 0=reverses, 1=mimics)
```
Resumable at stage level (pipeline_state.json), study level (structured_done.json), and batch level (result files).
The two AI passes are independent. On a `--resume` where AI already finished, the AI preflight is skipped.

---

## 5. Key concepts

**Analysis modules + module-tied filter.** A UI "Analysis module" radio (`name="module"`) → `RunConfig.module`
(default `bulk_rna_seq`; CLI `--module`; saved in settings). `build_final.MODULES` maps a module → the build()
headline "mode" whose keep-rule defines its table; `module_mode()`; `build(P, mode, is_headline=)`;
`build_all(P, module=)`. `bulk_rna_seq` → mode `"splicing"` == the splicing-amenable filter below (verified
byte-identical + idempotent), and writes `cellline_index.json` (the deep-dive input) for the chosen module.
**Adding a module** = a `MODULES` entry (+ a new keep-branch in `build()` if it needs a different filter), a UI
radio option, and (for an aligner) a deploy module like `star_deploy.py`.

**Splicing filter (`build_final.is_splicing_amenable`, the bulk_rna_seq keep-rule):** KEEP TruSeq/NEBNext/KAPA/
total-RNA **and Smart-seq** (full-length); REMOVE 3'-end methods — single-cell/nuclei, plate-seq, 10x/droplet,
sci-Plex, bulk 3'-tag (QuantSeq/DRUG-seq/3'-DGE/BRB-seq/Tag-Seq) — via protocol text + title regex + the AI
Single-cell category. Lexogen *QuantSeq* = remove; Lexogen *Ribocop/total-RNA* = keep (don't match bare "lexogen").

**Two-layer suitability filter (study-level + run-level).** The above is the STUDY/sample-level layer (works off
GEO protocol text + AI category + title, before any SRA run table exists). A run can still re-enter the deep-dive
by matching the target cell-line VALUE even though its GSM was filtered out, so `cellline_match._splice_drop_reason`
adds a RUN-level layer over the reconstructed runtable's SRA controlled-vocab columns: drops non-RNA-Seq `Assay
Type`; **long-read `Instrument`/`Platform` (Oxford Nanopore MinION/GridION/PromethION, PacBio Sequel/RS — STAR is
short-read, so these break alignment; ~11k such RNA-Seq runs in the A549 set, caught by NO other layer)**; non-
splicing `LibrarySelection` (CAGE/RACE/size-fractionation; cDNA/PolyA/Oligo-dT/RANDOM/Inverse-rRNA kept); and any
single-cell straggler by text. **NB the columns do NOT separate scRNA from bulk** — both are `ILLUMINA`+`cDNA`; the
only scRNA signal is protocol/title text, which is why single-cell is owned by the study-level layer. `OTHER`
(LIBRARY_STRATEGY catch-all: RASL/GRO/PRO/Ribo-seq…) is dropped by the strategy gate by default — the safe choice;
a rescue (keep `OTHER` iff `LibrarySource=TRANSCRIPTOMIC` + a known-good selection) rarely fires, not worth the risk.

**Deep dive (8-12) — best cell line → download-ready run list.** BEFORE ranking, `deepdive_select.consolidate` **merges cell-line NAME variants** in
cellline_index.json (deterministic: same Sample Type + a normalized key — A549 / A-549 / "A549 cells" collapse to
one row; the dominant spelling wins; merged spellings saved as `aliases` → cellline_merge.json) so a line split
across spellings isn't under-counted/mis-ranked. Selection then considers only REAL lines (Sample
Type "Cell line"; never UNRESOLVED/Patient/Organoid), ranked by #unique compounds then total reads (auto picks
#1; manual pauses → UI ranked list + `/api/select`). The Run Selector table is reconstructed byte-for-byte from
SRA XML. The **disambiguation agent** marks a value as the target only if it IS/resembles it (A549≈A-549≈
"A549 cells"), excluding e.g. BEAS-2B; keep rule is a HYBRID union — keep a run if its GSM was already classified
as the line OR its cell-line value is agent-blessed. `--skip-ai` ⇒ deterministic normalized-equality match. The pre-select consolidation's merged spellings are unioned into the matcher's
`target_aliases` (both the agent and the deterministic path) for run-table recall.
Drug annotation reuses `normalize_v2` + `compound_map` (general, not a hardcoded vocab).

**Three-way drug-treated (Drug Treated / Not Drug Treated / Undetermined).** Hybrid: a sample with a structured
`treatment` field → treated iff ≥1 real (non-control, is_drug) compound; else the AI title classification; else
Undetermined (its own column — was hidden "pending"). Cell-line CSVs are **14 cols**; `cellline_index.json`, the
AI `emit_samples` enum, and the per-run `drug_treated` column all carry it.

**Undetermined RECOVERY in `runtable_annotate` (2026-06-24) — two tiers, applied only to the Undetermined branch
(verified 0 determined labels ever change).** The PSI grouping reads the runtable's `drug_treated` column, which was
COLUMN-only and so far MORE pessimistic than `cellline_index` (A549: 4,643 undetermined vs the AI's 1,995) — those
extra undetermined runs were silently DROPPED from the treated-vs-control comparison. Two recoveries close the gap:
- **(1) Noise-framed core (`recover_label`).** `is_control`/`clean_compound` match a bare token but miss it under
  dose/time/framing noise, so `PBS treatment`/`vehicle treated`/`DMSO treated` → Undetermined (should be control)
  and `12 h, 5uM BRM014` too (should be drug). `recover_label` re-tests the NOISE-STRIPPED core (`_core_treatment`
  strips DOSE/`_TIME`/`_FRAMING`): `is_control(core)`→Not Drug Treated; a compound the AI map CONFIRMS (`is_drug=True`
  by exact key OR canonical name in `drug_names`)→Drug Treated. CONSERVATIVE — a novel/ambiguous token stays
  Undetermined (mislabeling contaminates the comparison; a dropped Undetermined is harmless). A549: 28 recovered.
- **(2) AI title fallback (the hybrid `build_final` already uses for `cellline_index`).** For a run STILL
  Undetermined, defer to `sample_map.get(title)` keyed by the GEO sample title from `raw_json` (build_final's exact
  key; `gsm2title` built from `raw_json` samples). Adopt ONLY a confident `Drug Treated`/`Not Drug Treated` (N/A &
  Undetermined stay). This makes the PSI table CONSISTENT with the headline `cellline_index` instead of dropping
  samples the AI already classified — A549: **+2,010** (undetermined 4,643→2,605, −44%), MDS-L: all 18→0. It inherits
  the AI's classification quality (e.g. `Lipofectamine 2000`→treated is debatable) but introduces no NEW judgment —
  it's the same call already in the headline tables. The `review.csv` audit stays the condition-based (column+noise)
  classification; the AI fallback is per-GSM and only sets the output `drug_treated`. Log shows `recovered N
  (noise=, AI-title=)`.
- **Nucleic-acid / method tokens are NEVER drugs (`runtable_annotate._is_nondrug_input`, 2026-06-24).** `canon_drug`
  treated ANY leftover `clean_compound` token as a compound (`if drug: "Drug Treated"`), so a treatment value like
  `STRT 10ug`→`STRT` or `10ug RNA`→`RNA` would be mislabeled **Drug Treated** — an RNA *input dose* read as a drug
  dose. `canon_drug` now returns `''` for `*RNA`/`*DNA` forms (rna/mrna/total rna/sirna/sgrna/cdna/gdna/genomic
  dna/…) + `STRT`/`ERCC`/spike-in/input → they become Undetermined (dropped from PSI), never Drug Treated. Real
  drugs (cisplatin/imatinib/BRM014/doxorubicin) untouched. Hit LIVE: K562 `GSM3057932-39` (`STRT 10ug … RNA-Seq` =
  an RNA-input titration). Those GSMs were already safe (empty treatment columns→Undetermined); the fix closes the
  latent case where the dose string is in a treatment column. (Aside: STRT-seq is single-cell but the SC keyword
  filters don't catch `STRT`, so such samples can slip into the runtable as RNA-Seq — drug_treated=Undetermined
  still keeps them out of the treated-vs-control comparison.)

**AI cleaning.** One `classify()` for all three providers (anthropic=tools; openai/gemini=forced
function-calling via the `openai` SDK, Gemini at its OpenAI-compat endpoint; JSON-in-content fallback). Default
models: `claude-haiku-4-5` / `gpt-5.4-nano` / `gemma-4-31b-it`. Two passes: **compounds** (raw → {name, is_drug})
and **samples** (title/cell-tag → {cell_line, category, drug_treated}); prompts in `ai_clean.py`.
- **Custom OpenAI-compatible endpoint:** `RunConfig.base_url` → `make_client(..., base_url=)`. UI field under the
  OpenAI provider; CLI `--openai-base-url`. The key field is sent to that host. base_url MUST flow into EVERY
  `preflight(...)`/`make_client(...)` call (the batch passes, `cellline_match`, AND `_ensure_ai_works`'s preflight).
- **Reasoning models (`disable_reasoning`):** a reasoning model on the OpenAI-compat path (e.g. Xiaomi MiMo
  `mimo-v2.5`) emits `reasoning_content` that eats `max_tokens` and truncates the tool call (`finish=length`,
  no tool call) → empty `results` → `ai_clean`'s **"no output"** with the whole batch silently dropped. The
  **"Disable model reasoning"** checkbox (under the OpenAI base-URL field) / CLI `--disable-reasoning` →
  `RunConfig.disable_reasoning` → `classify(..., disable_reasoning=True)` sends `llm_providers.NO_REASONING_BODY`
  (`thinking:{type:disabled}` + `chat_template_kwargs:{enable_thinking:false}`) via `extra_body` on the
  **openai/gemini path ONLY** (standard OpenAI/Gemini 400 on unknown body fields, so it's opt-in, default off).
  Like base_url it MUST flow into the batch passes, `cellline_match`, AND the `_ensure_ai_works` preflight.
- **Preflight fix-or-disable:** `ai_clean.preflight(cfg)` does one tiny 1-item live call up front (a valid-but-
  throttled provider still PASSES; only a real misconfig trips it). On failure `pipeline._ensure_ai_works` PAUSES
  → correct provider/model/key and retry, or **turn off AI**. Wiring: `RunReporter.await_ai_fix`/`provide_ai_fix`,
  `/api/ai_retry`, `renderAiFix`, CLI `_console_ai_fix`, `classify_ai_error` (auth|model|perm|rate|network|unknown).
- **Mid-run outage = SAME inline pause, not a crash:** if the provider dies *during* a pass (proxy goes down
  after hours), `ai_clean.run_pass` RAISES (>`DROP_CEILING` unanswered). `pipeline._run_ai_pass_resilient`
  wraps both passes: it CATCHES that, routes it through the same `await_ai_fix` form (switch provider — e.g.
  proxy→Ollama — or turn AI off), and re-calls `run_pass`, which **resumes per-batch so only the unanswered
  batches re-run** (completed batches preserved). Turning AI off mid-pass calls `ai_clean.drop_fill_missing`
  (Unknown-fills just the unanswered batches, keeps the answered ones) so the pass completes deterministically
  instead of discarding work. `cfg`-edits applied by `_apply_ai_fix` (shared with preflight); `_ai_cfg_from(cfg)`
  rebuilds the pass cfg each retry so a provider switch is honored (incl. the downstream `cellline_match`).
  Before this, a mid-run outage hit `reporter.fail()` → `error` state with only the generic banner and no inline
  retry (recoverable only via the form's "Resume last run").
- **Rate limits don't pause:** a 429/quota (category "rate") auto-retries every 30 s indefinitely — both in the
  preflight loop and per batch worker — instead of pausing or dropping the batch. AI concurrency cap is 1–99.

**Parallel NCBI fetch.** `structured_extract.run` and `runtable_fetch.run` thread per-study fetches
(`workers=min(cfg.concurrency, 24/12)`) behind a GLOBAL thread-safe pacer that spaces request STARTS to NCBI's
limit (0.11s≈10/s with a key, 0.34s≈3/s without) while responses overlap — so the API key now yields ~3x. The
rate limiter, NOT the thread count, bounds req/s. Extract's resume checkpoint is batched (every 25 + final), safe
because jsonl rows are deduped by GSM in build_final.

**Cluster download handoff (13-14).** `cluster_mode` ∈ off | manual (zip) | autonomous (ssh upload + launch).
PER-CELL-LINE ISOLATION: deploys to `PIPELINE_ROOT/<cellline>` (the entered root is the PARENT). Ships PER-STUDY
`by_study/<GSE>/SraAccList.txt` (never a combined list) so each study downloads independently. On an autonomous
upload failure the run PAUSES with a diagnosed, prefilled SSH-fix form (`/api/cluster_retry`,
`reporter.await_cluster_fix`, CLI `_console_cluster_fix`) and retries JUST the upload. The vendored download
pipeline cleans transient clutter on COMPLETE (keeps fastq.gz/SraAccList/PIPELINE_COMPLETE/watchdog.log).

**STAR alignment module (15-16), auto-chained.** When module==bulk_rna_seq AND cluster_mode!=off AND a deep dive
exists. `star_deploy.build_star_bundle` fills `star_template/`'s config.sh: `FASTQ_INPUT_DIR=<download_root>/by_study`,
`BAM_OUT=<download_root>/STAR_bams`, `RUNTABLE=<bundle>/SraRunTable_<line>.csv` (runs of one BioSample → one BAM),
`JOB_TAG=<dlTag>_star`, `ORGANISM` auto-detected from the run table. STAR consumes the FASTQs, never the `.sra`.
`submit_star_over_ssh` uploads to `<download_root>/star` and submits `star_launch.sh` as a **SELF-RESCHEDULING
LSF job** (the watchdog pattern): each pass checks the download's `PIPELINE_COMPLETE.txt`/`_STALLED`; if not done
it re-queues itself +30 min (`bsub -b`); when done it `exec`s `run_star_pipeline.sh`. So everything is on the
cluster after the upload — **close SpliceScout and STAR still runs days later.** Non-fatal like the download. **The launcher bsub is setsid-DETACHED (2026-06-25):** `submit_star_over_ssh` (and the bed/psi/concordance equivalents) sends the launcher as `( setsid bsub … <stage>_launch.sh </dev/null >>launch.out 2>&1 & )` — a subshell so ONLY the bsub is async (not the preceding untar/chmod). Without it, under a saturated per-user pending-job quota the inline `bsub` BLOCKS on "Pending job threshold reached. Retrying in 60s" and HANGS the deploy ssh until its 900s timeout ("stuck on launch star alignment"). Detached, the ssh returns instantly and the launcher retries through the quota in the background. NOTE this only fixes the deploy HANG; a starved run's jobs still can't ENTER LSF until the quota frees.
- **Genome-index resolution** (Python fills config; cluster bash decides, since the index lives there): explicit
  valid `GENOME_DIR` → `star_index_registry.json` organism match → a prior build in `STAR_INDEX_ROOT/<org>` →
  BUILD-ONCE `build_star_index.sh` (download FASTA+GTF, `genomeGenerate`, build-once guard). `lib_index.sh` +
  `resolve_index.sh` write `RESOLVED_INDEX.env`; per-sample STAR jobs get `-w done($BUILD_JID)`. **Fill the
  registry's `organisms["homo sapiens"].index_dir` (or the UI GENOME_DIR field) to skip the ~1-2h build.**
- **STAR status** in "Check cluster status": `remote_star_status`/`parse_star_status` (BAMs vs sample_list,
  launcher-pending, index-building, COMPLETE/STALLED, ETA); a 2nd banner line.

**Multiple instances + on-demand status.** Each launch claims an instance identity (lock dir
`~/.geo_pipeline_instances/`, PID-liveness) and the first free port from 8765 (probe by CONNECT, not bind). The
launcher PROMPTS for an instance NAME (exported as `$SPLICESCOUT_INSTANCE` → `server._resolve_instance_name()`);
`_claim_instance_slot(preferred)` sanitizes it (`_sanitize_tag`: keep `[A-Za-z0-9_]`, append `-2`/`-3` on a
live-name collision) into the cluster `JOB_TAG`, or falls back to the lowest free `sraN` when left blank. The
prompt lives in `launch_Win.bat` / `launch_Mac.command` because the default Windows tray launch is windowless
(so `server.py` must never call `input()`). That windowless `pythonw` launch can't reliably surface the UI —
`webbrowser.open()` returns True but no tab opens, and the Win11 tray icon hides in the overflow (symptom: "I run
the launch file and nothing happens", while a server is actually serving on 127.0.0.1:876x) — so `tray.py` writes
its chosen URL to `_last_url.txt` and `launch_Win.bat` reads it + opens the browser from its OWN console (reliable);
`SPLICESCOUT_OPENED_BY_LAUNCHER=1` tells `tray.py` not to also open it (avoids a double tab). Run dir + cluster `JOB_TAG` are set to that tag so concurrent runs
never collide. **The cluster deploy FOLDER + every stage's job names are the instance tag SCOPED BY the (normalized) cell line**
(`cluster_deploy.project_job_tag` → `_effective_root` = `PIPELINE_ROOT/<JOB_TAG>_<cellslug>`, e.g. `…/sra1_mdsl`;
pipeline.py rewrites `cfg.cluster_cfg["JOB_TAG"]` once after select so the folder + ALL stage job names inherit it).
So reusing ONE instance name for DIFFERENT cell lines ISOLATES each into its own folder/jobs (they never
share/clobber), while the SAME line resumes its folder. The slug is **normalized** (alnum + trailing `cell(s)`
stripped) so `MDS-L`/`MDSL`/`MDS-L cells` all → `mdsl` → the SAME folder no matter how the AI spells it that run —
this is what made plain cell-line keying unsafe before (it FRAGMENTED a project when the AI renamed the line; now
fixed by normalization + `build_final._merge_variants`). Idempotent: an instance whose name already IS the line
(an `A549` instance on A549) is unchanged. A RESUME keeps whatever tag the bundle was ALREADY deployed under
(`_read_config_jobtag`) so an in-flight run is never relocated; the status probes read that same deployed tag. The
slug is also used for LOCAL run-dir filenames (`SraRunTable_<slug>.csv`).
**Walltime/mem self-heal:** every cluster job submit (download prefetch/conversion, STAR, BED — per sample/study;
PSI + concordance — single job, via their watchdog) reads the LAST LSF termination from the job's `-o` log (awk,
compute-safe) and ESCALATES before resubmitting — `-W` → queue max on a `TERM_RUNLIMIT`, `-M` (+rusage) +50% per
`TERM_MEMLIMIT` — so a job that hit a limit isn't just re-killed (or dropped after N) but actually finishes.
**EVERY job runs at `-W 66480` (the normal-queue MAX, `1108:00` ≈ 46 days) — "everything to max" (2026-06-25).** Work jobs + launchers already did; the watchdog/poller passes, the `cs` convert (`-W 30`), and the `diagnose` job (`-W 90`) were the holdouts and are now bumped too (concordance's work `WALL` also `48:00`→`1108:00`). WHY: a watchdog/launcher pass that blocks on the pending-job threshold (a saturated per-user quota, e.g. the A549 load test at 3,300+ PEND) used to hit its `-W 20` walltime and get `TERM_RUNLIMIT`-killed BEFORE its reschedule-first `bsub` could even queue a successor → the whole self-driving chain died (observed LIVE: `MDS_L_mdsl` downloaded 78/78 then STAR never launched). At the queue max, a blocked reschedule has ~46 days to clear the threshold, so nothing dies to walltime. Reschedule-first + the `-W`→queue-max self-heal on a `TERM_RUNLIMIT` resubmit remain as defense-in-depth. Bumped byte-level in all 5 templates + `diagnose_ai` (LF-safe); **live runs keep their old `-W 20` until re-deployed — surgically patch the deployed `*.sh` with `sed -E 's/-W (20|30|90) /-W 66480 /g'` + `bash -n` + `.prewallfix.bak`** (done for the live MDSL run). NOTE: the separate no-progress backstop `MAX_WALL_HOURS=336` (14-day STALL) is unchanged — it's a runaway guard, not a per-job walltime.
**Notifications + self-heal (2026-06-23):** every stage bundle vendors `lib_notify.sh` (`log_event`/`notify_error`/`notify_update`) — cluster jobs EMAIL the user directly via the cluster's `mail` and append every event to `$PIPELINE_ROOT/EVENTS.log`. `ALERT_EMAIL` is baked into each `config.sh` at deploy (`cluster_deploy._alert_email()` reads PC `settings.alert_email`, injected into `vals` before `fill_config`). All 5 watchdogs `notify_error` on STALLED/ORPHANED + `notify_update` on COMPLETE. **PSI + concordance watchdogs self-heal a RUN-but-FROZEN deadlock**: a work job whose `cpu_used` is unchanged for `IDLE_STALL_PASSES` (=3) passes is bkilled + resubmitted + emailed (the 14h-A549-freeze class). **BED writes are collision-proof**: `run_bed_job.sh` runs in a private `$BED_OUT_DIR/.bedwork/<label>.<jobid>` (BAM+ref symlinked in) then atomic-`mv` publishes — duplicate jobs can't tear the shared `<sample>__*.bed` (the A549/MDS_L corruption root); STAR's BAM publish is likewise temp+atomic-rename. The BED watchdog only counts a conversion FAILURE when the attempt actually ENDED (`bed_job_ended`: `-o` mtime newer than a `.lastsub` stamp) — a still-running job is never false-dropped (fixed the MDS_L spurious meltdown). `bed_cleanup_tools` is gated on `TOOLS_CLEANUP_COMPLETE.txt` + run at pass-top, so a COMPLETE-but-uncleaned run self-heals on a re-arm. `compress_done.sh` falls back off the `-T` threaded flag (old-xz "0/72" bug) and marks `PIPELINE_COMPRESS_FAILED.txt` (scanned by `remote_alerts`) on failure.
**Resume:** `/api/resume` + a "Resume last run" button re-attach this instance's most recent `runs/*` dir, rebuild `RunConfig` from its `config.json`, and continue (the per-stage `begin()` done-checks skip finished stages) — closing the new-run-dir-per-launch gap so a server restart resumes. Resume can override provider/model/skip_ai (switch to Ollama if the proxy is down).
**CPU diagnostic AI (`diagnose_ai/`, installed at `/data/salomonis-archive/LabFiles/SpliceScout_AI`):** a self-contained cluster CPU LLM (conda env + `llama-cpp-python` prebuilt wheel + **Google Gemma 4 E4B it-qat GGUF, REASONING ON** — swapped from Qwen2.5-3B 2026-06-23) the watchdogs `bsub` on STALL via `notify_diagnose` (in `lib_notify.sh`). The cluster's `llama-cpp-python 0.3.30` already implements the `gemma4` arch; the GGUF is the exact Ollama `gemma4:e4b-it-qat` blob (pulled from the Ollama registry, sha256-verified). Explicit thinking: 0.3.30's chat API can't pass `enable_thinking`, so `diagnose.py` renders the template via `Jinja2ChatFormatter(enable_thinking=True)` + tokenize `add_bos=False` + raw `create_completion`, then `_extract_json` takes the LAST balanced `{...}` after the `<|channel>thought` block. ~80s + 8.16GB peak RSS/diagnosis → `notify_diagnose` bsub bumped to `-n 8 -M 16000 -W 90`. `diagnose_job.sh` gathers context -> `diagnose.py` (model -> JSON `{cause,action,args,confidence}`) -> EMAILS the diagnosis; with `DIAGNOSE_AUTOFIX=1` it APPLIES a budget-capped, reversible WHITELIST (`quarantine_bed`/`rearm`; `bump_*` recommend-only), never arbitrary commands. Knobs `DIAGNOSE_ON_STALL`/`DIAGNOSE_AUTOFIX`/`DIAGNOSE_MAX_REARMS`/`DIAGNOSE_AI_HOME` in every config.sh. The fallback for stalls the deterministic self-heal + the proxy AI can't resolve. See the [[splicescout-cpu-diagnostic-ai]] memory. **Model resolution + per-pipeline cache (2026-06-23):** `diagnose_job.sh` resolves the GGUF in priority order — explicit `DIAGNOSE_MODEL_PATH` (a specific `.gguf`) → a model already cached in `DIAGNOSE_MODEL_DIR` (default `<PIPELINE_ROOT>/.splicescout_ai/models`) → the shared install's `models/`. If found ONLY in the shared install it COPIES ("uploads") the model into the pipeline-dir cache (atomic temp+rename) so every FUTURE run pointed at that directory reuses it locally (and it survives the shared install being cleaned). `DIAGNOSE_MODEL_PATH`/`DIAGNOSE_MODEL_DIR` are template config vars, baked from PC settings `diagnose_model_path`/`diagnose_model_dir` (`cluster_deploy.bake_diagnose_model`, injected into `vals` beside `ALERT_EMAIL` in all 5 deploys) AND passed as args 7/8 to `diagnose_job.sh` by `notify_diagnose` (so they reach the bsub'd job without LSF env propagation). Set them in the GUI's Advanced cluster settings (`#clmodelpath`/`#clmodeldir`).
Because the folder is a codename, the download bundle stamps the resolved cell line into a `CELL_LINE.txt`
at the folder root (+ a `Cell line:` header in `RUN_ON_CLUSTER.txt`), so you can still map folder → cell
line on the cluster: `grep -H . <PIPELINE_ROOT>/*/CELL_LINE.txt`. (Only the download stage writes it; a
phase-start onto a folder that never had a download won't have one — extend to star/bed/psi if needed.) "Check cluster status"
(results banner + top-of-form button) is **strictly instance-scoped**: the page exposes `INSTANCE_TAG`,
`fetchClusterStatus` POSTs `{job_tag: INSTANCE_TAG}`, `/api/cluster_status` has no bare-"sra" fallback, and the
probe self-discovers the cluster root from this instance's `<tag>_*` LSF jobs (works after a server restart).
It is on-demand only (no auto-refresh) and shows a download/convert ETA refined by least-squares over your
successive checks (persisted in localStorage). Plots tab + clickable step docs are pure client-side.
The **cross-stage STALL alert** (`cluster_deploy.remote_alerts`, the RED "N stages STALLED" banner that catches a
silent downstream stall the nested probes miss) is also **strictly run-scoped** (2026-06-22): the panel scans only
THIS run's effective root (the discovered job CWD, else the `config.sh` `PIPELINE_ROOT`) — **never** the bare
shared `PIPELINE_ROOT`. Scanning the shared root made one run INHERIT a SIBLING/abandoned run's stall (a fresh
download run showed an unrelated old `…/MDSL/…/bed` BED stall as a false alert); if only the bare root is
resolvable the scan is refused (an unattributable stall is not this run's alert). The probe also lists stage dirs
that reached `PIPELINE_COMPLETE.txt` and **suppresses** any STALLED/ORPHANED marker whose dir later completed (a
re-armed → finished stage leaves a stale marker behind — don't re-alert on it). The background email poller
(`_alert_poll_once`, opt-in via `alert_email`) intentionally still scans the SHARED root — it's a cross-run
heads-up, deduped by path+time — and gets the COMPLETE-suppression for free.

**Assistant (chatbot) — `chat_assist.py` + `llm_providers.chat()` + `/api/chat` + the "Assistant" tab (2026-06-15).**
A plain-English agent for non-technical users that FILLS/controls every setting, writes & TESTS the NCBI GEO
query from a description, explains stages + reads logs, and answers data questions + draws Plotly charts inline
— all via tool-calling on the UI-configured provider. The browser holds the conversation (sessionStorage) and
POSTs it to `/api/chat`; `run_turn()` drives the model↔tool loop (`asyncio.run` per request, cap 8 rounds),
then returns `{reply, trace, settings_changed, charts}`. When the bot calls `update_settings` (the SAME
`_save_settings` store as the form) the page re-applies `/api/settings` so the form visibly updates.

**Universal data + charts (2026-06-16).** `run_data.build_db()` loads EVERY run artifact into in-memory SQLite
— `studies` (ncbi_raw), `study_protocol`, `samples` (the 40k-row structured_samples.jsonl), `pipeline_stages`
(progress), a synthesized `data_funnel`, plus every tables/* & runtable/* CSV — so `run_sql` and `make_chart`
reach ALL collected data, not just the deep-dived cell-line table. `make_chart` takes `sql=<SELECT>` (shape the
data, then chart its columns) OR `source=<table|funnel|cellline>`; `chart_engine.build_figure()` renders any of
bar/line/scatter/histogram/box/violin/pie/funnel/waterfall/heatmap (the vendored Plotly is the FULL bundle).
`list_data` enumerates tables+columns; `data_funnel`/`source='funnel'` gives the rise-then-fall data-volume
funnel (studies→samples→cell-line→[cluster BAM/BED/PSI if `include_cluster`]). The server renders returned
figures verbatim (`Plotly.newPlot(fig.data, fig.layout)`), so new chart types need NO frontend change.

**PREPARE-ONLY**: no launch/kill/deploy tool — the bot sets up, the user presses Start; cluster access is
read-only (`cluster_deploy._ssh_capture_*`, whitelisted paths); API keys + ssh passwords are redacted before
they ever reach the model. Local Ollama works but is slow for multi-turn chat.

**Per-model max output tokens (2026-06-16).** UI field (AI provider card) → `settings.model_max_tokens` =
`{model_id: int}` (persisted, keyed by model so each remembers its own). Flows to the pipeline as
`RunConfig.max_tokens` → `ai_cfg["max_tokens"]` → `classify()`, and to the Assistant via `chat_assist._run`;
blank = the long-standing 60000 default (chat: 4096). Raise it for verbose reasoning models so chain-of-thought
doesn't truncate the tool call; lower it to cut cost / stay under a TPM cap.

---

## 6. Design decisions & gotchas (DON'T break these)

- **GEO→SRA resolution = FOUR paths in order (`structured_extract.sra_ids_for_study`):** (1) `elink(dbfrom=gds,db=sra,id=<gds_uid>)`, (2) `esearch sra` by the GSE accession, (3) `esearch sra` by the study's SRP/BioProject from its GEO esummary `extrelations`/`bioproject` **— GUARDED** (see below), (4) `esearch sra` by the study's OWN GSM accessions (from the esummary `samples` list, batched 100/query, OR'd), `_study_meta` (one esummary → SRP + GSMs + n_samples). elink is exact WHEN PRESENT but it's **MISSING for some studies**, and bare-accession esearch silently returns 0 for most — so paths 1+2 alone **silently dropped studies that DO have SRA** (e.g. GSE164788: elink=none, `esearch sra GSE164788`=0, yet its SRA study SRP301436 has 765 runs). **NEVER conclude "a study has no SRA" from `esearch sra term=<GEO accession>` — it's a false negative;** use elink / SRP / per-GSM.
- **Download launcher: clear stale markers + DETACH the submit (both fixed 2026-06-23; the A549 "stuck on upload+launch" incident).** (a) Per-instance folders are REUSED across re-runs, so a stale `PIPELINE_COMPLETE.txt` from a prior run made the new run's watchdog instantly "already finalized" → it re-ran the COMPLETE cleanup, DELETED the just-uploaded scripts, and stopped (work jobs then died `watchdog.sh: No such file`). `run_pipeline.sh` now `rm`s stale `PIPELINE_*` terminal markers + `.finalized.lock` after setup. (b) `run_pipeline.sh` ran `run_all.sh` (submit every study) SYNCHRONOUSLY; on a big/saturated run each `bsub` blocks on the pending-job threshold, so it ran past the PC launch ssh's `timeout=600` → ssh killed `run_pipeline` before the watchdog was armed. Now it bsubs a new **`launch_all.sh`** (`${JOB_TAG}_launch`) that submits + arms the watchdog on a compute node, returning the ssh in seconds. A plain `nohup &` over ssh does NOT persist — use bsub. Both UNTESTED live; verify on the next download run. See [[splicescout-gotchas]].
- **Bundle upload = ONE tar.gz, not `scp -r` of the tree (fixed 2026-06-24, the "stuck on upload, timed out" report).** `_submit_systemssh`/`_submit_paramiko` (`cluster_deploy.py`, reused by the download AND the STAR/BED/PSI submits) used to `scp -r` / per-file `sftp.put` the whole `cluster/` dir. A big run's `by_study/` holds one tiny `SraAccList.txt` PER STUDY (A549 = **766 dirs / 782 files**) and each file is a separate SSH round-trip → hundreds of handshakes → blows past `timeout=600` → "stuck on upload". FIX: `_make_bundle_tar` packs the bundle into ONE `.tar.gz` (782 files → 89 KB in 0.9s), transfers the single file, and the launch ssh does `tar xzf _bundle.tar.gz && rm -f … && chmod +x *.sh && ./run_pipeline.sh` (untar timeout bumped to 900 / paramiko exit-wait to 120 for many-files-on-NFS). Verified: tar round-trips to an identical file set. (Separate `psi_deploy.py:687` `scp -r` uploads the multi-GB AltAnalyze toolkit — gated to when it's NOT found at the lab install, 1-hr timeout — left as-is.)
- **Launcher bsub DETACHED so a saturated quota can't hang the upload ssh (fixed + verified live 2026-06-24).** The tar fix above solved the *transfer*, but the actual live stick (MDSL-2, only 4 studies) was downstream: `run_pipeline.sh`'s `bsub launch_all.sh` runs INLINE in the upload ssh, and under a saturated per-user pending-job quota (the A549 load test = 3,800+ PEND) `bsub` BLOCKS on "Pending job threshold reached. Retrying in 60s" → the head node resets the long-held ssh → "stuck on upload, timed out". FIX: `run_pipeline.sh` DETACHES that bsub with **`setsid bsub … </dev/null >>launch.out 2>&1 &`** — the ssh returns immediately and the bsub keeps retrying in the background until a slot frees. **`setsid` survives the ssh disconnect on this cluster (persistence-tested); plain `nohup` does NOT** (why the original launch-detach used bsub). Dropped the `|| bash launch_all.sh` inline fallback (ran the heavy submit on the head node). Verified LIVE: re-deploy returned `submitted:True` instantly; `launch.out` shows the detached bsub retrying; it submits once A549 frees a slot, load test untouched. Recover a GUI-FAILED upload without a server restart: `cluster_deploy.submit_over_ssh(Paths(run_dir), settings['cluster'], {})` after refreshing the bundle's `*.sh` (NOT the filled `config.sh`).
- **Launcher TEMP-WATCHDOG during submission (fixed 2026-06-24, the A549 heavy-load observation).** On a huge run `launch_all.sh` can spend >1h in `run_all.sh` under the pending-job threshold (`A549` = 865 studies / 1,214 pending), and it armed the watchdog only AFTER finishing — so for that whole window the run is un-watchdogged (a conversion that dies mid-submit sits stranded; if the launcher itself dies the work orphans). FIX: `launch_all.sh` now runs `run_all.sh` in the BACKGROUND and, while it's alive, every `${LAUNCH_HEAL_INTERVAL_SECS:-600}`s runs a **HEAL-ONLY watchdog pass** (`SRA_WATCHDOG_HEAL_ONLY=1 watchdog.sh`); it polls every 30s so the real watchdog arms promptly once submission ends. `watchdog.sh`'s heal-only mode does ONLY the safe resubmit-of-stranded-conversions and SKIPS reschedule, refetch (run_all is still submitting prefetches, so "missing" = not-yet-downloaded), the backstop, the completion/stall decision, finalize, and cleanup. Also added a duplicate-conversion guard (always on): skip per-accession resubmission for a study whose bulk `cs` converter is still live (it'll convert; critical while cs jobs sit PENDING behind the flood). Verified by stub harness: heal-only → resubmit only; normal → resubmit + reschedule + refetch unchanged. Same launch→submit→arm pattern exists in the STAR/BED/PSI launchers (could get the same heal-only treatment). LF preserved; UNTESTED live.
- **A BioProject is often a SHARED UMBRELLA — guard path 3 by `n_samples` (the ENCODE bug, fixed 2026-06-23).** `_sra_project_acc` blindly `esearch`ed the BioProject, but e.g. **ENCODE's `PRJNA30709` holds 5867 runs across thousands of 2-sample K562 sub-series** — so every such sub-series (GSE177723, …) got the WHOLE umbrella's runs, truncated at `retmax=2000`, mis-attributed to a 2-sample study (a K562 run came out ~99% phantom rows: 120/168 studies pinned at exactly 2000). FIX: path 3 fetches the project's `_esearch_count` and **SKIPS it when `count > max(200, 10×n_samples)`** (an umbrella), falling through to path 4 (per-GSM), which resolves the sub-series to just its real runs. The guard is sized so a genuinely large dedicated study still passes (GSE164788: 765 ≈ 764 samples → kept). Per-GSM esearch is NOT universal (works for ENCODE GSMs, returns 0 for GSE164788's), which is exactly why BOTH the guarded-SRP path AND the GSM path are kept. The `+N samples` console/`set_detail` line is `len(rows)`; `≈2000` is the tell-tale of an old-code umbrella over-match.
- **Parallel fetch is rate-limited, not thread-bound.** Keep the global `_throttle`/`_throttle_lock` (in BOTH
  `structured_extract` and `runtable_common`) and the `write_lock` on extract's jsonl/done-set/protocol writes.
  Don't re-serialize. The threading change to the VALIDATED `runtable_common` is pacing-only — `--validate-runtable`
  must still PASS. NCBI's 10/s (keyed) is the hard ceiling for all NCBI stages.
- **Perf pass (2026-06-24, all verified output-equivalent; under the NCBI ceiling, wins come from FEWER requests
  not more threads):** (1) **Stage 2 extract** — `_study_meta(uid, meta_item)` REUSES the Stage-1 esummary already
  in `ncbi_raw.json` (identical schema, verified `reuse==live`) instead of re-fetching it, removing ~1 esummary
  request per study (the common path-3 case); `run()` threads the record through `pairs`/`todo`/`handle`(now a
  5-tuple)/`process_study`/`sra_ids_for_study`. Live-fetch FALLBACK preserved when the record is missing. (2)
  **Stage 1 fetch** — esummary `batch_size` 50→200 (~4x fewer round-trips). (3) **Stage 11 cellline_match** — the
  splice gate now scans `all_rows` ONCE (was a keep pass + a separate drop-log pass, each re-evaluating
  is_line/`_splice_drop_reason` over ~210k rows); `_NORM_SUB` precompiled. (4) **Stage 10 runtable_build** —
  `SRAFiles/SRAFile` walked once not twice (byte-identical; `validate()` re-confirmed PASS). (5) **Stage 12
  runtable_annotate** — annotation memoized per distinct raw treatment string (~11x fewer regex calls: 13906 rows
  → 1271 distinct), and `make_workbook` takes the in-memory table (skips a CSV re-read). Bigger wins NOT yet done
  (offered): Stage 7 build load-once/classify-once (~2/3 of the stage). Stage 9 has NO safe dedup — it and Stage 2
  resolve studies→runs by DIFFERENT validated methods whose sets legitimately diverge.
- **The 429 slow-down is TEMPORARY, not permanent (2026-06-23).** On a 429, `_slow_pacer` raises the global
  interval (+0.05, cap 1.0) AND stamps `last_429`; `_recover_locked` (called inside `_throttle`, under the lock)
  ramps the interval back toward the keyed/keyless `baseline` once a full quiet minute has passed since the last
  429 — halving the remaining excess each minute, snapping to baseline within ~0.02 s, and re-arming the
  throttled-notice. A fresh 429 re-raises it. Same recovery in `fetch_5000_ncbi.fetch_summaries_batch` (the local
  `extra_pace` decays after 60 s quiet). So a brief throttle storm no longer drags the WHOLE run at the slow pace.
- **`merge_ai` counts are glob-based, never hardcoded** (the original hardcoded 14/222 dropped data on other sizes).
- **`build_final` writes into the run's `tables/`** (single normalizer `normalize_v2` shared by extract/prep/build —
  no private copies). High Count = >40,000,000 spots/sample.
- **Windows file locks:** `build_final`/workbook write a `*_v2` copy if the target is open in Excel — keep it. AI
  result files are temp-then-`os.replace`. `_ask` catches EOFError (agent shell reports a TTY but has no stdin).
- **No keyword-guessing of drug names from titles** — compounds come from the depositor's structured
  treatment/agent/compound SAMPLE_ATTRIBUTES; cell lines from the `cell line` tag; reads from `spots`.
- **base_url in every AI client call** (gotcha that 401'd MiMo): the preflight dict in `_ensure_ai_works` MUST
  include `base_url`, not just the batch passes.
- **`.strip()` API keys at EVERY ingestion point** (fixed 2026-06-24): a leading TAB from a paste made `Bearer
  \tsk-…` fail auth on the gateway — and broke ONLY the assistant, because the launch path stripped the key
  (`server.py` `_start_run`) but `chat_assist` did not. Strip now happens in `chat_assist` (key read),
  `server._save_settings` (api_keys/ncbi_key/base_urls), and `llm_providers.api_key()` (+ openai/anthropic
  `make_client` pass it explicitly). A LiteLLM gateway in config-mode (no Postgres, master-key-only) reports a
  key MISMATCH as the misleading `400 "No connected db"` and always shows `"db":"Not connected"` — so to tell an
  auth problem from a real outage, test with the MASTER key: 200 ⇒ it's the key, not the infra.
- **Secrets:** AI keys + SSH password live in memory/env only and in the PLAINTEXT settings file
  `C:\Users\krog5w\.geo_pipeline_settings.json` (prefills the form; delete to wipe). Per-run `config.json` holds
  NON-secret cfg only.
- **REAL cluster = CCHMC salomonis lab.** SSH to the LSF **submit host `bmiclusterp-head`**, NOT the login node
  `bmiclusterp` (bsub/bjobs/sra-toolkit aren't on its PATH). `PIPELINE_ROOT` parent =
  `/data/salomonis-archive/LabFiles/...`.
- **bash 4.2 (RHEL7) + `set -u`:** expanding an EMPTY array (`"${QOPT[@]}"`) is a fatal unbound-variable. ALL
  array expansions in `cluster_template/` AND `star_template/` are wrapped `${QOPT[@]+"${QOPT[@]}"}` /
  `${DEPW[@]+...}`. **RE-APPLY when re-vendoring** from `Downloads/SRA_pipeline_template/` (their source has the
  bare form). Watchdog `nlive` must count only WORK jobs, not itself, or PIPELINE_COMPLETE.txt is never written.
  The download watchdog also **DELETES a `.sra` once its `.fastq.gz` exists** (the per-acc converter does this, but
  the bulk `convert_study` path can leave the source behind; a stranded converted `.sra` keeps `nsra>0` so the
  "zero `.sra` left" completion gate never fires → a FALSE STALL despite all data present — observed LIVE on
  MDAMB231: 568/568 converted but 367 `.sra` stranded → STALLED). The download's fasterq-dump temp already falls
  back from a full `SCRATCH_DIR` to in-place under PIPELINE_ROOT (the 26T volume) and that worked — scratch did NOT
  block downloads, only STAR (which lacked the fallback until the `_workspace` fix).
- **bash 4.2 `local a=$1 b=${a}` on ONE line trips `set -u`** ("a: unbound variable" — `b`'s `${a}` is evaluated
  before the local `a` is bound). Declare on SEPARATE lines. This silently broke `star_template/watchdog.sh`'s
  `finalize()` (`local status="$1" rep="…${status}…"`): it crashed every pass *before* writing the marker, so the
  STAR watchdog detected completion, died, rescheduled, and LOOPED FOREVER — never stamping PIPELINE_COMPLETE. First
  hit when STAR ran to completion live (MDS-L 18/18). The download watchdog was already safe (declares on 2 lines).
- **STAR chaining = the SELF-RESCHEDULING launcher** (`star_launch.sh`). DON'T revert to `bsub -w ended(watchdog)`
  + a poll: the download watchdog self-reschedules so `ended()` fires after the first pass, and a poll job hits
  its walltime. The STAR `JOB_TAG` MUST differ from the download's (`<dlTag>_star`) or the two `${JOB_TAG}_watchdog`
  jobs collide. Keep `star_template/` LF; no signatures on re-vendored scripts. The launcher RUNS (not `exec`s)
  `run_star_pipeline.sh` and RETRIES (reschedules itself) when the launch fails — a transient `setup failed` must
  not silently skip STAR with no retry (hit LIVE on MDAMB231: the launch died on a setup hiccup and never retried).
  `star_deploy._star_launch_sh` generates this; keep the run-and-retry form, never `exec`-and-give-up.
- **Watchdogs RESCHEDULE-FIRST** (`cluster_template/watchdog.sh` + `star_template/watchdog.sh`): each pass queues
  its successor at the TOP (before the bsub-heavy resubmission), stores that job id, and `finalize()` `bkill`s it
  on COMPLETE/STALLED; a bottom safety-net reschedules if the start bsub didn't take. WHY: a pass that blocks on
  LSF's **pending-job threshold** (`bsub … Retrying in 60 seconds…`) and hits its walltime is
  TERM_RUNLIMIT-killed — and the OLD reschedule-at-END never ran, permanently halting the self-driving chain
  (observed LIVE: a download stalled with 201 .sra unconverted, no watchdog alive, while 1600+ jobs were pending).
  DON'T move the reschedule back to the end. **RE-APPLY when re-vendoring.** The top-of-pass
  `if [ -f PIPELINE_COMPLETE/STALLED ]; then exit 0` guard makes the already-queued extra successor a no-op.
- **Watchdog walltime = dead-man's-switch, DERIVED from the interval (2026-06-29):** the watchdog `-W` is computed
  as `WATCHDOG_INTERVAL_MIN - 5` at all THREE arm sites (`watchdog.sh` reschedule, `launch_all.sh` initial arm,
  `lib.sh sra_nudge_watchdog`), so it is ALWAYS < the reschedule interval — a STRUCTURAL invariant: a hung pass is
  killed ~5 min before its successor starts, freeing the flock so the chain can't overlap (a colliding successor
  fails `flock -n` and exits WITHOUT rescheduling = dead chain). RAISE `WATCHDOG_INTERVAL_MIN` to give big-run
  passes more time (A549 = 65 → 60-min passes); the walltime follows. NEVER hardcode a watchdog `-W` ≥ the interval
  — that re-breaks the switch (the 2026-06-25 `-W 66480` bump did exactly that: a hung pass held the lock 8h20m).
  A pass is slow ONLY when the queue has room to resubmit (it then runs a per-accession `bjobs -J` re-verify
  `lib.sh sra_job_is_live` for each stranded .sra — pathological under a big pending backlog); a FULL queue
  fail-fasts and finishes in ~2 min. **Efficiency fix DONE (2026-06-29):** the resubmit loop re-verifies each
  stranded `.sra` against a 2nd full snapshot (`LIVE_B`, in-memory string-match — live in EITHER snapshot ⇒ skip)
  instead of a per-accession `bjobs -J` — **2 table scans, not N+1**, so the ~12-min loop drops to ~1 min while
  keeping the fail-closed-vs-partial-snapshot guard. `sra_job_is_live` is now unused (left defined in `lib.sh`).
- **Download→STAR lag fix (watchdog kicks the launcher):** `cluster_template/watchdog.sh` finalize() `bsub`s the
  bundled `$PIPELINE_ROOT/star/star_launch.sh` (job `${JOB_TAG}_star_launch`) the MOMENT the download finalizes, so
  STAR starts in seconds instead of waiting up to two of the launcher's 30-min poll ticks (the launcher keeps its
  self-poll as a fallback). Guarded by `[ -f star/star_launch.sh ]` → no-op for plain download runs. RE-APPLY on re-vendor.
- **STAR finalize lag fix (last job nudges the watchdog):** `star_template/run_star_job.sh`, right after it
  publishes its BAM, calls `lib_star.sh:star_nudge_watchdog "$SAMPLE"`. If this is the LAST live work job
  (`star_live_work_count <= 1` — the watchdog `${JOB_TAG}_watchdog` is NOT in that count, only
  `^${JOB_TAG}_star_` work jobs are, so the `<= 1` is the calling job itself, still RUN), it queues a
  `${JOB_TAG}_watchdog` pass **gated on `-w "ended($LSB_JOBID)"`** so finalize lands within **seconds** of the
  final BAM instead of up to a full `WATCHDOG_INTERVAL_MIN` (default 30 min) poll later — the STAR-side analogue
  of the launcher kick, closing the "watchdog still queued while STAR is already done" gap. **The `ended()` gate
  matters:** the job nudges while it is *still RUN* (before it exits), so an *immediate* pass could see itself
  still live (`nlive==1`) and merely reschedule instead of finalizing — defeating the nudge; gating on the job
  ENDING makes the woken pass see `nlive==0` and finalize (one job id, the same single-dep mechanism
  `star_submit_sample` already uses for the genome build). **PURE ACCELERATOR** — the timed poll stays the
  fallback (a last job that dies WITHOUT nudging is still finalized by the next poll); NEVER drop the poll.
  Waking an ALREADY-active watchdog is safe (reschedule-first queues exactly one successor, finalize() bkills it,
  stale passes no-op via the already-finalized guard). `flock -n` ⇒ only ONE nudger if several finish together.
  Takes effect for NEWLY-submitted jobs only — a wave already RUNning when the patch lands keeps its in-memory
  script and falls back to the poll for that one finalize. RE-APPLY on re-vendor. **All THREE stages now have this
  nudge** (added to download 2026-06-07): the last SRA→FASTQ conversion (`fasterqdump_job.sh` →
  `lib.sh:sra_nudge_watchdog`), the last STAR BAM, and the last BED (`bed_nudge_watchdog`) each wake their own
  watchdog. The gate is `nlive == 1` (EXACTLY one — the still-RUN calling job), NOT `<= 1`: `*_live_work_count`
  returns 0 when `bjobs` hiccups under load, and a spurious 0 once fired the BED nudge into ~1000 watchdogs.
- **BAM→BED stage (AltAnalyze junction/exon; the stage AFTER STAR)** — `bed_template/` + `bed_deploy.py`, a faithful
  clone of the STAR module: one LSF job per BAM (`run_bed_job.sh`) runs AltAnalyze's `BAMtoJunctionBED.py` +
  `BAMtoExonBED.py` → `<sample>__junction.bed` + `<sample>__exon.bed` BESIDE each BAM (the tools have no output-dir
  arg); reschedule-first watchdog, idempotent `bed_done` (both BEDs non-empty), `ended($LSB_JOBID)` nudge. **ALL-IN-ONE:**
  the AltAnalyze toolkit is VENDORED in `bed_template/altanalyze/` (the two `BAMto*BED.py` + `export.py`/`unique.py` +
  the ~100 MB `refs/Hs/Hs_Ensembl_exon.txt`), so the cluster needs NO AltAnalyze install — only the stock `python/2.7.5`
  (which supplies `pysam`) + `samtools` modules. `export.py`'s module-level `import UI` is patched lazy (try/except) so
  no GUI tree is dragged in. **Auto-chain:** STAR's `watchdog.sh` finalize() kicks `$PIPELINE_ROOT/bed/bed_launch.sh`
  (job `${JOB_TAG}_bed_launch`) the moment STAR finishes — mirroring the download→STAR kick; `bed_launch.sh` waits on
  STAR's `PIPELINE_COMPLETE.txt` then runs `run_bed_pipeline.sh`. BED deploys to `<BAM_OUT>/bed/`; its PIPELINE_ROOT is
  that `bed/` subdir so markers/state NEVER collide with STAR's in BAM_OUT. JOB_TAG = `<starTag>_bed` (work jobs
  `..._bed_bed_<label>`, the same doubling as STAR's `..._star_star_`). Wired via pipeline.py stages
  `bed_bundle`/`bed_submit`, a `bed` settings block + `remote_bed_status` (server.py/cluster_deploy.py), and stage docs.
  **TWO load-bearing AltAnalyze gotchas (cost hours; don't re-break):** (1) `BAMtoExonBED.py` (this EnsMart91 build)
  DEFAULTS to `intronRetentionOnly=True` (writes `__intronJunction.bed`, NO `__exon.bed`); `--intronRetentionOnly False`
  writes `__exon.bed`. This is exposed as **`BED_MODE`** (config.sh) = `intron` (default; matches the lab's `BAMtoBED.sh`)
  | `exon` | `both` (runs the exon pass FIRST, then the plain intron pass LAST so the authoritative `__intronJunction.bed`
  wins). `bed_done()` + `remote_bed_status` are MODE-AWARE; CRUCIAL: `__intronJunction.bed` is hard-gated and can be
  legitimately EMPTY, so the intron/both done-check uses `[ -e ]` (exists), NOT `[ -s ]`, or a clean sample loops forever
  → STALL. (2) The exon ref uses
  UCSC `chr1` names but Ensembl-built STAR BAMs (both SpliceScout's registry FASTA and A549's index) use `1`;
  `BAMtoExonBED` AUTO-RECONCILES this (strips/adds `chr` to match the BAM), so the vendored chr-prefixed ref is correct
  and needs NO stripping — stripping does NOT fix a 0-entry result (the flag does). The ~100 MB ref is uploaded once and
  size-skipped (`bed_deploy._upload_ref_idempotent`); kept OUT of the bundle zip. Residual: every exon job rewrites a
  shared `<ref>__minimumIntronIntervals.bed` next to the ref (a Kallisto-index artifact, unread by the BED outputs) —
  garbled under concurrency but harmless. RE-APPLY the vendoring + the `BED_MODE` wiring on re-vendor.
- **AltAnalyze splicing / PSI stage (the stage AFTER BAM→BED)** — `psi_template/` + `psi_deploy.py`. Unlike STAR/BED
  (one LSF job PER sample), AltAnalyze runs as **ONE job over the whole `--bedDir`**, so the watchdog is a single-job
  variant: "done" = the PSI table exists (`psi_done` globs `<PSI_OUT>/AltResults/AlternativeOutput/*EventAnnotation*`
  with **nullglob**, NEVER `grep -c`); a job that dies with no output is resubmitted up to `MAX_RESUBMITS` (then STALLED);
  same reschedule-first + flock + pass/wall backstop + `ended($LSB_JOBID)` nudge as the others. **AltAnalyze is NOT
  vendored** (multi-GB with its DB): `submit_psi_over_ssh` PROBES the cluster — if `$ALTANALYZE_HOME/AltAnalyze.py` + a DB
  (`ALTANALYZE_DB` override, else `$ALTANALYZE_HOME/AltDatabase`) are present it uses them IN PLACE (no upload); else if the
  user set `ALTANALYZE_LOCAL` (a local copy) it uploads it ONCE to `<psi_root>/altanalyze_home` and rewrites the local
  config.sh's `ALTANALYZE_HOME` BEFORE the bundle upload; else `setup.sh` flags it. Default `ALTANALYZE_HOME` = the lab
  install `/data/salomonis2/software/AltAnalyze-91/AltAnalyze`. Modules: `python/2.7.5` + `samtools` + `R` (matches the
  lab's `AltAnalyze.sh`). **Auto-chain:** `psi_launch.sh` waits on BED's `<BAM_OUT>/bed/PIPELINE_COMPLETE.txt`, then runs
  `run_psi_pipeline.sh`. PIPELINE_ROOT = `<download_root>/psi` (sibling of STAR_bams/STAR_beds); bedDir = `<download_root>/STAR_beds`;
  JOB_TAG = `<dlTag>_psi` (work job `..._psi_job`). Wired via pipeline.py `psi_bundle`/`psi_submit` (gated on `bed_go`), a
  `psi` settings block + `remote_psi_status`, and stage docs.
  **Comparison groups.** AltAnalyze always emits the per-sample PSI table; a `groups.txt`+`comps.txt` adds the differential
  (dPSI) test. Those are built CLUSTER-SIDE by `build_groups.sh` = the shipped `sample_groups.tsv` (BioSample→group_num→label)
  **∩ the `*__junction.bed` files actually present** (failed samples excluded); `MIN_PER_GROUP=2`;
  `comps.txt` = the shipped **`sample_comps.tsv`** matched pairs (kept only where BOTH groups still survive the BED∩MIN
  filter) when present, ELSE every group vs the lowest group_num (control=1). The groups.txt KEY is `<BioSample><GROUP_KEY_SUFFIX>` =
  `<BioSample>.bed` (AltAnalyze's convention; BioSample is also the STAR/BED sample label). `sample_groups.tsv` is written by
  `build_psi_bundle` from the annotated `SraRunTable_<line>.csv`, collapsing runs→BioSample. **Default** (no user groups) =
  **per-condition** (`psi_deploy._build_default_groups`): the pooled control baseline (every "Not Drug Treated" sample) = group 1
  "control", and each distinct drug CONDITION among "Drug Treated" samples = its own group 2..N, so `build_groups.sh`'s
  every-group-vs-group-1 yields **one `PSI.<GSE>.<drug>_<dose/time>_vs_control.txt` per condition**. The condition LABEL (=group
  key =output-file stem) is `<GSE>.<canonical drug>[_<dose>][_<timepoint>]`, built by `_condition_label` from the `GSE_Series` +
  canonical `drug`/`dose` columns PLUS a duration token parsed from the raw `treatment` text (the timepoint, e.g. MDSL's `8h`/`20h`,
  lives ONLY there — not in drug/dose). Zero new AI. If per-condition isn't viable (< 2 groups with ≥ `MIN_PER_GROUP`=2 samples, e.g.
  all-singleton conditions or no replicated control) it **falls back to the old binary** `drug_treated`(2)-vs-`not_drug_treated`(1)
  so a run is never worse off. (Undetermined still dropped.) **Baseline choice:** a SINGLE control-study → ONE pooled `control`
  group, no comps spec (build_groups does all-vs-control) → clean `..._vs_control.txt`. **MULTI control-study** (≥2 GSEs that each
  carry their own controls — e.g. A549's 33 studies) → `_pergse_groups`: PER-GSE control groups (`<GSE>.control`) + an explicit
  `sample_comps.tsv` pairing each condition to its OWN study's controls (no cross-study batch confounding) →
  `PSI.<GSE>.<drug>_vs_<GSE>.control.txt`. A drug-GSE with NO controls of its own BORROWS the technically-NEAREST control study via a
  nearest-neighbor match on batch covariates (`_nearest_control_gse`: instrument, read length, library prep — `_TECH_FEATS`); the
  borrowed study shows in the filename + is logged. Core is pure/unit-tested; validated on MDSL (single → control + Db2115_8h/20h)
  AND A549 (multi → 261 per-GSE matched comps; control-less GSE310111 → GSE162281 via NN). Applied LIVE to the running A549 psi
  (sample_groups.tsv + sample_comps.tsv + build_groups.sh redeployed; launcher will use them when BED completes). **Phase B
  (user-defined groups):** `group_cfg` from the UI (a "Comparison groups" editor: name + keywords + a control flag) →
  `group_assign.assign` (pipeline `_psi_group_inputs` hook, BEFORE build_psi_bundle) classifies each run "fixed code first,
  AI for the remainder": a deterministic keyword match on the FULL metadata row + `is_control`/compound-map for the control
  group, then ONE `llm_providers.classify` call (parametrized by the user's group names, fed the whole row) for the rest;
  unresolved → dropped (counted in `group_assignment_audit.csv`). It writes an **additive `group` column** (the existing
  `drug_treated` + splicing filter are untouched), and `build_psi_bundle` ships that column instead of the default. **Honest
  limit (by design):** auto-grouping only resolves axes the metadata actually carries (treated/control, drug-vs-drug);
  samples with no signal are dropped, never guessed. **CONFIRM ON THE FIRST LIVE RUN (not yet cluster-tested):** that
  AltAnalyze runs groupless cleanly when `--groupdir/--compdir` are omitted; that it tolerates `__intronJunction.bed`/`__exon.bed`
  sitting beside `__junction.bed` in the bedDir; the exact `groups.txt` sample-key AltAnalyze expects (currently `<BioSample>.bed`
  — flip `GROUP_KEY_SUFFIX` if it wants the bare stem or `__junction.bed`); and that GO-Elite is left off unless a comparison runs.
- **Phase-range control (run only part of the pipeline).** `progress.CHECKPOINTS` is the ordered list of START-able
  PHASES (fetch / extract / prep→build / select / runtable / download / STAR / BED); the web UI's LEFT vertical
  dual-handle slider (`#phaserail` in server.py) picks a START and END phase. `RunConfig.start_stage`/`end_stage`
  (+ `supplied_inputs`) drive it. In `run_pipeline`, `begin()` skips any stage outside `[start_stage, end_stage]`
  (via `_idx`/`in_range`); `_inject_start_artifacts` copies user-supplied artifacts (e.g. a `by_study/` folder or a
  `cellline_selection.json`) into the run dir at their Paths location and pre-marks earlier stages done (idempotent →
  resume-safe). The `progress.RunReporter.complete_stage` guard (`status=='skipped' → return`) stops the unconditional
  `complete_stage` calls from flipping a pre-skipped stage to "done". **Cluster-start wait bypass:** starting at STAR/BED
  means the prior cluster stage never writes the `PIPELINE_COMPLETE.txt` the launcher polls, so
  `submit_{star,bed}_over_ssh` take `prior_skipped` and pre-`touch` that sentinel (same SSH command, before the launcher
  bsub) so the stage runs NOW on the FASTQs/BAMs already on the cluster. `cfg.deep_dive` is forced True for any start
  at/after `select` (the cluster stages live inside the deep-dive branch, which loads `deep` from the injected
  cellline_selection). Ending early just doesn't deploy the later bundles (the auto-chain kick is then a harmless no-op);
  BUT a cluster download ALREADY running arms its own star/bed launchers, so for a hard stop end before `cluster_bundle`.
- **Disk hygiene: consume-as-you-go FASTQ deletion, separated BED outputs, tool cleanup (all default ON).**
  `run_star_job.sh` deletes a sample's SOURCE FASTQ(s) (`DELETE_FASTQ_AFTER_BAM`) the moment its BAM is
  published + quickcheck-verified — only the `by_study` ORIGINALS (`FASTQ1`/`FASTQ2`), never the staged `$WORK`
  copies (those are trap-removed). BED outputs no longer land beside the BAMs: `bed_template/config.sh` derives
  **`BED_OUT_DIR`** = `<dirname BAM_INPUT_DIR>/STAR_beds` (a SIBLING of STAR_bams; files together so AltAnalyze's
  per-sample junction+exon pairing survives). The AltAnalyze tools still write each `.bed` beside the BAM (forced,
  no output-dir arg) and `run_bed_job.sh` `mv`s them into STAR_beds (atomic rename — same NFS volume). `bed_done()`
  + `_BED_STATUS_PROBE` read STAR_beds; run_bed_job/watchdog self-derive BED_OUT_DIR (`: "${BED_OUT_DIR:=...}"`) so
  they work even against a pre-BED_OUT_DIR deployed config (lets you live-patch a running deploy with scripts only).
  **Tool cleanup** (`CLEANUP_TOOLS_WHEN_DONE`, on COMPLETE only — STALLED stays inspectable): STAR finalize removes
  the now-empty `by_study/` + the download bundle scripts (`<dlroot>/*.sh`,`*.py`); BED finalize removes the vendored
  `altanalyze/` (toolkit + 100 MB ref) + the `star/` bundle + leftover download scripts. KEPT: BAMs, SJ.out.tab,
  STAR_beds, all PIPELINE_COMPLETE markers, logs. **No self-deletion** — each finalize only deletes ANOTHER stage's
  tooling (the running watchdog's own dir/scripts are never removed); paths are guarded (`[ "$dlroot" != "/" ]`).
  **Two GUI toggles** (server.py `#del_fastq` default ON, `#del_bam` default OFF) drive `DELETE_FASTQ_AFTER_BAM`
  (→ star_cfg → STAR config, exposed via `STAR_CONFIG_DEFAULTS`) and **`DELETE_BAM_AFTER_BED`** (→ bed_cfg → BED
  config, `BED_CONFIG_DEFAULTS`); the latter makes `run_bed_job.sh` delete each BAM (+ .bai) once its BEDs are
  made + verified. Since BAMs then disappear mid-run, `_BED_STATUS_PROBE`'s total reads `bed/bam_list.tsv` rows
  (the fixed denominator) rather than a live `*.bam` count. RE-APPLY on re-vendor.
- **STAR workspace: test writability BY ACTION, never `[ -w ]`/`df`.** On the lab NFS (`/data/salomonis-archive`),
  from COMPUTE NODES `[ -w dir ]` returns false and `df` returns empty even where `mkdir`/`touch` actually succeed —
  this silently killed EVERY STAR job ("no workspace with >=20G free") despite a 26T volume, while `/scratch` was
  100% full. `run_star_job.sh` now uses `can_write()` (mkdir a probe dir) and falls back to a dedicated
  **`$PIPELINE_ROOT/_workspace`** folder (the BAM_OUT volume, where outputs land anyway) when TMPDIR/SCRATCH lack
  room — so a full `/scratch` (or a flaky df / lying `[ -w ]`) can never block alignment. STAGING (copying the
  FASTQs into the workspace) runs ONLY to fast LOCAL disk; on the NFS `_workspace` fallback STAR reads the by_study
  FASTQs IN PLACE (no pointless NFS→NFS copy — `_workspace` then holds only STAR's 2-pass temp + the BAM). NFS temp
  is slower; free `/scratch` for speed. RE-APPLY on re-vendor.
- **Editing UTF-8 files:** use the Edit/Write tools. From PowerShell never `Get-Content -Raw` then `WriteAllText`
  (PS 5.1 reads no-BOM as cp1252 and double-encodes em-dashes/arrows into mojibake); use
  `[System.IO.File]::ReadAllText(path,[Text.Encoding]::UTF8)` + `WriteAllText`. Keep all `*.sh` LF.
- **Signatures:** a `# Signed Nicholas Krol` line exists ONLY on `normalize_v2.py`. Do not re-add elsewhere.
- **Reliability hardening (2026-06-07 audit; DON'T re-break).** A weak-links audit (18 fixes) hardened the
  self-driving chain and the control plane. The load-bearing invariants:
  - **`bjobs` is unreliable EMPTY *or* PARTIAL under load.** Every watchdog now: captures bjobs **rc**
    (`LIVE="$(*_snapshot)"; *_SNAP_RC=$?` → rc!=0 skips the pass), **re-verifies each missing sample with a
    targeted `bjobs -J`** before resubmitting (a partial bulk snapshot can't fool it), and applies a **sanity
    floor** (live-count collapse >50% with no progress ⇒ skip). The `*_snapshot` helper only EMITS — its rc is
    read at the call site (`$(…)` is a subshell, so a global set inside is lost). NEVER resubmit/finalize off a
    single unverified snapshot.
  - **One pass at a time + finalize exactly once.** A per-pass `flock -n` on `.watchdog.run.lock` serializes
    overlapping passes (nudge + timed + double-arm); `finalize()` claims an atomic `mkdir .finalized.lock`
    (NFS-safe) and `bkill`s ALL `${JOB_TAG}_watchdog` but itself.
  - **Absolute backstop:** STALL after `${ABSOLUTE_MAX_PASSES:-960}` passes or `${MAX_WALL_HOURS:-336}`h, plus a
    no-churn detector — a permanently-PENDING job can no longer loop forever with no signal.
  - **STALLED is NOT a clean GO.** When a downstream launcher proceeds on an upstream STALL it writes
    `PIPELINE_INCOMPLETE_UPSTREAM.txt`; finalize then writes `PIPELINE_COMPLETE_PARTIAL.txt`, **disables all
    destructive cleanup + FASTQ/BAM deletion**, and reports honestly. FASTQ delete is also gated on a successful
    `samtools index` + `MIN_MAPPED_FRAC`; a not-done sample whose source is already gone logs UNRECOVERABLE
    instead of resubmitting a doomed job. This closes the silent partial-dataset-then-purge cascade (audit #1).
  - **Atomic publish:** `bed_file_ok` (trailing newline + parseable last row) gates the BED `mv`;
    `make_sample_list.py` uses tmp+`os.replace`.
  - **Injection / control plane:** `cluster_deploy.shq()` POSIX-quotes every value into remote ssh/bsub commands
    + status probes; `_shval` escapes `$`/backtick inside config.sh double-quotes (allowlisting `$USER` paths);
    `server.py` POSTs get an Origin/CSRF check + a token gate on non-loopback binds. `/api/settings` never serves
    the live `ssh_password` (also no longer persisted), but the API/NCBI keys still PREFILL on the default loopback
    bind (they're already in the local plaintext settings file) and are withheld only on a non-loopback/token bind.
    The 4 `_start_run` `self._send_json` NameErrors (dead validation) are fixed.
  - **Live-patch safety:** all new logic uses `${VAR:-default}` so it runs against an un-replaced config.sh; the
    STRICT predicates (`STRICT_BAM_CHECK`/`STRICT_BED_CHECK`/`MIN_MAPPED_FRAC`) default OFF and are only set ON in
    NEW runs' generated config — so a mid-run script swap never re-evaluates already-done BAMs/BEDs. Always deploy
    `lib_*.sh` together with its `watchdog.sh`/`run_*_job.sh`. RE-APPLY on re-vendor.
  - **NEVER count with `grep -c` in cluster code — it returns EMPTY on the compute nodes** (LIVE bug 2026-06-07).
    grep *matching* works there (`grep -q`/`grep -E` are fine), but `grep -c`'s count output comes back blank, which
    set the watchdog's `nlive=""`/`exp_n=0` → the completion test `[ "" -eq 0 ]` errored → the stage looped forever
    despite all BEDs done (it is NOT reproducible from the head node, where `grep -c` works). All gate-critical
    counts now use PURE-BASH `while`-read loops (`bed_count_work`/`star_count_work`/`sra_count_work`, loop-based
    `*_expected_count`) — the same primitive `*_done_count` already used reliably. Diagnose by reading the live
    `watchdog.log` (look for an empty `nlive` in the `progress:` line), not by re-reading the script (the deployed
    code md5-matches the template; the bug is the runtime environment).

---

## 7. Verify after edits (from the project dir)

- `python -c "import pipeline, server, llm_providers, cluster_deploy, star_deploy, bed_deploy, psi_deploy, group_assign, build_final, ai_clean, cellline_match, structured_extract, runtable_fetch, runtable_build, progress, stage_docs, pipeline_paths"`
- `python -c "import server; server._page()"` (page builds) — and `node --check` on the extracted inline `<script>`
  (JS lives inside a Python string, so Python won't catch JS errors). `node` is available; if git-bash is present you
  CAN `bash -n psi_template/*.sh` locally (otherwise shell scripts are verified on the cluster).
- `python pipeline.py --validate-runtable` must print `VALIDATION: PASS`.
- Useful spot checks: a cap-3 `--skip-ai --no-deep-dive` run exercises fetch/extract/build live; `star_deploy.build_star_bundle`
  on a synthetic run dir dry-runs the STAR config fill (no cluster needed); `psi_deploy.build_psi_bundle` on a synthetic
  run dir with a `SraRunTable_<slug>.csv` (BioSample + drug_treated cols) dry-runs the PSI config + `sample_groups.tsv` +
  the launcher; `group_assign.assign` with a `group_cfg` + `skip_ai=True` dry-runs the deterministic group pass (writes the
  additive `group` column); `cluster_deploy.parse_psi_status(...)` is unit-testable with no SSH.

---

## 8. Status & next steps

**Built + locally verified** (imports, page build, `node --check` JS, `--validate-runtable` PASS, byte-identical
filter parity, parsers, STAR bundle dry-run): the deterministic chain, web UI, deep dive, 3-way drug-treated,
cluster download handoff, analysis modules + module-tied filter, the STAR alignment module (auto-chain + genome
resolution), parallel NCBI fetch, AI preflight/fix-or-disable + rate-limit retry, custom OpenAI base_url,
instance-scoped on-demand cluster status.

**AI run live:** Gemini `gemma-4-31b-it` works (tool-calls correct) but a free-tier key is heavily throttled →
enable billing for scale. MiMo `mimo-v2.5` via `https://api.xiaomimimo.com/v1` (custom base_url) works **only with reasoning disabled** —
it's a reasoning model whose thinking otherwise exhausts `max_tokens` and truncates every batch ("no output");
tick **Disable model reasoning** / `--disable-reasoning`. Verified live: 250/250 coverage, names lowercased,
zero controls mislabeled, `is_drug` 100% self-consistent; canonicalization quality on par with — and on prose
protocols BETTER than — reasoning-on (which truncates the batch anyway). Anthropic/
OpenAI proper not run live, but their paths are unchanged. The cluster DOWNLOAD was verified live earlier (real
LSF jobs on bmiclusterp-head).

**Live cluster run (in progress):** `bulk_rna_seq` + autonomous ran end-to-end on the real LSF cluster — SRA
download, per-study conversion, the self-rescheduling `star_launch.sh`, AND **STAR alignment** are all confirmed
working (sra2/MDS-L: 18 BioSamples aligning into the GRCh38 index, one BAM each). Three issues found + fixed LIVE
this session, all deployed to the active roots: (1) a download watchdog TERM_RUNLIMIT-killed while blocked on the
pending-job threshold (1600+ queued) → **reschedule-first** hardening; (2) the launcher lagging up to two 30-min
ticks behind a finished download → the **watchdog kicks the launcher** on finalize; (3) every STAR job dying "no
workspace >=20G" because `[ -w ]`/`df` lie on the compute-node NFS while `/scratch` was 100% full → **can_write-by-
action + `$PIPELINE_ROOT/_workspace` fallback**. **Still to confirm:** STAR running to full completion (BAMs
publishing + watchdog COMPLETE); a clean small `--cap 25` AI quality check on a billing-enabled key. The lab GRCh38
index is at `/data/salomonis2/Genomes/STAR-2.7.10b-Index-GRCH38/Grch38-STAR-index` (GENOME_DIR field or the registry).

**Standing instruction:** keep this handoff updated on every change (the user asked for this explicitly).
