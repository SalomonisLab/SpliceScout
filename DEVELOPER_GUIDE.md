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
| `runtable_annotate.py` | Stage 12 — drug/dose/is_control + 3-way `drug_treated` on the filtered table + `.xlsx`. **Treatment columns** = `treatment_columns(header)`: case-insensitive, the SAME tag set as the headline count (`structured_extract.COMPOUND_TAGS`); `pick_treatment` reads ALL of them (first value naming a drug wins; a run is a control only if every value says control); `control_like` = `is_control` on the raw OR dose/replicate-normalized value. A depositor `drug` attribute is preserved as **`drug_original`** before the canonical `drug` column is written (2026-09-18; see §9) |
| `cluster_deploy.py` | Stages 13-14 — `build_bundle` + `submit_over_ssh`; `diagnose_failure` + fix/retry; `remote_status`/`remote_star_status`/`remote_bed_status`/`remote_psi_status` (on-demand progress over SSH) + **`remote_alerts`** (ONE `find` for any stage's STALLED/ORPHANED/LAUNCH_TIMEOUT marker across all runs → the silent-stall heads-up) + **`send_alert_email`** (emails via the cluster's own `mail`, base64 body, no PC SMTP); reads `cluster_template/`. `fill_config`/`_shval` + `_submit_systemssh`/`_submit_paramiko` reused by star/bed/psi_deploy |
| `cluster_template/` | Vendored LSF **download** pipeline (source of truth: `Downloads/SRA_pipeline_template/`); only config.sh is regenerated. **Drop-after-`MAX_FAILS` (=3):** `lib.sh` tracks per-accession attempts in `$PIPELINE_ROOT/.attempts/<acc>.n`; `fetch_missing.sh` (download) + `watchdog.sh` §1 (conversion) bump it and, past `MAX_FAILS` (3) fails, `sra_drop_acc` writes a `.dropped` marker + appends to `dropped_accessions.txt` + clears the stranded `.sra`; the completion gate counts `total_done + dropped` so one undeliverable run can't stall the study forever (user 2026-06-17) |
| `star_deploy.py` | Stages 15-16 (bulk_rna_seq) — `build_star_bundle` + `submit_star_over_ssh` (self-rescheduling launcher); `detect_organism` |
| `star_template/` | Vendored STAR 2-pass aligner (consumes the download's fastq.gz). Adds `lib_index.sh`/`resolve_index.sh`/`build_star_index.sh` for genome-index resolution + build-once. **Drop-after-`STAR_MAX_FAILS` (=3)** (2026-06-22): a sample that fails alignment 3× (or whose FASTQ is gone) is dropped → `star_dropped.txt`, completion = `done_n+dropped_n>=exp_n`, + a >10% meltdown→STALL guard — parity with BED/download |
| `star_index_registry.json` | organism → prebuilt index path (`organisms`, read on the cluster) + build-once FASTA/GTF URLs (`reference_urls`, read in Python) |
| `bed_deploy.py` | Stages 17-18 (bulk_rna_seq) — `build_bed_bundle` + `submit_bed_over_ssh` (self-rescheduling launcher waits on STAR); `_upload_ref_idempotent`; `organism_to_species` |
| `bed_template/` | Vendored BAM→BED (AltAnalyze junction/exon) stage incl. the `altanalyze/` toolkit + ~100 MB exon ref. **Exon-OPTIONAL in `BED_MODE=both`** (2026-06-22): `run_bed_job.sh` drops a truncated `__exon.bed` but still publishes junction+intronJunction, and `bed_done`'s `both` case no longer requires exon (PSI uses junction+intronJunction; the exon pass OOM-truncates on huge BAMs). **Drop-after-`BED_MAX_FAILS` (=3):** per-sample attempts in `$PIPELINE_ROOT/.attempts/<label>.n`; `watchdog.sh` drops a BAM that fails conversion 3× (or is BAM-gone) → `bed_dropped.txt`, completion gate = `done_n + dropped_n >= exp_n`, so a few bad BAMs can't STALL the stage. (BED `finalize()` does NOT kick PSI — `psi_launch.sh` polls for BED's `PIPELINE_COMPLETE.txt`.) |
| `psi_deploy.py` | Stages 19-20 (bulk_rna_seq) — `build_psi_bundle` + `submit_psi_over_ssh` (ONE AltAnalyze job; **find-or-upload** AltAnalyze; writes `sample_groups.tsv` [+ `sample_comps.tsv`] from the run table via `_build_default_groups`, plus **`compound_funnel.tsv`** (`_write_compound_funnel`: per compound, the step it stopped at — also copied to `runtable/`)); launcher waits on BAM→BED |
| `psi_template/` | Vendored AltAnalyze splicing (PSI) stage — **single-job** self-driving watchdog; `build_groups.sh` builds groups.txt/comps.txt cluster-side (shipped map ∩ present BEDs) + `groups_attrition.tsv`; **`whitelist_job.sh` + `junction_whitelist.py`** = the manuscript's junction PREVALENCE filter (`JUNCTION_PREVALENCE_TAU`, default 0.01; filtered copies in `junction_beds_filtered/`), run by `run_psi_job.sh` right before AltAnalyze. AltAnalyze.py itself is NOT shipped (resolved on the cluster) |
| `concordance_deploy.py` | Stages 21-22 (bulk_rna_seq) — `build_concordance_bundle` + `submit_concordance_over_ssh`; auto-selects a cancer atlas from the cell line (`cancer_atlas_registry.json`), reads `concordance_template/` (EVERY file in it is bundled); launcher waits on PSI |
| `concordance_template/` | Vendored splicing-concordance stage — **single-job** self-driving watchdog; `gather_signatures.sh` (study-matched filter, `STUDY_MATCHED_ONLY`); the VENDORED `splicingConcordance_advanced.py` scorer (also writes **`pair_stats.tsv`**: per-pair n/same/opposite + inclusion counts) + `rank_concordance.py` ranker (analytic-null significance, ≤`SUMMARY_ROWS_PER_DRUG` rows per drug, EVERY compound listed) + **`score_with_null.py`** (cross-atlas BH → `results/scored_pairs_with_null.tsv` + `concordance_by_compound.tsv`; optional Mann-Whitney `ENRICH_AGENTS`) vs cancer-subtype atlases (AML-OncoSplice / LUAD+LUSC / TCGA). Reuses the PSI-resolved AltAnalyze on PYTHONPATH |
| `rerun_deploy.py` | **Targeted re-run / concordance re-score kit builder** (CLI, not a DAG stage) -- `build_rerun_kit`: builds the run's current PSI plan + concordance into `runtable/rerun/build_<mode>/` (never over the run's own bundles), retargets them to NEW cluster folders, writes `rerun_plan_runs.tsv` + `rerun.env`, zips `SpliceScout_<mode>_<LINE>.zip`; promotion into the original folders by default. See §9i |
| `rerun_template/` | Cluster side of a re-run kit: `rerun_submit.sh` (full), `rescore_submit.sh` (concordance only), `recover_nested.sh` (LSF array: unpack nested/gzipped downloads of older deployments), `promote_launch.sh` (moves finished results into the original folders, old contents kept as `.old_<date>`), `README.txt` |
| `cancer_atlas_registry.json` | cell line → cancer atlas (query dirs + patient-count source) for the concordance stage; GUI `cancer atlas` field overrides |
| `group_assign.py` | Phase B — user-defined comparison groups: deterministic (keyword on the full row + `is_control`/compound map) then AI for the rest → writes the **additive** `group` column + `group_assignment_audit.csv` |
| `plot_data.py` | Plots-tab data — per-RUN dataset from the picked line's filtered run table; dynamic numeric/categorical field lists |
| `stage_docs.py` | `STAGE_DOCS` for the UI's clickable step-doc modal (injected as `__STAGE_DOCS__`) |
| `vendor/plotly.min.js` | vendored Plotly (offline; served at `/plotly.js`, lazy-loaded) |
| `normalize_v2.py` | shared dose/control normalizer (`clean_compound`/`normalize_compound`/`is_control`) — the ONE signed file. `is_control` also catches `STRONG_CONTROL_TOKENS` (mock/uninfected/sham/untreated/parental/…) when underscore/hyphen-joined to a model prefix (e.g. `SARS2_Mock`), which the residue logic missed; solvent words (DMSO/vehicle) stay residue-based so a drug-in-DMSO isn't mislabeled control. **2026-09-18:** the residue test also strips FRAMING words (treated/exposure/with/for/cells…), replicate tags and day/min durations, treats `_` as a word break (`DMSO_24h`, `vehicle_control`), accepts a lone negation (`Not treated`, `non-treated`, `without treatment`), and a value whose dose tokens are ALL zero (`0 uM`, `Erlotinib 0 nM`) is a control (`_zero_dose_only`, also in `clean_compound`) — 58-case regression list in §9 |
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
limit (0.13s≈7.7/s with a key, 0.36s≈2.8/s without — a safety margin under NCBI's 10/3) while responses overlap — so the API key now yields ~3x. The
rate limiter, NOT the thread count, bounds req/s. Extract's resume checkpoint is batched (every 25 + final), safe
because jsonl rows are deduped by GSM in build_final.

**Cluster download handoff (13-14).** `cluster_mode` ∈ off | manual (zip) | autonomous (ssh upload + launch).
PER-PROJECT ISOLATION: deploys to `PIPELINE_ROOT/<instance>_<cellslug>` (`cluster_deploy.project_job_tag`; the entered root is the PARENT — see "Multiple instances" below). Ships PER-STUDY
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
**[SUPERSEDED for WATCHDOG passes on 2026-06-29 — see "Watchdog walltime = dead-man's-switch" in §6: a watchdog `-W` must stay BELOW its reschedule interval (download: `INTERVAL-5`; STAR/BED/PSI/concordance: `-W 20` < the 30-min interval). Work jobs, launchers and diagnose keep the queue max.]** **EVERY job runs at `-W 66480` (the normal-queue MAX, `1108:00` ≈ 46 days) — "everything to max" (2026-06-25).** Work jobs + launchers already did; the watchdog/poller passes, the `cs` convert (`-W 30`), and the `diagnose` job (`-W 90`) were the holdouts and are now bumped too (concordance's work `WALL` also `48:00`→`1108:00`). WHY: a watchdog/launcher pass that blocks on the pending-job threshold (a saturated per-user quota, e.g. the A549 load test at 3,300+ PEND) used to hit its `-W 20` walltime and get `TERM_RUNLIMIT`-killed BEFORE its reschedule-first `bsub` could even queue a successor → the whole self-driving chain died (observed LIVE: `MDS_L_mdsl` downloaded 78/78 then STAR never launched). At the queue max, a blocked reschedule has ~46 days to clear the threshold, so nothing dies to walltime. Reschedule-first + the `-W`→queue-max self-heal on a `TERM_RUNLIMIT` resubmit remain as defense-in-depth. Bumped byte-level in all 5 templates + `diagnose_ai` (LF-safe); **live runs keep their old `-W 20` until re-deployed — surgically patch the deployed `*.sh` with `sed -E 's/-W (20|30|90) /-W 66480 /g'` + `bash -n` + `.prewallfix.bak`** (done for the live MDSL run). NOTE: the separate no-progress backstop `MAX_WALL_HOURS=336` (14-day STALL) is unchanged — it's a runaway guard, not a per-job walltime.
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
  with **nullglob**, NEVER `grep -c`) **AND (since 2026-09-18) the comparisons are finished: AltAnalyze exited 0
  (`ALTANALYZE_OK.txt`, written by `run_psi_job.sh`) OR every requested comparison's dPSI file exists** — see §9b; a job
  that dies with no output is resubmitted up to `MAX_RESUBMITS` (then STALLED);
  same reschedule-first + flock + pass/wall backstop + `ended($LSB_JOBID)` nudge as the others. **AltAnalyze is NOT
  vendored** (multi-GB with its DB): `submit_psi_over_ssh` PROBES the cluster — if `$ALTANALYZE_HOME/AltAnalyze.py` + a DB
  (`ALTANALYZE_DB` override, else `$ALTANALYZE_HOME/AltDatabase`) are present it uses them IN PLACE (no upload); else if the
  user set `ALTANALYZE_LOCAL` (a local copy) it uploads it ONCE to `<psi_root>/altanalyze_home` and rewrites the local
  config.sh's `ALTANALYZE_HOME` BEFORE the bundle upload; else `setup.sh` flags it. Default `ALTANALYZE_HOME` = BLANK ->
  `$PIPELINE_ROOT/altanalyze_home` (the portable default, psi_template/config.sh); point it at the lab install
  `/data/salomonis2/software/AltAnalyze-91/AltAnalyze` in the GUI to reuse it in place (that path is only a built-in
  FALLBACK for the concordance scorer's PYTHONPATH, `concordance_deploy._ALTANALYZE_FALLBACKS`). Modules: `python/2.7.5` + `samtools` + `R` (matches the
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
  samples with no signal are dropped, never guessed. **RESOLVED LIVE:** AltAnalyze's RNASeq workflow does NOT run
  groupless ("No groups or comps files found ... exiting") -> `run_psi_job.sh` fails loudly without groups (STALL with a clear
  cause); `__intronJunction.bed` IS required beside `__junction.bed` (intron-retention events live only there); `__exon.bed` is
  excluded from the bedDir. **Still worth confirming:** that it tolerates `__intronJunction.bed`/`__exon.bed`
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

---

## 9. 2026-09-18 — full-code review fixes + the "130 compounds -> 2-3 in the summary" investigation

**The symptom.** A549's headline table shows ~130 unique compounds, yet the concordance summary listed 2-3 compounds
from a couple of studies. **Root causes (stacked, all verified in code):**

- **(1) The summary showed only the TAILS**: every C < 0.30 pair, plus the top 12 pairs above 0.70.
  - Rows were sorted by overlap size N, so a few compounds with huge signatures repeated across subtypes and filled
    the page.
  - Those huge signatures are typically cross-study, batch-dominated contrasts (manuscript: ~5x larger).
- **(2) Few drug signatures reached concordance at all.** The study-matched filter (correctly) quarantines every
  contrast whose control arm comes from another study, so any study WITHOUT >= 2 recognized controls of its own
  loses ALL its compounds. Controls were under-recognized for five reasons:
  - `normalize_v2.is_control` missed `Not treated`, `non-treated`, `Exposure to DMSO for 48 hours`, `DMSO_24h`,
    `vehicle_control`, `DMSO treated` and zero-dose arms (`0 uM`). Without AI, `Not treated` even became a compound
    named "Not".
  - `runtable_annotate` matched treatment columns EXACTLY and case-sensitively against a SHORTER list than the
    headline count.
  - It read only the FIRST non-empty column.
  - It overwrote a depositor `drug` column with its own output.
  - `drug_treated_label` only tested the RAW string (`DMSO_rep1` -> Undetermined).
- **(3) Grouping losses.**
  - Un-replicated conditions (drug x dose x time with 1 BioSample) were dropped.
  - "Not Drug Treated" non-drug perturbations (siRNA/KO/infection) were pooled INTO the control baseline.
  - With exactly ONE control study, every other study was compared to a pooled `control` label carrying no GSE,
    so the cross-study filter could not see it.
- **(4) Cluster side.** The PSI preflight quarantined samples whose `__intronJunction.bed` is legitimately empty,
  which can take a control group below 2 and silently remove every comparison of that study.

**Fixes (this session):**

- **Summary** (`rank_concordance.py`):
  - Significance from each pair's own analytic null (reads the scorer's new `pair_stats.tsv`); BH within the atlas.
  - At most `SUMMARY_ROWS_PER_DRUG` (3) rows per drug in the candidate tables.
  - A final "EVERY SCORED COMPOUND" section.
  - `all_scored_pairs.tsv` gains `null_pi0`, `p_value` and `q_atlas`.
  - Falls back to the legacy 0.30/0.70 cut-points when `pair_stats.tsv` is absent (older scorer).
- **Scorer** (`splicingConcordance_advanced.py`): also writes `pair_stats.tsv`; `concordance.txt` is unchanged.
- **Cross-atlas null** (`score_with_null.py`, NEW; python 2/3, stdlib):
  - Implements pi0 = pP·pD + (1−pP)(1−pD) with an exact two-sided binomial test.
    - Verified against brute force: max |Δp| = 5.5e-12 over 3,000 cases.
  - BH across all atlases -> `results/scored_pairs_with_null.tsv`, the file the manuscript figure scripts read
    (columns match `make_figures.py` / `make_benchmark_figure.py`).
  - Also writes `concordance_by_compound.tsv`.
  - Optional one-sided Mann-Whitney (`ENRICH_AGENTS`). It reproduces all 6 rows of manuscript Table 4 exactly
    (e.g. U2AF1-S34 z=−3.67, p=1.2e-4).
  - `run_concordance_job.sh` runs it after all atlases.
  - New config keys: `MIN_OVERLAP` = 25 (significance floor, manuscript |E|≥25), `FDR_ALPHA`,
    `SUMMARY_ROWS_PER_DRUG`, `ENRICH_AGENTS`, `STUDY_MATCHED_ONLY`.
- **`gather_signatures.sh`**:
  - `_is_cross` also quarantines `MULTISTUDY` baselines.
  - Stale signature copies are cleared before each gather.
- **`normalize_v2`**:
  - The residue test strips framing words, replicate tags and durations; `_` counts as a word break.
  - A lone negation (`Not treated`, `non-treated`, `without treatment`) is a control.
  - All-zero doses are controls.
  - Regression list (58 cases, 0 failures): `Not treated`, `Exposure to DMSO for 48 hours`, `DMSO_24h`,
    `vehicle_control`, `0 uM`, `Erlotinib 0 nM` are controls; `treated with cisplatin`, `DMSO + cisplatin`,
    `inhibitor`, `0.5 uM erlotinib`, `Cisplatin in DMSO`, `no dox`, `CD133 positive` are not.
- **`runtable_annotate`**: `treatment_columns` / `pick_treatment` / `control_like` plus `drug_original`
  preservation (see §3). `group_assign` uses the same rules; its vehicle group on the U-87 test went 31 -> 253 runs.
- **`psi_deploy._build_default_groups`**:
  - The control baseline is `is_control=yes` runs, plus runs with no treatment text (AI-title calls, kept as before).
  - Perturbations are excluded from both arms.
  - The per-GSE matched path is used whenever ANY study has >= 2 controls (was >= 2 studies).
  - Pooled/binary baselines are labelled `<GSE>.` or `MULTISTUDY.`.
  - `POOL_THIN_CONDITIONS` rescues single-BioSample conditions by pooling the same drug's doses, then timepoints,
    WITHIN the study (`…_pooled` labels).
  - `compound_funnel.tsv` names each compound's stopping step: `not_in_run_table`, `unreplicated`,
    `cross_study_only` or `compared_same_study`.
- **PSI preflight**: an empty `__intronJunction.bed` is valid (`lib_psi.sh psi_check_beds`).
- **Junction prevalence filter** (manuscript Methods): NEW `psi_template/whitelist_job.sh` + `junction_whitelist.py`.
  - Junction key = chromosome + the `JUNC<n>:<donor>-<acceptor>` BED name field, de-duplicated per library.
  - Threshold round(τN), matching the manuscript's 63 (A549) / 25 (K562).
  - Writes filtered copies; intron BEDs pass through.
  - Outputs `junction_prevalence_summary.tsv`; idempotent via `.whitelist_done`; a no-op when τN rounds to <= 1.
  - Runs inside `run_psi_job.sh` after the "already done" check.
- **Measured on real metadata** (the 9 U-87 MG studies in `Documents/sra_ids`, skip-AI, same code path as a run):
  - Before: 2 studies lost their own controls (`Not treated`, `Exposure to DMSO…`), `Not` was counted as a
    compound, and 192 of 203 compounds reached a same-study comparison.
  - After: 5 control studies, and 194 of 201 compounds reach a same-study comparison.
  - The remaining 5 cross-study-only compounds are the dbcAMP time courses whose only baseline is a `0h` arm — an
    honest loss.

**Other verified bugs fixed:**

- **Server/UI:**
  - `server._LOOPBACK_HOSTS` contained `0.0.0.0` -> `--host 0.0.0.0` got NO token.
  - The live log froze after 600 lines; the UI now keys on `progress.snapshot()['log_total']`.
  - `chat_assist` `list_runs`/`get_run_status` read `current` as a dict; `_current_stage` fixes it.
  - `/readme` now serves USER_GUIDE.md (which mirrors README.md).
- **Pipeline:**
  - Resume never armed STAR/BED/PSI/concordance after the upstream submit completed in an EARLIER session; the
    `*_go` gates now also accept `done(<upstream>_submit)`.
  - The CLI `--end-stage` default is `concordance_submit` (was `bed_submit`, so CLI runs never reached
    PSI/concordance).
  - FETCH writes every id to `uids` but records only successful esummaries -> extract/build `KeyError`; missing
    records are now skipped and counted.
  - `build_final`: an AI `Unknown` cell line no longer overrides a valid structured tag (`_ai_cell`).
- **LLM providers:** `llm_providers._create_with_reasoning_fallback` — the compound pass always asks for reasoning
  OFF; an endpoint that 400/422s on `NO_REASONING_BODY` is retried without it and remembered per (base_url, model).
- **STAR:**
  - `star_template/build_star_index.sh`: its 2nd `trap … EXIT` replaced the lock-release trap, so `.buildlock`
    was never removed. Now one `_buildidx_cleanup`, plus a FASTA/GTF chromosome-naming guard.
  - `star_index_registry.json`: Ensembl-111 GTFs to match the Ensembl FASTAs (was GENCODE `chr*`), and the lab
    GRCh38 index is filled in `organisms["homo sapiens"].index_dir` (validated before use).
- **Cleanup:** STAR finalize AND BED `bed_cleanup_tools` removed `by_study/` unconditionally, overriding keep-FASTQ
  and deleting never-aligned samples. They now remove it only when it holds no FASTQ.
- **Download:**
  - `fasterqdump_job.sh` publishes via hidden temps + rename.
  - The watchdog checks the live converter BEFORE the converted test and treats a leftover `.part` temp as NOT
    converted. Before, a job killed between `_1` and `_2` had its `.sra` deleted and lost `_2` forever.
- **Phase-start shortcuts:** the BED/STAR `prior_skipped` sentinel touch is now guarded like PSI's (only if no
  upstream `watchdog.log`).
- **Repo hygiene:**
  - `bed_template/*.sh` converted CRLF -> LF.
  - The missing `bed_template/altanalyze/` + `vendor/plotly.min.js` were restored from `main`.
  - README/USER_GUIDE: 22 stages, current folder naming, outputs, and the "why fewer compounds" section.

**Verify after these changes:**
- Imports (§7) + `server._page()` + `node --check` on both inline scripts.
- `bash -n` on all 59 shell scripts; no CRLF in any `*.sh`.
- `python pipeline.py --validate-runtable` still hits NCBI (not re-run in this session).

**Cluster-side items NOT live-tested yet** (scripts syntax-checked, Python unit-tested on synthetic data):
- `whitelist_job.sh` on a real cohort (memory ~34 GB for A549).
- The scorer's `pair_stats.tsv` under python 2.7.5.
- `MULTISTUDY` quarantine.
- The atomic `fqd` publish.
- Rebuilding the STAR index with the Ensembl GTF.

### 9b. Follow-up (same day) — "every compound in the summary comes from just 2 studies"

That is a STUDY-level loss. Whole studies can drop out at these points:
1. **A study has no usable controls of its own** (< 2 recognized control BioSamples). Its conditions can only be
   compared cross-study, and the study-matched filter quarantines that.
2. **A comparison group falls below 2 samples** at the BED intersection. `build_groups.sh` drops every comparison of
   that study; there is no fallback.
3. **AltAnalyze never writes the study's dPSI files.**
4. **The signatures share < 5 events with every cancer subtype.**

**(3) was a real bug — `psi_done` treated the per-sample `EventAnnotation` table as DONE.**
- AltAnalyze writes that table BEFORE the per-comparison `Events-dPSI_*/PSI.<cond>_vs_<ctrl>.txt` files. The PSI
  work job's exit code was ignored by the watchdog: done + nlive==0 -> COMPLETE.
- So an AltAnalyze run that crashed, was killed, or froze part-way became COMPLETE with only the FIRST
  comparisons written.
- Comparisons are numbered by sorted label, i.e. sorted GSE, so the first comparisons belong to the lowest-numbered
  studies. Concordance then ranked a couple of studies and silently dropped the rest.
- The job body's idempotency check (`if psi_done; then skip`) used the same predicate, so a resubmit never
  finished the missing comparisons.
- **Reproduced:** on a tree built from the real U-87 plan with 10 of 205 comparison files written (2 studies), the
  old `psi_done` = TRUE and the new one = FALSE.

**Fix:**
- `psi_done` = the table AND (`ALTANALYZE_OK.txt` OR every requested comparison present). New helpers:
  `psi_comparisons_expected`, `psi_comparisons_produced` (distinct basenames across `Events-dPSI_*`, `.gz` counted),
  and `psi_comparison_report`.
- `run_psi_job.sh`:
  - clears the marker before each attempt;
  - captures AltAnalyze's rc;
  - ALWAYS writes `PSI_COMPARISONS.tsv` (requested comparison -> expected file -> produced? -> event rows);
  - writes `ALTANALYZE_OK.txt` only on rc 0.
- The watchdog report shows "N of M comparisons written". New §2c kills a FROZEN job whose comparisons are all
  written (a post-analysis tail such as the web-service hang) so it finalizes instead of waiting for walltime.
- `_PSI_STATUS_PROBE` emits `PSICOMPS <written> <requested>` + `CLEANEXIT`. The UI PSI banner shows
  "N / M comparisons written" and flags a COMPLETE-but-incomplete run.

**NEW diagnostics** (so a study-level loss is never silent again):
- **`concordance_template/study_coverage.py`** (py2/3, stdlib; ships in the bundle):
  - Per study: plan (`sample_groups`/`sample_comps`) -> after BED (`ExpressionInput/groups|comps.*.txt`) ->
    dPSI files written -> gathered / quarantined -> scored, with the step where each study was lost.
  - `rank_concordance.py --psi-root --concordance-root` appends it to every ranked summary.
  - `run_concordance_job.sh` writes `results/study_coverage.txt` + `.tsv`.
  - Run it by hand on any EXISTING run: `python study_coverage.py --root <PIPELINE_ROOT>/<instance>_<line>`.
- **PC side:** `psi_deploy._write_study_funnel` -> `runtable/study_funnel.tsv` (+ `runtable/psi/`). Per study it gives
  treated / control / Undetermined / perturbation BioSamples, same-study / cross-study / un-replicated conditions,
  and a verdict.

**More causes of type (1), fixed:**
- **`normalize_v2`** (90-case regression, 0 failures) now recognizes:
  - TIME-ZERO arms (`_zero_time_only`: `1mM dbcAMP_0h`, `Day 0`, `0 min`, but not `0 h recovery` / washout /
    post), which are a time course's own baseline;
  - `Ctrl_24h`, `NT`, `baseline`, `pre-treatment`;
  - plain `medium` / `normal growth medium` (but not conditioned / serum-free / high-glucose);
  - `saline`, `H2O`;
  - `no inhibitor` / `without drug`.
- **Trailing timepoints** are stripped from compound names (`dbcAMP_24h` -> `dbcAMP`), so one agent at several
  times counts once. `psi_deploy._time_tokens` now reads `_24h` (it treats `_` as a separator), so the timepoints
  still become separate conditions.
- **Corrected my own earlier change:** 'Not Drug Treated' with `is_control=no` is excluded from the baseline ONLY when
  it matches `_PERTURBATION_RE` / `_SIRNA_TOKEN_RE` (siRNA/CRISPR/KD/KO/OE/transfection/virus/irradiation/hypoxia/
  sorting…). Other AI-`is_drug=False` values are mostly control spellings the deterministic test doesn't know; they
  are controls again, as before.

**Measured on the real U-87 MG metadata (9 studies):**

| | Before | After |
|---|---|---|
| Studies with a same-study comparison | 5 | 7 |
| Same-study comparisons | — | 203 |
| Cross-study comparisons | 14 | 0 |

- The two dbcAMP time courses now use their own 0 h arms.
- The 2 remaining studies have no treatment metadata at all; they stay Undetermined unless the AI title fallback
  classifies them.

**Using it on an existing run:**
- Copy `study_coverage.py` to the cluster and run it against the run folder.
- If it reports "AltAnalyze WROTE NONE" for most studies, redeploy the PSI stage (new `lib_psi.sh` +
  `run_psi_job.sh` + `watchdog.sh`), remove `psi/PIPELINE_COMPLETE.txt`, and re-arm the PSI watchdog. Then do the
  same for concordance: remove `concordance/PIPELINE_COMPLETE.txt` + `results/*/concordance.txt`.

### 9c. Read-only check of the REAL cluster runs (same day) — where the A549 studies actually go

Read through the OnDemand shell, with nothing modified: `/data/salomonis-archive/LabFiles/SpliceScout_Test/<run>/`.

| Run | Selected studies | Drug group in PSI plan | dPSI written | Gathered | Quarantined (cross-study) |
|---|---|---|---|---|---|
| A549 | 865 | 161 | 135 | 120 | 38 |
| K562 | 1320 | 81 | 60 | 0 | 0 |
| Live_Demo_h358 | 2 | 2 | 2 | 2 | 0 |
| MDSL | 6 | 2 | 2 | 2 | 0 |
| sra3_hepg2 | 31 | 25 | 24 | 25 | 2 |

- **The §9b `psi_done` bug did NOT hit A549.** All 301 comparisons were written, 263 signatures from 120 studies were
  gathered, and the old summary names 58 studies.
  - The "2 studies" summaries are Live_Demo_h358 (only GSE315052 + GSE324125 were ever selected, both doxorubicin)
    and MDSL (6 selected, 2 with a drug group).
- **The dominant loss is PC-side: studies with NO drug call.** The annotated A549 run table
  (`star/SraRunTable_A549.csv.gz`) has 13,906 runs in 766 studies. Only 209 studies have any 'Drug Treated' run.
  - **289 have no value in any treatment column.**
    - Clearly non-drug: 126 genetic, 14 infection, 14 genetic+infection, 6 physical, 10 cytokine/other.
    - **71 have no varying metadata column at all**, so their design is only in the GSM title.
    - 31 vary in columns with no recognizable signal.
    - 17 show drug-like text in some other column (e.g. GSE124636 dexamethasone/TNF in `treatment_2`).
  - **268 have treatment text but nothing called a drug.**
    - Mostly real non-drug studies: 84 genetic, 32 infection, 19 genetic+infection, 10 physical, 7 cytokine, plus
      a few mixed.
    - 56 with unrecognized text.
    - **44 with drug-like text.** Examples:
      - GSE185207 / GSE185209: `treated by 50 nM mitoxantrone for 48 hours` vs `treated by 1/1000 DMSO …`
      - GSE191255: `1 week 50nM CFI-400945 treated`
      - GSE118448: `Treated; 1.0uM PG` vs `Untreated; 0.05% DMSO`
      - GSE127001: `5mM MMA for 10 days` vs water
      - GSE122168: interferon-β vs vehicle

**Root cause (new): `normalize_compound` destroyed dose-first free text.**
- The simple-value rule `[_,\s]+<dose>.*$` removes EVERYTHING after a dose. That is right for `Erlotinib 10 uM, 24h`
  but turned `treated by 50 nM mitoxantrone for 48 hours` into `treated by`.
- The AI compound pass (`prep_ai` -> `ai_clean`) sees ONLY the cleaned key. It could not name a drug, so it
  answered not-a-drug, and both arms of the study became 'Not Drug Treated'.
- `1 week 50nM CFI-400945 treated` was cut to `1 week`, which `is_control` read as a time-only CONTROL.

**Fixes:**
- **`normalize_v2`:**
  - `_strip_framing` removes, before anything else: leading framing that has a connector or punctuation
    (`treated by|with`, `Treated;`, `treatment:`, `Exposure to`, `cells were treated with`, but not a bare
    `Treatment A`), a trailing `for <duration>`, and a leading duration (now including weeks / months). It never
    strips a value down to nothing.
  - The dose split keeps the text AFTER the dose when only framing precedes it (`_HEAD_FRAMED`), e.g.
    `A549 treated by 50 nM X`.
  - `10 uM of cisplatin` -> `cisplatin`.
  - A dilution (`1/1000 DMSO`, `1:1000`) is control residue.
  - `by|using|of` were added to `_FRAMING_WORDS`.
- **`normalize_v2.is_control` — corrected my own §9b change.** Framing words used to be stripped unconditionally, so
  a bare `treated` / `Treated` / `stimulated` / `Treated 24h` / `cells` became a CONTROL. The original code never
  did that.
  - Now framing words only complete a control when a real control word is present (`_CONTROL_ANCHOR`: DMSO,
    vehicle, ethanol, PBS, water, H2O, control, ctrl, medium, saline) or a lone negation remains (`Not treated`).
  - A value that is only units / time / dilution / replicate tags is still a control, as in the original.
- **`ai_clean._resume_plan` — content-aware resume.**
  - Batches are numbered (`cmpd_005.json`), not content-addressed. When PREP re-chunks a changed vocabulary, a stale
    result file used to count as done, and the new strings never reached the model.
  - Now a result counts only if it covers every string in its batch.
  - A batch whose strings were all answered before (in any earlier result file) is rebuilt from those answers with
    no model call. Only batches holding new strings are sent.
- **`structured_extract.is_compound_tag`** — numbered / generically qualified treatment tags (`treatment_2`,
  `Treatment_1`, `exposure`, `drug treatment`, `agent 2`). It is the single test for both the headline count and
  `runtable_annotate.treatment_columns`. It was tightened in §9d: an agent-named qualifier no longer matches.
- **`psi_deploy._write_study_funnel`:**
  - new `treatments` column: the depositor's own treatment strings, up to 6;
  - a console NOTE naming studies that have no drug call although a treatment string carries a molar / mass dose
    and is neither a control nor a genetic / infection / physical perturbation (`_dosed_treatment`).

**Verified:**
- `is_control`: 107 cases (the 90 from §9b plus 17 new), 0 mismatches. `clean_compound`: 33 cases, 0 mismatches.
- **Real U-87 metadata:** of 207 distinct treatment values, only one cleaned key changes (`Exposure to rutaecarpin
  for 48 hours` -> `rutaecarpin`), and no control call changes. The end-to-end funnel is unchanged: 195/197
  compounds, 7/9 studies, 0 cross-study.
- **Replay of 8 real A549 studies** (their exact treatment strings; the AI verdicts the cluster labels imply):
  - old code: 0/8 studies with a same-study comparison, matching the cluster;
  - new code: 7/8 (mitoxantrone x2, PG, MMA, CFI-400945, interferon-β, dexamethasone/TNF);
  - the siRNA study (GSE103016) stays out.
- **Content-aware resume unit test:** after a re-chunk, only the batch holding `mitoxantrone` goes to the model. The
  old rule sent nothing.
- **NOT verified:** the new normalizer over all 766 A549 studies. The OnDemand session expired first. The re-run's
  `study_funnel.tsv` will show the real count.

**Still open (the next lever, not implemented):**
- **~100 A549 studies have their design ONLY in the GSM title.** The AI title fallback (`sample_map`) returns only
  Drug / Not / Undetermined and never the compound, so a title-only 'Drug Treated' run is labelled `<GSE>.treated`
  with every drug of the study pooled.
- **Fix:** have the samples AI pass also return the compound named in the title. That needs a prompt + schema change
  and a re-run of that pass.
- **Unresolved AI answers:** a compound the AI could not resolve (or a dropped batch) is filled with `is_drug:
  False` (`ai_clean._unknown_value`), which is indistinguishable from a real "not a drug". The new NOTE surfaces the
  dose-bearing ones.

- **`runtable_annotate.treatment_columns`** also skips the stage's own `drug` output once `drug_treated` is present.
  Re-annotating an annotated table used to read its previous canonical answer back in as a treatment. Verified: a
  second `run()` gives identical labels.

**Applying it to an existing (finished) run:**
- **`--start-stage` does NOT do this.** `begin()` runs a stage only if it is in range AND not already `done`, and
  `--start-stage` only marks EARLIER stages done.
- **PC:** delete these keys from `<run>/pipeline_state.json`, then `python pipeline.py --run-dir <run> --resume`:
  - `prep`, `ai_compounds`, `merge`, `build`, `runtable_annotate`
  - `psi_bundle`, `psi_submit`, `concordance_bundle`, `concordance_submit`
- **Keep the other stages done:**
  - `select`: an auto re-pick could switch the cell line;
  - download / STAR / BED: the download bundle holds every run of every selected study (`by_study`), whatever its
    drug label.
- **PREP** re-chunks from the existing `samples.jsonl`, and `_resume_plan` sends only the batches holding new keys.
- **Cluster:**
  - rename `psi/PIPELINE_COMPLETE.txt` + `concordance/PIPELINE_COMPLETE.txt`;
  - move `concordance/results` aside.
  - Otherwise both launchers exit at "already finalized".
- Re-running `extract` would NOT re-parse finished studies (done-set). New tags like `treatment_2` still reach the
  run-table annotation, which reads the run-table columns directly.
- **Not yet exercised end-to-end on the cluster.**

### 9d. The A549 PC run folder was lost -> rebuilt from the cluster, plus the fixes that surfaced

**Rebuild.** One-off scripts, kept outside the repo in the session scratchpad: `a549_rebuild/rebuild.py` and
`build_bundles.py`. They produce `runs/A549_reannotated/` from:
- the cluster's annotated run table, `A549/star/SraRunTable_A549.csv.gz`;
- the three `config.sh` files.

**Findings on the real table:**
- **Rows are (Run, GSE) pairs.** SuperSeries list the same runs again: 13,906 rows, 10,556 runs, 8,294 BioSamples.
  Key old labels by `(Run, GSE_Series)`.
- **Recovering the lost AI verdicts** (`compound_map.json` was gone). They are rebuilt from the old annotation:
  - a POSITIVE old verdict (the AI named drug X) carries over even when the fix changed the cleaned key;
  - a NEGATIVE one carries over only when the key is unchanged (it judged a mangled string such as `treated by`).
- **Names no AI ever judged:** 49. 15 matched the genetic/infection pattern. The other 34 were hand-classified with
  the pipeline's own AI rule (`MANUAL_VERDICTS` in `rebuild.py`; listed in `runtable/review_new_names.tsv`).
- **Title-labelled runs.** Runs the new code leaves Undetermined keep their old label, which came from the AI title
  fallback (whose `sample_map` is gone too).

**Result (same grouping code throughout):**

| | Old plan on the cluster | New plan |
|---|---|---|
| Studies with a same-study comparison | 134 | 149 |
| Same-study comparisons | 428 | 497 |
| Cross-study comparisons | 69 | 27 |
| Compounds with a same-study comparison | — | 247 |

**More fixes that the real table forced:**
- **`is_compound_tag` tightened.** A qualifier before `treatment`/`agent`/… must be GENERIC (drug, chemical,
  secondary…). Agent-named tags hold values that are not agent names: `tgf-beta_treatment: yes/no`, `dox_treatment`,
  `ifn_treatment`, and `infectious_agent` holds virus strains. Added exact tags `treated`, `drugs treated`,
  `treatment description` (real drug arms in A549).
- **`normalize_v2.clean_compound`:** a value that names no agent (`yes`, `+`, `true`, `treated`, `drug`, …) -> None,
  i.e. Undetermined. It used to become a "drug" called `Yes`.
- **`psi_deploy._build_default_groups` — background perturbations.** A perturbation that the study's DRUG arms carry
  too (GSE162316/8: miR-mimic transfection ± IFN-α2) is the study's background. Its untreated arms stay controls
  (`bg_pert`); the §9b exclusion had left the study with no baseline.
- **`psi_template/run_psi_job.sh` — gz-aware, grouped-only bedDir:**
  - The PSI stage's own `COMPRESS_WHEN_DONE` gzips the whole project tree (`COMPRESS_DIR` defaults to the parent of
    PIPELINE_ROOT), BEDs included. So a PSI re-run found no `*__junction.bed`: A549 has 6,270 of 6,363 as `.bed.gz`,
    and the filtered set's `__intronJunction.bed` symlinks point at originals that became `.bed.gz`.
  - A gzipped BED, or the `.gz` of a dangling link's target, is now decompressed into a private copy in
    `$PIPELINE_ROOT/junction_beds`. The original is never touched, and copies are reused.
  - `PSI_GROUPED_BEDS_ONLY=1` (new, default) links only samples in `sample_groups.tsv`.
  - `build_groups.sh` now ALWAYS reruns on the curated bedDir. `run_psi_pipeline.sh` builds groups against the raw
    dir first, where gzipped BEDs are invisible.
  - Tested in Git Bash on a fake tree, with symlinks emulated via `MSYS=winsymlinks:lnk`: plain links, gz copies,
    dangling-link -> target.gz, samples outside the plan skipped, stale copies dropped, idempotent, originals intact.

**Cluster facts (read-only, via OnDemand Files):**
- `STAR_beds_filtered` is the manuscript's τ=1% prevalence filter (`psi_fix/`: `whitelist_ge63`, ~6,316
  libraries). The new PSI therefore runs with `JUNCTION_PREVALENCE_TAU=0`.
- Only ~70–75% of planned samples have a junction BED (2,043 of 2,904 in the 149 studies). The download stage ended
  STALLED (`dropped_accessions.txt`).
- The old PSI grouped 1,906 of 5,538 planned samples. Only 250 were BED-quarantined; the rest are mostly
  missing-BED group attrition.
- The registry's `lung` atlas points LUSC at `ONCObrowser/Temporary/LUSC/...`, which is EMPTY. The old run used
  `TCGA_AML/Supervised_Analysis/{LUAD,LUSC}/DSE/Events-dPSI_0.1_adjp`, and the rebuilt bundle keeps that atlas.

**Launch:** `runs/A549_reannotated/runtable/LAUNCH_ON_CLUSTER.txt`. It targets a NEW folder,
`SpliceScout_Test/A549_reannotated`, with job tags `A549r_*`, and leaves the old A549 folder untouched. Not launched
yet: it needs the user's cluster login.

### 9e. Nathan Salomonis's review of the A549 concordance (same evening)

**Wrong atlas.** "Only a few subtypes reversed by many drugs, the majority missing." The old A549 concordance scored
against `/data/salomonis2/NCI-R01/TCGA_AML/Supervised_Analysis/{LUAD,LUSC}/DSE/Events-dPSI_0.1_adjp`. Despite the
folder names, these hold AML OncoSplice subtypes (`PSI.NS7(R2-C17)_vs_Others…`, `U2AF1-Stringent…`).

The correct lung signatures, per Nathan (cluster paths; `/Volumes/salomonis2` on a Mac):
- **LUAD:** `ONCObrowser/Temporary/LUAD/DE_splicing_events/Events-dPSI_0.1_adjp`. 84 signatures, e.g.
  `PSI.LUAD_BA_R3-V29`; counts in `Temporary/LUAD/Oncosplice_clusters/MergedResult.txt`.
- **LUSC:** `ONCObrowser/Testing/LUSC/DE_splicing_events/Events-dPSI_0.1_adjp`. 18 signatures, e.g.
  `PSI.R1-V27_vs_Others`; counts in `Testing/LUSC/Oncosplice_clusters/MergedResult.txt`.
- **Registry change:** `cancer_atlas_registry.json` `lung` LUSC moved from `ONCObrowser/Completed/LUSC` to `Testing/LUSC`.
  `Completed/LUSC` also exists (36 signatures, `PSI.LUSC_BT_R3-V9` naming like LUAD's); ask Nathan if both are
  wanted.
- **More to come:** Nathan has two more signature sets to locate. Add them as extra `queries` in the `lung` atlas.

**Formats checked.** Both sets use AltAnalyze dPSI columns (`ClusterID`, `EventAnnotation`, `dPSI`, `rawp`, `adjp`),
which the scorer's non-`Feature` branch reads.

**TSV outputs ("formatting is weird").** `ranked_concordance_summary.txt` is an aligned text report. New outputs:
- `rank_concordance.py` writes `results/<atlas>/significant_pairs.tsv`: every significant reversal/mimic, no
  per-drug cap.
- `score_with_null.py --complete` writes `results/complete_drug_by_subtype.tsv`, wired into `run_concordance_job.sh`:
  - EVERY drug × EVERY subtype of every atlas, built from `concordance.txt`'s header (all subtypes) and rows (all
    drugs);
  - `pair_stats.tsv` only holds pairs with ≥ 1 shared event, so a subtype a drug never touched used to be invisible;
  - each row carries `result`: significant reversal/mimic, not significant, "below overlap floor (n < 25): not
    tested", or "no shared events".
  - This answers "provide a full result for a few drugs to make sure it is reporting all mutations/subtypes".
- `_fmt` is now unicode-safe under the cluster's python 2.7 (py2 `str()` raised on 'β' / 'µM' names).
- Tested on synthetic scorer output: 2 drugs × (3 LUAD + 1 LUSC) = 8 rows, with each result kind present.

**Quick re-score (no PSI re-run).** Script: `a549_rebuild/build_lung_concordance.py` in the session scratchpad.
- Output: `runs/A549_lung_concordance/runtable/concordance_bundle.zip` plus `LAUNCH_ON_CLUSTER.txt`.
- Concordance ONLY, on the EXISTING A549 PSI results: `PSI_ROOT=A549/psi`, read-only. The gather step gunzips the
  127 compressed dPSI files into its own folder.
- Scored against the lung atlas into a NEW folder, `SpliceScout_Test/A549_lung_concordance` (tag `A549L`), in about
  1–2 h.
- The re-annotated bundle (`runs/A549_reannotated`) now also uses the registry lung atlas.

### 9f. Lung re-score results + a p-value precision fix (2026-09-19)

**The quick re-score ran.** The user launched it, and it was COMPLETE at 2026-09-18 22:03 after about 7 minutes.
There were no errors.
- 263 drug contrasts, gathered from 120 studies.
- 26,826 pairs in `complete_drug_by_subtype.tsv` (263 × 102):
  - 13,043 tested (n ≥ 25);
  - 12,168 below the floor;
  - 1,615 with no shared event.
- 4,494 significant at FDR < 0.05: 1,833 reversals and 2,661 mimics.
- **Nathan's concern is answered: reversals are spread across subtypes.**
  - LUAD: 73 of 84 subtypes have at least one significant reversal.
  - LUSC: 18 of 18.
  - Per-subtype reversal count: median 15 (IQR 7–25).
- **Deliverables:** `runs/A549_lung_concordance/results_for_nathan/`:
  - `README.txt`;
  - `A549_lung_example_4drugs_all_subtypes.tsv`: trametinib, indisulam, cisplatin and metformin × all 102 subtypes;
  - `A549_lung_subtype_summary.tsv`.
  - Both TSVs were built in the page from the cluster table and copied via the clipboard, with checksums verified.

**p-value floor, fixed.** `score_with_null.binom_two_sided_p` returned `1 - central_mass`, which cannot go below
about 1e-16. The strongest pairs therefore printed `p = 0` / `q = 0`, and the relative precision of p < 1e-8 was
poor.
- It now finds the central interval and sums the two TAILS directly, in log space. Minimum 5e-324.
- Checked against exact rational arithmetic on 600 random (k, n, p) cases: worst relative error 2.6e-13. For
  example, k=10, n=121 gives 1.04e-22 (it was 0).
- Values above ~1e-16 are unchanged (to 1e-12), so the manuscript Table 4 reproduction still holds.
- The ranker uses the same function.
- The A549 lung files on the cluster still have the old zeros. The example TSV carries a `p_value_precise` column,
  and no significance call changes.

### 9g. K562 (2026-09-22): the concordance stage never ran, and the run is thin upstream

**State on the cluster** (`SpliceScout_Test/K562`, read-only inspection):
- `concordance/` is EMPTY: the stage was never deployed for this line. That is why the earlier survey found 0
  signatures gathered (vs 120 for A549).
- `psi/` COMPLETE 2026-08-19, 853 grouped samples, 98 dPSI files written (66 gzipped by the compress step).
- The PSI plan itself was small: `sample_groups.tsv` = 1,997 samples / 337 groups / 235 studies, but
  `sample_comps.tsv` = **99 comparisons** (85 same-study across 53 studies, 14 cross-study). Most groups are a
  study's control arm with no drug arm, and some drug arms carry the generic `<GSE>.treated` label (the title-only
  case in §9d).
- **The download stage STALLED** (root `PIPELINE_STALLED.txt`, 2026-07-10) with `dropped_accessions.txt` =
  **5,851 runs** that never downloaded or converted. 1,997 planned samples -> 853 with usable BEDs.
- `STAR_beds` 7,450 entries, `STAR_beds_filtered` 4,045, `psi/quarantined_beds` 189.
- **No run table on the cluster.** A549 has `star/SraRunTable_A549.csv.gz`; K562's `star/` has none, so the §9d
  rebuild (re-annotate from the cluster's own table) is NOT possible for K562 as things stand.

**Registry change.** `cancer_atlas_registry.json` `k562` used to hold only the ENCODE RBP signatures. K562 is a
leukemia line, so it now carries both:
- `AML`: `/data/salomonis2/NCI-R01/AML-OncoSplice`, 61 Leucegene OncoSplice subtypes, the disease reference
  (counts via the bundle's `aml_subtype_counts.tsv`);
- `K562-ENCODE`: 232 RBP-knockdown signatures in K562 itself, i.e. which RBP a drug's splicing resembles.
Per-atlas summaries use per-atlas BH; `scored_pairs_with_null.tsv` applies BH across both, and the `atlas` column
separates them.

**Bundle built** (`scratchpad/build_k562_concordance.py` -> `K562_concordance/runtable/concordance_bundle.zip`):
reads `K562/psi`, writes into the empty `K562/concordance`, `MIN_OVERLAP=25`, `STUDY_MATCHED_ONLY=1`, alert email
restored. Not launched: it needs the user to upload and start it.

**Still wrong upstream for K562** (scoring cannot fix these):
- 5,851 dropped accessions -> re-download (`fetch_missing.sh` exists in the run folder) before any re-run;
- the §9c/§9d annotation gap, which needs the PC-side run folder or a rebuilt run table.

**Controlled Folder Access is blocking again:** python/bash writes under `Documents` fail (WinError 2 / permission
denied), while the app's own Edit/Write still work. Build generated artifacts in the scratchpad.

**K562 concordance result** (launched by the user; COMPLETE 2026-09-22 18:09):
- Gathered 97 signatures, quarantined 13 cross-study, left **84 study-matched** from 52 studies.
- 24,612 pairs (84 × 293 references), 20,577 tested; 7,069 significant at FDR < 0.05 (2,807 reversals, 4,262 mimics).
- **AML (61 subtypes):** 32 subtypes have a significant reversal (median 2 drugs per subtype, max 28 for NS10).
  - Of the 29 subtypes without one, 23 are small mutation-variant signatures that share < 25 events with EVERY
    drug (ASXL1, DNMT3A, FLT3, KRAS, TET2, ...). That is a property of those references, not missing data.
  - The splicing clusters (NS10, KAT6B-correlated, U2AF1-Stringent, TP53 R2-C3) carry most reversals.
- **K562-ENCODE (232 RBP knockdowns):** 229 have a reversal (median 9 drugs per RBP).
- **Studies:** 51 scored; 42 have at least one significant AML reversal. The "only 2 labs" complaint is resolved.
- **Content flag:** some annotated "drugs" are reagents (4-thiouridine labelling, doxycycline induction, the dTAG-7
  degradation tag) and should be excluded or labelled.

### 9h. 2026-09-22 (evening): "rerun everything" -- download-stage root causes, K562 run table rebuilt, re-run kits

**Why K562 lost 5,851 runs** (read-only diagnosis of `SpliceScout_Test/K562`):
- **4,081 were downloaded but never converted.** prefetch writes `<study>/<acc>/<acc>.sra`; `convert_study.sh`
  flattens it, but it bails out on a full queue. The old watchdog / `fetch_missing.sh` only looked for the flat name,
  so they counted the run as a failed download, re-prefetched it (prefetch: "found locally") and DROPPED it after
  MAX_FAILS. The PSI stage's compress pass then gzipped the nested copies to `.sra.gz`. GSE174695 -- a 1,263-run
  K562 drug screen (~36 compounds x ~60 runs) -- was entirely in this state.
- **GSE127062 (1,522 runs):** one `timeout 7200 prefetch --option-file` for the whole list; every attempt was killed
  after ~76 runs (exit 124, 7,203 s).
- **A full queue burned attempts:** a blocked bsub (124) still bumped the attempt counter (conversions and
  re-fetches), so runs were dropped untried. A pass whose submits all blocked also looked idle -> false STALL.
- None of the dropped runs were in the old K562 plan.

**Template fixes** (scratchpad working copy; `bash -n` clean; mock-tested with stub bsub/bjobs):
- `cluster_template/watchdog.sh`: flattens nested downloads (a nested duplicate of an existing flat copy is removed,
  else it holds the "zero .sra" gate open); an attempt counts only after bsub took the job; a blocked submit is
  pending work, not a stall (the pass/wall-clock backstops still bound it).
- `fetch_missing.sh`: per-accession flatten; ANCHORED FASTQ test (`SRR123` no longer hides behind `SRR1234`);
  attempts counted once the re-fetch is queued; its last line reports "submit blocked".
- `prefetch_job.sh`: the timeout bounds EACH accession.
- `psi_template/compress_done.sh`: skips `.sra/.sralite/.vdbcache` and the tooling / stage inputs
  (`*/altanalyze/*`, `*/altanalyze_home/*`, `*/STAR-index/*`, `SraRunTable*`, `SraAccList*`, sample / BAM / group
  lists). The earlier pass had gzipped the BED exon reference (both lines) and A549's STAR run table.
- `star_template/build_sample_list.sh`, `bed_template/build_bam_list.sh`: a targeted re-run (`RERUN_TARGETED.txt`)
  may yield 0 rows; the BED list names only BAMs that still lack BEDs (complete plain BED or `.bed.gz` = done).
- `normalize_v2.py` `flatten_structured_treatment`: ENCODE records (`treatment_term_name: X; ... duration: N;
  duration_units: U`) read as `X N U` -- in normalize / is_control / clean_compound, `runtable_annotate.pick_treatment`
  and `psi_deploy._raw_treatment` (GSE127062: 48 "compounds" -> 11 drugs x 4 timepoints + DMSO controls).
- `runtable_annotate._negated_agent_controls`: a run negating one of ITS OWN study's drug arms (`no IFNγ`,
  `without Lip-1 treatment`) is that study's control; `no serum` / `no dox` are untouched.

**K562 run table rebuilt from NCBI** (`scratchpad/k562_rebuild/`): the 352 `by_study` GSEs (their SraAccList lists =
8,421 runs, transferred checksum-verified) through `runtable_fetch` + `runtable_build` (28,980 runs, 0 failures),
filtered to K562's runs. K562's STAR ran without a run table (one BAM per RUN), so the plan keys samples by run
(`BioSample := Run`, the depositor value kept in `BioSample_orig`). Verdicts: 225 carried from the old plan labels,
38 hand-classified, 50 non-drug by pattern, 53 real drug names left to the default. Old-plan calls carried onto runs
the new code leaves Undetermined, but never onto reagent / genetic runs (121 refused).
- K562: 2,132 Drug Treated / 2,408 Not / 3,881 Undetermined -> plan 3,959 samples, 431 groups, **217 comparisons
  (207 same-study)** vs 99 before.
- A549 (§9d rebuild re-run with this code, same guard on its carry-over: 144 refused): plan 4,563 samples,
  524 comparisons (490 same-study).

**Targeted re-run kits** (`scratchpad/rerun_build/`: `build_rerun_bundles.py`, `kit/`):
`SpliceScout_rerun_<LINE>.zip` -> unzip into `<run root>/rerun/`, `DRY_RUN=1 bash rerun/rerun_submit.sh`, then again
without DRY_RUN. `rerun_submit.sh`:
- classifies every plan sample from the live files: BED-ready / BAM awaiting BED / needs delivery (FASTQ on disk,
  .sra, nested, or download);
- un-drops only the plan's runs and marks every OTHER unconverted run `not-needed` -- STAR deleted the FASTQs of
  aligned runs, so a re-armed watchdog would otherwise re-download all of them; drop markers no list names move to
  the backup (they inflate done+dropped);
- resets STAR / BED (markers, lists, `.attempts` -- stale drop markers can finalize a stage while resubmitted jobs
  are still pending -- watchdog state), sets `CLEANUP_TOOLS_WHEN_DONE=0`, restores gzipped tooling, installs the fixed
  scripts and the STAR -> BED hand-off launcher (K562's BED scripts live in `K562/bed`);
- submits the unpack array `<tag>_recover[1-N]%40`, the download watchdog on `ended(<tag>_recover)`, and the NEW PSI
  (`K562/psi_v2`, `A549_reannotated/psi`: tau=1% over grouped samples, compression off) and concordance
  (`K562/concordance_v3`, `A549_reannotated/concordance`: EXCLUDE_REAGENTS=1) launchers, 8-week windows.
- K562 estimate: 2,348 of 3,959 plan samples need delivery (1,262 of them GSE174695, all nested).

**Still valid, quick:** the concordance-only re-scores `K562_v2` (-> `K562/concordance_v2`) and `A549_lung_v2`
(-> `A549_lung_concordance_v2`) over the OLD PSI (reagent filter + exact p) -- results in hours, not weeks.

**Controlled Folder Access now blocks the Edit tool as well.** Every change above lives in the scratchpad working
copy `SpliceScout_work`; copy it back once the apps are allowed.

**Later the same evening (Controlled Folder Access switched off by the user):**
- The 10 changed files were copied into the repo (hash-verified; originals in `SpliceScout.backup-20260922`), and
  the build scripts now run the repo code (`SPLICESCOUT_CODE` overrides).
- **Lung atlas, 2 more reference sets** (Nathan: "there are two more"; he said to look ourselves). Searched
  `NCI-R01` read-only. `TCGA-Lung/Korean_and_TCGA-Lung/TCGA-{LUAD,LUSC}/Oncosplice/Tumor_vs_Healthy/PSI/Events-dPSI_0.1_adjp`
  (2024-05) are the only other lung sets in the same OncoSplice PSI format with content: one signature each,
  `PSI.Tumor_vs_Healthy.txt`, 1,208 / 1,853 junction clusters, dPSI = tumor - healthy. The sibling
  `PriorKnownSubtypes` files are header-only; `CCLE-LUSC_LUAD` is hg19 (event IDs would not match hg38).
  Added to `cancer_atlas_registry.json` `lung` as `LUAD_Tumor_vs_Healthy` / `LUSC_Tumor_vs_Healthy`
  (`counts_kind: none`; the scorer reads only `PSI.*` files, so the folder's `event_summary.txt` is ignored).
  **Pending Nathan's confirmation.** The A549 quick re-score and the A549 re-run kit were rebuilt with the 4 lung
  queries.
- Provenance saved in `runs/_rerun_2026-09-22/` (README there): the cluster lists, the K562 NCBI rebuild and
  re-annotation, the A549 rebuild, the kit builder and kits, the quick re-score bundles.
  `runs/A549_reannotated` (09-18) is superseded (`SUPERSEDED.txt`).

**Results go back into the ORIGINAL folders (the user, 2026-09-22: "put this new data in the old folders"):**
- `SpliceScout_Test/launch_rerun2.sh swap` moves the finished quick re-scores into `K562/concordance` and
  `A549_lung_concordance`. The previous contents are kept as `<folder>.old_<date>` (never deleted), and the
  top-level configs and reports are re-pointed at the new path.
- The re-run kits (`SpliceScout_rerun2_<LINE>.zip`) arm `promote_launch.sh`. It polls hourly (8-week limit) and,
  once the new concordance is COMPLETE with no stage jobs live, moves the staging PSI and concordance
  (`K562/psi_v2`, `K562/concordance_v3`; `A549_reannotated/psi`, `A549_reannotated/concordance`) into
  `K562/psi` + `K562/concordance` and `A549/psi` + `A549_lung_concordance`, the same way, and writes
  `PROMOTED.txt`. It never promotes a STALLED concordance. `FINAL_PSI_DIR` / `FINAL_CONC_DIR` in `rerun.env`.
- `live` now always re-unpacks the uploaded kit (a dry run may have unpacked an older one).
- Both paths were mock-tested: swap, idempotent re-run, unfinished guard, promote before and after completion.

### 9i. 2026-09-25 -- the re-run workarounds made permanent, and targeted re-runs built in

The 2026-09-22 re-run kit (9h) worked around several template problems by hand. Each one is now fixed in the
templates or deploy code, so a normal run -- or a later re-run -- no longer hits it:

| Problem (where it bit) | Fix |
|---|---|
| **Stale drop markers finalized a stage early.** The download / STAR / BED completion gate is `done + dropped >= expected`, and `*_dropped_count` counted EVERY `.attempts/*.dropped` -- also markers of runs/samples no list names any more (a list rebuilt for a re-run) and of ones delivered after all. | `sra_dropped_count` (lib.sh), `star_dropped_count` (lib_star.sh), `bed_dropped_count` (lib_bed.sh) count only items of the CURRENT list (SraAccList.txt / sample_list.tsv / bam_list.tsv) that are not done. Mock test: 1 done + 3 stale markers of a 3-sample list used to finalize COMPLETE; now it resubmits the third sample. STAR `submit_all.sh` also skips dropped samples, as the watchdog does. |
| **A re-armed watchdog hit the backstop on its first pass.** `.watchdog.state.firstpass` survived a finalize, so re-arming a stage days later (re-run, manual or AI re-arm) exceeded `MAX_WALL_HOURS` at once. | All 5 watchdogs: a gap of `BACKSTOP_RESET_GAP_HOURS` (new config key, 12) since the last pass (`.watchdog.state.lastpass`; for older state, the age of `.passes`) starts a fresh pass/wall-clock window, and `finalize()` clears the window. |
| **Re-arming the download after STAR re-downloaded every aligned run** (STAR deletes the FASTQs; the watchdog saw them as missing). | `run_star_job.sh` writes `<study>/<run>.aligned` (sample, BAM path, time) BEFORE it deletes a FASTQ. `sra_delivered` (lib.sh) = FASTQ OR `.aligned`; used by `sra_done_count`, `sra_dropped_count`, `fetch_missing.sh`, the STALLED report, and the watchdog (a stray `.sra` of an aligned run is removed). compress_done never touches them (size filter). |
| **The PSI plan was keyed by BioSample even when STAR labelled BAMs by Run** (no run table in the STAR bundle -> one BAM per run, as in K562): no group matched a single BED. | `psi_deploy._star_sample_key(P)`: an empty `RUNTABLE` in the run's `star/config.sh` -> key by Run (`_sample_col`); override with psi_cfg `SAMPLE_KEY` = auto / biosample / run. Threaded through `_build_default_groups`, `_write_study_funnel`, `_write_sample_groups` (Phase B too). |
| **Launchers timed out while the chain was busy.** Each launcher gave up after MAX_WAIT_HOURS once the ADJACENT stage's `watchdog.log` was stale -- a stage that had not started yet (or the stale log of an earlier run) looked dead while the download two stages up was working; BED waited only 168 h; a re-armed launcher kept its old `.launch_first_seen`. | `cluster_deploy.launch_wait_sh()` is the ONE bounded-wait block of star/bed/psi/concordance_launch.sh: alive = the freshest of ALL upstream watchdog logs + the `.launch_heartbeat` each upstream launcher touches every pass (transitive); a heartbeat older than 12 h renews the first-seen stamp. BED default wait 336 h like the others. |
| **prefetch refused runs > 20 GB** (its own `--max-size` default), so every deep run failed MAX_FAILS times and was dropped. | `prefetch --max-size "$PREFETCH_MAX_SIZE"` (new download config key, `500G`). |
| **STAR/BED/PSI/concordance watchdog re-schedules had no timeout** -- a `bsub` blocked at the pending-job cap hung the pass (the download stage already had one). | `timeout "${WATCHDOG_SUBMIT_TIMEOUT:-120}"` on those submits and on the STAR nudge. |
| **Odd `<run>_3` / `<run>_4` "samples".** `fasterq-dump --split-files` writes one file per read, so runs with index/barcode reads got `_3`, `_4`, which `make_sample_list.py` took as separate single-end samples. | `_N.fastq.gz` with N >= 3 are EXTRA reads of the run: left out of the list, reported in `<list>.extra`. |

**Targeted re-runs are now a built-in tool: `rerun_deploy.py` + `rerun_template/`** (generalized from the 09-22
`build_rerun_bundles.py` / `build_rescore.py` / kit, which stay in `runs/_rerun_2026-09-22/` as provenance):

    python rerun_deploy.py --run-dir runs/<run> --cluster-root /data/.../SpliceScout_Test/<LINE> [--mode full|rescore]
    # on Windows Git Bash, /data/... arguments are MSYS-mangled; rerun_deploy undoes it (as pipeline.py does)

- `full` (default): builds the run's CURRENT PSI plan (like the psi_bundle stage) and concordance bundle into
  `<run>/runtable/rerun/build_full/` -- never over the run's own psi/ + concordance/ bundles -- retargets them to NEW
  cluster folders (`<root>/psi_rerun_<date>`, `<root>/concordance_rerun_<date>`), writes `rerun_plan_runs.tsv`
  (label / run / study, labels keyed like the plan) and `rerun.env`, and zips the kit
  `<run>/runtable/rerun/SpliceScout_full_<LINE>.zip`: `rerun_submit.sh`, `recover_nested.sh`, `promote_launch.sh`,
  the current download / STAR-list / BED-list scripts, the STAR -> BED hand-off launcher, both bundles.
- `rescore`: concordance only, over the existing finished PSI (`<root>/psi`), into `<root>/concordance_rescore_<date>`;
  kit = `rescore_submit.sh` + `promote_launch.sh` + the bundle.
- Promotion is ON by default: when the new concordance is COMPLETE, `promote_launch.sh` moves the new results into
  the original folders (`--final-psi` / `--final-conc`, default `<root>/psi` + `<root>/concordance`); the previous
  contents are kept as `<folder>.old_<date>`. `--no-promote` leaves them in the new folders. A re-score promotes
  only the concordance.
- PSI / concordance settings come from the run's `config.json` (`psi_cfg`, `concordance_cfg`), then the re-run
  defaults (`PSI_GROUPED_BEDS_ONLY=1`, `COMPRESS_WHEN_DONE=off`, `EXCLUDE_REAGENTS=1`), then `--psi-set` /
  `--conc-set KEY=VALUE`. Other options: `--atlas`, `--sample-key`, job tags, `--bed-scripts` (older deployments
  keep BED scripts in `<root>/bed`), `--alert-email`, `--lsf-queue`, `--wait-hours` (1344 = 8 weeks).
- `rerun_submit.sh` (vs the 09-22 kit): also clears the `.aligned` marker of every run it re-delivers, treats
  aligned runs as delivered, installs `make_sample_list.py`, and moves stale BED `PIPELINE_LAUNCH_TIMEOUT.txt` /
  `.launch_first_seen` aside. It keeps the other state resets: cell lines deployed with OLDER scripts still carry
  that state.
- Checked against the A549 kit uploaded on 09-22: `rerun_deploy.py` with the same folders, tags and settings
  (`--psi-dir .../A549_reannotated/psi --conc-dir .../A549_reannotated/concordance --psi-set
  JUNCTION_PREVALENCE_TAU=0.01 --psi-set ALTANALYZE_HOME=...`) gives the identical plan (4,563 samples, 5,159 runs,
  524 comparisons; `rerun_plan_runs.tsv` identical below its header, `sample_groups.tsv` identical). Every other
  bundle file is identical too, except the ones this section changed (the new config key, the launcher wait block,
  the watchdogs). Without `--psi-set`, the tau comes from the run's config.json (`0` for A549). The A549 rescore kit
  reads the old PSI.

**Verified:** `bash -n` on all 66 stage/kit scripts; all 33 top-level modules import; the Python 3 template scripts
compile; the four generated launchers pass `bash -n`. Mock tests (stub bsub/bjobs/samtools):
- the download helpers: aligned / unlisted / delivered-after-drop cases;
- the STAR watchdog: a 20-day-old window resets instead of STALLING; stale drops no longer finalize early;
- `make_sample_list.py`: `_3` / `_4` go to `.extra`.
