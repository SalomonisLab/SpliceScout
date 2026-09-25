# -*- coding: utf-8 -*-
"""
Per-stage documentation shown when a step in the web UI's progress stepper is clicked.

STAGE_DOCS is keyed by the same stage keys as progress.STAGES. The server injects this into the
page as JSON; the browser opens a modal with the matching entry. Keep these in sync with the
pipeline stages (progress.py STAGES) and the actual behavior of each stage module.
"""

STAGE_DOCS = {
    "fetch": {
        "title": "Fetch GEO studies",
        "what": "Runs an NCBI Entrez esearch for your query against the GEO DataSets (gds) database, "
                "paginating through every matching study, then batched esummary calls pull each "
                "study's summary — accession, title, and its list of samples. Rate-limited to 3 "
                "requests/sec (10 with an NCBI API key).",
        "inputs": "Your GEO search query",
        "outputs": "ncbi_raw.json (all study summaries), unique_titles.json",
    },
    "extract": {
        "title": "Extract SRA metadata",
        "what": "For each study, links GEO→SRA (elink) and fetches the full SRA XML, parsing per "
                "sample the structured cell-line tag, treatment/compound tags, and total read count "
                "(spots). In the same pass it captures the study's library-prep protocol "
                "(construction protocol, strategy, selection, instrument). No AI and no "
                "keyword-guessing — only the depositor's structured metadata. Resumable per study; "
                "this is usually the longest stage.",
        "inputs": "ncbi_raw.json",
        "outputs": "structured_samples.jsonl (per-sample cell line / treatments / spots), study_protocol.json",
    },
    "prep": {
        "title": "Prepare AI batches",
        "what": "Builds the batched inputs for the two AI passes: a deduplicated list of cleaned "
                "compound strings, and a deduplicated set of cell descriptors (every distinct "
                "cell-line tag value, plus the titles of samples that lack a usable cell tag or "
                "treatment field). A sample_index maps each representative back to the raw values it "
                "stands for so results can be expanded later.",
        "inputs": "structured_samples.jsonl",
        "outputs": "ai_work/compound_batches/, ai_work/sample_batches/, sample_index.json",
    },
    "ai_compounds": {
        "title": "AI: canonicalize compounds",
        "what": "Sends each raw compound string to your chosen LLM (Claude / GPT / Gemini) to "
                "canonicalize it to a standard generic name — expanding abbreviations "
                "(5-FU → fluorouracil), stripping salts, converting trade → generic, sorting "
                "combinations — and flags non-drugs (controls, siRNA/CRISPR, bare numbers). Forced "
                "tool/function call, resumable per batch: on a resume only batches holding strings "
                "the model has not answered yet are sent. Skipped on a skip-AI run.",
        "inputs": "ai_work/compound_batches/",
        "outputs": "ai_work/compound_results/*.json  ({raw: {name, is_drug}})",
    },
    "ai_samples": {
        "title": "AI: classify samples",
        "what": "Sends each cell descriptor (a full sample title or a cell-line tag value) to the LLM "
                "to recover the canonical human cell line, bucket non-lines into a Sample Type "
                "(Cell line / Primary cells / Organoid / Patient-Tumor / iPSC-ESC / …), and classify "
                "the sample as Drug Treated, Not Drug Treated, or Undetermined. Skipped on a skip-AI run.",
        "inputs": "ai_work/sample_batches/",
        "outputs": "ai_work/sample_results/*.json  ({raw: {cell_line, category, drug_treated}})",
    },
    "merge": {
        "title": "Merge AI results",
        "what": "Combines all the per-batch AI result files into two lookup maps — compound "
                "canonicalization and sample classification — expanding the representative descriptors "
                "back to every raw value they covered via sample_index. Batch counts are discovered by "
                "glob, never hardcoded, so no data is silently dropped.",
        "inputs": "ai_work/*_results/, sample_index.json",
        "outputs": "compound_map.json, sample_map.json",
    },
    "build": {
        "title": "Build tables",
        "what": "Assembles the cell-line-grouped compound tables. For every sample it resolves the "
                "cell line + Sample Type (structured tag → AI → regex fallback), canonicalizes "
                "compounds (dropping non-drugs), counts reads, and classifies drug-treated three ways. "
                "It emits the all-protocols table, the TruSeq-only table, and the HEADLINE "
                "splicing-amenable table (keeps full-length protocols + Smart-seq; drops 3'-tag and "
                "single-cell), plus a per-cell-line index used by the deep dive.",
        "inputs": "structured_samples.jsonl, compound_map.json, sample_map.json, study_protocol.json",
        "outputs": "tables/ncbi_final_splicing.csv (headline), ncbi_final.csv, ncbi_final_truseq.csv, "
                   "ncbi_protocol_audit.csv, cellline_index.json",
    },
    "select": {
        "title": "Select best cell line",
        "what": "First merges cell-line NAME variants (A549 / A-549 / 'A549 cells' -> one line) so a "
                "line split across spellings isn't under-counted, then picks the single best REAL cell "
                "line to deep-dive (Sample Type 'Cell line' only — "
                "never UNRESOLVED / Patient / Organoid buckets), ranked by number of unique compounds, "
                "then total reads. Auto-mode takes rank 1; manual mode pauses the run so you can choose "
                "from the ranked list.",
        "inputs": "cellline_index.json",
        "outputs": "cellline_merge.json, runtable/cellline_selection.json",
    },
    "runtable_fetch": {
        "title": "Fetch run metadata (SRA XML)",
        "what": "For each of the selected cell line's studies, links GEO→SRA and downloads the full "
                "SRA EXPERIMENT_PACKAGE XML (with an ENA/EBI fallback for brokered studies), caching it "
                "locally. Resumable — already-cached studies are skipped.",
        "inputs": "cellline_selection.json",
        "outputs": "runtable/xml_cache/<GSE>.full.xml",
    },
    "runtable_build": {
        "title": "Reconstruct Run Selector table",
        "what": "Reconstructs the SRA Run Selector 'Metadata' table byte-for-byte from the cached XML "
                "— every run's accession, assay type, AvgSpotLen, bases, platform, library info, and "
                "sample attributes. Also collects the distinct cell-line-ish values seen across the "
                "studies as candidates for the disambiguation agent. The reconstruction is validated "
                "against the official NCBI export.",
        "inputs": "runtable/xml_cache/",
        "outputs": "runtable/SraRunTable_all.csv, match_candidates.json",
    },
    "cellline_match": {
        "title": "AI: match cell-line names",
        "what": "An AI agent decides which of the many cell-line spellings in the run table actually ARE "
                "the target line (A549 ≈ A-549 ≈ 'A549 cells', while excluding e.g. BEAS-2B). A run is "
                "kept if its GSM was already classified as the line OR its value is agent-blessed. On a "
                "skip-AI run, a deterministic normalized-equality match is used instead. Emits the "
                "download-ready SRR list.",
        "inputs": "match_candidates.json, SraRunTable_all.csv, cellline_selection.json",
        "outputs": "runtable/SraAccList.txt (MAIN), by_study/<GSE>/SraAccList.txt, SraRunTable_<line>.csv",
    },
    "runtable_annotate": {
        "title": "Annotate drugs + workbook",
        "what": "Annotates the filtered run table with canonical drug, dose, an is-control flag, and a "
                "3-way drug-treated label (reusing the general normalizer + compound map, plus a dose "
                "regex), then writes a filterable Excel workbook and a drug-annotation audit.",
        "inputs": "SraRunTable_<line>.csv, compound_map.json",
        "outputs": "annotated SraRunTable_<line>.csv, SraRunTable_<line>.xlsx, drug_annotation_review.csv",
    },
    "cluster_bundle": {
        "title": "Build cluster bundle",
        "what": "Assembles a ready-to-run LSF download bundle: the vendored cluster scripts, a config.sh "
                "filled from your settings (into a per-INSTANCE PIPELINE_ROOT subfolder named by the "
                "instance tag, so every stage + re-run of one instance shares a stable folder and runs "
                "never mix), and the PER-STUDY accession lists (each study "
                "downloaded independently). Zipped for download.",
        "inputs": "by_study/<GSE>/SraAccList.txt, your cluster settings",
        "outputs": "runtable/cluster/ + cluster_bundle.zip",
    },
    "cluster_submit": {
        "title": "Upload + launch on cluster",
        "what": "(Autonomous mode only) Uploads the bundle to your cluster over SSH and launches "
                "./run_pipeline.sh on the LSF submit host. If it fails, it reads the ssh/scp log, "
                "explains the cause, and lets you correct the SSH details and retry JUST the upload — "
                "no full rerun. Non-fatal: the bundle stays downloadable. Afterward, use 'Check cluster "
                "status' to see download/convert progress.",
        "inputs": "cluster_bundle.zip, your SSH settings",
        "outputs": "(remote) running LSF download/convert jobs; cluster_submit.json",
    },
    "star_bundle": {
        "title": "Build STAR bundle",
        "what": "(Bulk RNA-seq module) Assembles the STAR alignment bundle: the vendored STAR scripts, a "
                "config.sh pointed at the download's FASTQ output + the run's SraRunTable (so runs of one "
                "BioSample merge into one BAM), the organism (auto-detected from the run table), and a "
                "genome-index resolution (your GENOME_DIR → the organism registry → a build-once index "
                "job). JOB_TAG is the download tag + '_star' so the two stages never collide.",
        "inputs": "the download's by_study/ FASTQ tree, SraRunTable_<line>.csv, star_index_registry.json",
        "outputs": "runtable/star/ + star_bundle.zip",
    },
    "star_submit": {
        "title": "Launch STAR alignment",
        "what": "(Autonomous mode only) Uploads the STAR bundle and queues a launcher that WAITS for the "
                "SRA download to finish (LSF dependency on the download watchdog + a sentinel poll), then "
                "runs ./run_star_pipeline.sh — STAR 2-pass aligning every downloaded sample to BAMs + "
                "splice junctions. Consumes the FASTQs (never the .sra). Non-fatal: the bundle stays "
                "downloadable.",
        "inputs": "star_bundle.zip, your SSH settings, the running download",
        "outputs": "(remote) STAR BAMs + SJ.out.tab; star_submit.json",
    },
    "bed_bundle": {
        "title": "Build BED bundle",
        "what": "(Bulk RNA-seq module) Assembles the BAM->BED bundle: the VENDORED AltAnalyze toolkit "
                "(BAMtoJunctionBED.py + BAMtoExonBED.py + export.py/unique.py) and the exon reference, plus a "
                "config.sh pointed at STAR's BAM_OUT and the species (auto-mapped from the run's organism: "
                "Homo sapiens->Hs, Mus musculus->Mm, ...). ALL-IN-ONE: it ships the AltAnalyze slice it needs, "
                "so the cluster needs no AltAnalyze install (just stock python/2.7.5 + samtools). JOB_TAG is "
                "the STAR tag + '_bed'.",
        "inputs": "STAR's BAM_OUT (<download_root>/STAR_bams), the detected organism, the vendored altanalyze/ toolkit",
        "outputs": "runtable/bed/ + bed_bundle.zip",
    },
    "bed_submit": {
        "title": "Launch BAM->BED",
        "what": "(Autonomous mode only) Uploads the BED bundle to <BAM_OUT>/bed/ (the ~100 MB exon reference is "
                "uploaded once and size-skipped on re-runs) and queues a launcher that WAITS for STAR to finish "
                "(polls <BAM_OUT>/PIPELINE_COMPLETE.txt), then runs ./run_bed_pipeline.sh — converting each BAM "
                "to AltAnalyze <sample>__junction.bed + <sample>__exon.bed beside it. Non-fatal: the bundle "
                "stays downloadable.",
        "inputs": "bed_bundle.zip + the vendored exon ref, your SSH settings, the running/finished STAR alignment",
        "outputs": "(remote) <sample>__junction.bed + <sample>__exon.bed; bed_submit.json",
    },
    "psi_bundle": {
        "title": "Build AltAnalyze (PSI) bundle",
        "what": "(Bulk RNA-seq module) Assembles the AltAnalyze splicing bundle: the self-driving psi scripts, a "
                "config.sh pointed at the BED dir (STAR_beds) + the species, and sample_groups.tsv — a "
                "BioSample->group map for the differential comparison. By default the groups are the run table's "
                "treated-vs-control classification; with user-defined groups they come from a deterministic + AI "
                "assignment. AltAnalyze itself is NOT shipped (it is multi-GB with its database) — it is resolved "
                "on the cluster at submit time. JOB_TAG is the download tag + '_psi'.",
        "inputs": "the BED stage's BED_OUT_DIR (<download_root>/STAR_beds), the annotated SraRunTable_<line>.csv",
        "outputs": "runtable/psi/ + psi_bundle.zip (config.sh, scripts, sample_groups.tsv)",
    },
    "psi_submit": {
        "title": "Run AltAnalyze splicing",
        "what": "(Autonomous mode only) Resolves AltAnalyze on the cluster — uses an install found at "
                "ALTANALYZE_HOME (default the lab install) with its species database, or uploads a local copy only "
                "if none is found (an ALTANALYZE_DB path override is supported). Uploads the PSI bundle and queues a "
                "launcher that WAITS for BAM->BED to finish (polls <BAM_OUT>/bed/PIPELINE_COMPLETE.txt), then runs "
                "ONE AltAnalyze job over the whole BED dir -> a per-sample PSI table plus one dPSI comparison per "
                "drug condition vs its OWN study's controls (AltAnalyze cannot run groupless, so at least one "
                "study needs >= 2 recognized controls). For large cohorts a junction PREVALENCE filter first keeps "
                "only junctions seen in >= 1% of libraries (JUNCTION_PREVALENCE_TAU). Non-fatal: the bundle stays "
                "downloadable.",
        "inputs": "psi_bundle.zip, your SSH settings, AltAnalyze on the cluster, the running/finished BAM->BED stage",
        "outputs": "(remote) <psi_root>/output/AltResults PSI/dPSI tables; psi_submit.json",
    },
    "concordance_bundle": {
        "title": "Build concordance bundle",
        "what": "(Bulk RNA-seq module) Assembles the self-driving drug-concordance bundle: the vendored scorer "
                "(splicingConcordance_advanced.py), the ranker (rank_concordance.py), config.sh, and a queries.tsv "
                "listing the CANCER-SUBTYPE atlases to score against. The atlas is auto-selected from the cell line "
                "via cancer_atlas_registry.json (MDS-L -> AML/MDS Leucegene, A549 -> lung LUAD+LUSC); the GUI 'cancer "
                "atlas' field overrides it. For AML a vendored patient-count table ships too; for lung the counts are "
                "derived cluster-side from the OncoSplice MergedResult matrix. JOB_TAG is the download tag + '_concordance'.",
        "inputs": "the PSI stage's Events-dPSI drug signatures (<psi_root>/output/AltResults/AlternativeOutput), the cell-line selection",
        "outputs": "runtable/concordance/ + concordance_bundle.zip (scorer, ranker, config.sh, queries.tsv)",
    },
    "concordance_submit": {
        "title": "Run drug-vs-cancer concordance",
        "what": "(Autonomous mode only) Resolves the AltAnalyze install carrying export/UI/unique (for the scorer's "
                "PYTHONPATH; reuses what PSI resolved), uploads the concordance bundle, and queues a launcher that WAITS "
                "for AltAnalyze PSI to finish (polls <psi_root>/PIPELINE_COMPLETE.txt), then gathers the per-drug PSI "
                "signatures (CROSS-study contrasts quarantined: batch-dominated) and scores each against every cancer "
                "atlas (concordance 1=mimics, 0=reverses). Each pair is tested against its own analytic null "
                "(exact binomial, BH-FDR); the per-atlas summary lists significant reversal/mimic candidates (max 3 "
                "subtypes per drug) and EVERY scored compound on its own line. Skips cleanly when no atlas is "
                "configured for the cell line. Non-fatal.",
        "inputs": "concordance_bundle.zip, your SSH settings, the cancer atlas signatures on the cluster, the running/finished PSI stage",
        "outputs": "(remote) <concord_root>/results/<atlas>/{concordance.txt, pair_stats.tsv, ranked_concordance_summary.txt, "
                   "significant_pairs.tsv, all_scored_pairs.tsv} + results/complete_drug_by_subtype.tsv (every drug x "
                   "subtype) + results/scored_pairs_with_null.tsv + results/concordance_by_compound.tsv; "
                   "concordance_submit.json",
    },
}
