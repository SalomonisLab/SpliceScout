# -*- coding: utf-8 -*-
"""
Deep-dive stage — CELL-LINE MATCH + FILTER (the disambiguation agent).

The reconstructed run table for the selected studies contains runs from MANY cell lines, and the
same line is written many ways across studies (A549 / A-549 / A 549 / "A549 cells"). This stage
decides which cell-line-name values are the target line and keeps only those runs.

Hybrid keep rule (maximize recall without widening past the target line):
  keep a run if  (a) its GSM was already classified as the target line by the main pipeline
                     (deterministic floor, always available from cellline_selection.json), OR
                 (b) its cell-line value was blessed by the agent (recall for spelling variants).

The agent (Anthropic API, forced tool-use, prompt-cached) includes a value ONLY if it is, or
clearly resembles, the target line; unrelated lines (BEAS-2B) are excluded. When skip_ai, a
deterministic normalized-equality fallback is used (lowercase + strip non-alphanumeric), which
still merges A549 / A-549 / A 549.

MAIN OUTPUT: SraAccList.txt — the flat list of SRR run accessions for the kept (target-line) runs,
consumable by `prefetch --option-file SraAccList.txt`. Written both combined and per-study
(by_study/<GSE>/SraAccList.txt), matching the user's SRA_download_scripts layout.
"""
import os
import re
import csv
import json
import asyncio

from progress import NULL
from runtable_build import order_columns, strip_rep
from build_final import _safe_open
import llm_providers

INSTRUCTIONS = (
    "You decide which cell-line-name strings refer to the SAME human cell line as a given target.\n"
    "You receive a JSON object with `target` (the canonical target line), `target_aliases` (other "
    "spellings already known to be that line), and `candidates` (an array of cell-line-name values "
    "seen in SRA metadata, each with the column(s) it came from and a run count).\n"
    "For EVERY candidate, call the emit tool once with a result object:\n"
    "- value: the EXACT candidate string, unchanged.\n"
    "- matches: true ONLY IF the value IS, or clearly RESEMBLES, the target line — i.e. the same line "
    "written with different formatting/spacing/case, a trailing 'cells'/'cell line', or a same-root "
    "tagged form (e.g. for target 'A549': 'A549', 'A-549', 'A 549', 'A549 cells', 'A549-GFP' all "
    "match). Set matches=false for a clearly DIFFERENT line (e.g. 'BEAS-2B' when the target is "
    "'A549'). When genuinely unsure whether two names are the same line, return false.\n"
    "- canonical: your normalized name for the value (used only for logging).\n"
    "- reason: a few words (e.g. 'hyphenation variant', 'different line').\n"
    "Return one result per candidate, preserving every exact value string."
)

TOOL = {
    "name": "emit_cellline_matches",
    "description": "Return, for every candidate cell-line value, whether it is the target line.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value", "matches", "canonical", "reason"],
                    "properties": {
                        "value": {"type": "string"},
                        "matches": {"type": "boolean"},
                        "canonical": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
}


_NORM_SUB = re.compile(r"[^a-z0-9]")    # precompiled once: the hot per-row field normalizer in the gate


def _norm(s):
    return _NORM_SUB.sub("", (s or "").lower())


# ---- run-level splicing-suitability gate (only applied when the caller passes assay_keep, i.e. the
# bulk_rna_seq/splicing module) -------------------------------------------------------------------------
# The reconstructed runtable carries SRA's per-run controlled-vocab columns, which let us drop — at RUN
# granularity — exactly what the upstream study-level filter (build_final.is_splicing_amenable, which works
# off LIBRARY_CONSTRUCTION_PROTOCOL text + the AI 'Single-cell' category + title) cannot see reliably:
#   1. Assay Type  (LIBRARY_STRATEGY): keep only the wanted strategy (RNA-Seq) -> drops ChIP/MeDIP/ATAC/WGS/
#      Bisulfite/OTHER(RASL etc.). A controlled vocabulary, so the broad class is distinguished precisely.
#   2. Platform / Instrument: drop LONG-READ (Oxford Nanopore, PacBio). STAR is a SHORT-READ aligner, so
#      these break alignment outright; the instrument names them exactly (MinION/GridION/PromethION, Sequel/
#      PacBio RS). This is the highest-value add — long-read is NOT caught upstream and is common in big sets.
#   3. LibrarySelection: drop non-full-length-transcript RNA methods unfit for PSI — CAGE (5'-cap tags), RACE
#      (targeted), size fractionation (small-RNA). Standard selections (cDNA/PolyA/Oligo-dT/RANDOM/Inverse
#      rRNA ribo-depletion) are kept; these are the splicing-friendly preps.
#   4. scRNA/droplet backstop: single-cell is PRIMARILY excluded upstream, but a run can re-enter here by
#      matching the target cell-line VALUE even though its GSM was filtered out of the splicing table. Drop it
#      if its runtable text carries a single-cell platform marker. NOTE: platform/instrument/LibrarySelection
#      do NOT separate scRNA from bulk (both are Illumina + cDNA) — the only run-level signal is this text.
# Library STRATEGY alone cannot tell bulk from single-cell; that distinction lives in the protocol/title text.
_LONGREAD_PLAT = {"oxfordnanopore", "pacbiosmrt"}
_LONGREAD_INSTR = re.compile(r"min\s*ion|grid\s*ion|prometh\s*ion|\bsequel\b|pac\s*bio|\brs\s*ii\b", re.I)
_BAD_SELECTION = {"cage", "race", "sizefractionation"}    # non-splicing RNA library selections (drop)
_SC_RUN = re.compile(r"10\s*x\b|chromium|single[\s_-]*cell|single[\s_-]*nucle|\bscrna\b|\bsnrna\b|"
                     r"drop[\s_-]*seq|cel[\s_-]*seq|smart[\s_-]*seq|mars[\s_-]*seq|sci[\s_-]*rna|"
                     r"seq[\s_-]*well|indrop|microwell|\bnuclei\b|visium|multiome|cite[\s_-]*seq", re.I)
_SC_TEXT_COLS = ("Library Name", "source_name", "Sample Name", "cell_type", "Experiment")


def _splice_drop_reason(r, assay_keep):
    """Return None to KEEP a target-line run for short-read splicing PSI, else a short 'reason:detail' string
    explaining why it is unfit. Only consulted when assay_keep is set (the splicing module)."""
    if not assay_keep:                       # non-splicing module -> no run-level gate, keep everything
        return None
    if _NORM_SUB.sub("", (r.get("Assay Type") or "").lower()) not in assay_keep:
        return f"assay:{r.get('Assay Type') or '?'}"
    if (_NORM_SUB.sub("", (r.get("Platform") or "").lower()) in _LONGREAD_PLAT
            or _LONGREAD_INSTR.search(r.get("Instrument") or "")):
        return f"longread:{r.get('Instrument') or r.get('Platform') or '?'}"
    if _NORM_SUB.sub("", (r.get("LibrarySelection") or "").lower()) in _BAD_SELECTION:
        return f"selection:{r.get('LibrarySelection')}"
    if _SC_RUN.search(" ".join((r.get(c) or "") for c in _SC_TEXT_COLS)):
        return "single-cell"
    return None


def _deterministic(target, aliases, candidate_values):
    """Normalized-equality match: A549 == A-549 == A 549 == 'A549 cells' (-> a549)."""
    targets = {_norm(target)} | {_norm(a) for a in aliases}
    targets.discard("")
    out = {}
    for v in candidate_values:
        nv = _norm(v)
        hit = nv in targets or any(nv == t for t in targets)
        out[v] = {"matches": bool(hit), "canonical": target if hit else v,
                  "reason": "normalized match" if hit else "no normalized match"}
    return out


async def _classify_async(payload, ai_cfg, cost_log):
    ai_cfg = ai_cfg or {}
    provider = llm_providers.normalize_provider(ai_cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, ai_cfg.get("model"))
    user = {"target": payload.get("target", ""),
            "target_aliases": payload.get("target_aliases", []),
            "candidates": [c["value"] if isinstance(c, dict) else c
                           for c in payload.get("candidates", [])]}
    client = llm_providers.make_client(provider, ai_cfg.get("max_retries", 8),
                                       base_url=ai_cfg.get("base_url"))
    try:
        results, usage = await llm_providers.classify(
            client, provider, model, INSTRUCTIONS, user, TOOL, ai_cfg.get("max_tokens", 8000),
            disable_reasoning=ai_cfg.get("disable_reasoning", False))
    finally:
        await llm_providers.close_client(client)

    out = {}
    for r in results:
        if isinstance(r, dict) and r.get("value"):
            out[r["value"]] = {"matches": bool(r.get("matches")),
                               "canonical": r.get("canonical") or "",
                               "reason": r.get("reason") or ""}
    try:
        with open(cost_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"pass": "cmatch", "provider": provider, "model": model,
                                "n_in": len(user["candidates"]), "n_out": len(out),
                                "input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"],
                                "cache_read": usage["cache_read"], "cache_creation": usage["cache_creation"]}) + "\n")
    except Exception:
        pass
    return out


def _slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "cellline"


def _write_acc_lists(P, keep_rows):
    """MAIN OUTPUT: combined SraAccList.txt + per-study by_study/<GSE>/SraAccList.txt."""
    combined = sorted({(r.get("Run") or "").strip() for r in keep_rows if (r.get("Run") or "").strip()})
    with open(_safe_open(P.sra_acc_list), "w", encoding="utf-8") as f:
        f.write("\n".join(combined) + ("\n" if combined else ""))
    # Each unique run goes into EXACTLY ONE study's list. A run shared by many GEO series (K562: often 100+
    # series point at the SAME SRA study) must be downloaded + STAR-aligned ONCE, not once per series -- else
    # the cluster launches ~one job per (run x series): here 8,421 runs would become ~1.15M jobs. The per-
    # cell-line PSI is a SINGLE grouped (drug-vs-control) analysis that reads every run's BED regardless of
    # which study folder holds it, so collapsing to one folder per run just avoids re-downloading/re-aligning
    # identical reads. by_study still spans every study that has at least one first-seen run.
    by_study, _assigned = {}, set()
    for r in keep_rows:
        run_acc = (r.get("Run") or "").strip()
        gse = (r.get("GSE_Series") or "").strip() or "unknown"
        if run_acc and run_acc not in _assigned:
            _assigned.add(run_acc)
            by_study.setdefault(gse, set()).add(run_acc)
    for gse, runs in by_study.items():
        d = os.path.join(P.by_study_dir, gse)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SraAccList.txt"), "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(runs)) + "\n")
    return combined, by_study


def run(P, sel, ai_cfg=None, skip_ai=False, reporter=NULL, assay_keep=None):
    """Match cell-line names, filter the run table to the target line, emit SraAccList + filtered CSV.
    `assay_keep`: optional set of NORMALIZED library strategies to keep (e.g. {"rnaseq"}). When set (the
    splicing/bulk_rna_seq module passes it), it ALSO arms the full run-level splicing-suitability gate
    (_splice_drop_reason): drops non-RNA-Seq strategies, long-read platforms (Nanopore/PacBio, which STAR
    can't align), non-splicing LibrarySelections (CAGE/RACE/size-fractionation), and any single-cell run
    that slipped past the upstream study-level filter — so only short-read bulk RNA-seq reaches STAR/
    AltAnalyze. None keeps every run (non-splicing modules)."""
    if not os.path.exists(P.match_candidates) or not os.path.exists(P.runtable_all_csv):
        print("  CMATCH: missing inputs (no candidates / run table) -> skipping")
        return None
    if sel is None:
        print("  CMATCH: missing cell-line selection (sel is None) -> skipping")
        return None
    with open(P.match_candidates, encoding="utf-8") as f:
        payload = json.load(f)
    cand_values = [c["value"] if isinstance(c, dict) else c for c in payload.get("candidates", [])]
    target = payload.get("target", sel.get("canonical", ""))
    aliases = list(payload.get("target_aliases", []))
    # also recognize the spellings merged by the pre-select consolidation (deepdive_select.consolidate)
    for a in (sel.get("aliases") or []):
        if a and a not in aliases:
            aliases.append(a)
    payload["target_aliases"] = aliases     # so the AI agent (_classify_async) sees them too
    cl_cols = payload.get("cellline_columns", [])

    reporter.set_total(1)
    if skip_ai or not cand_values:
        match = _deterministic(target, aliases, cand_values)
        mode = "deterministic"
    else:
        try:
            match = asyncio.run(_classify_async(payload, ai_cfg or {"model": "claude-haiku-4-5"}, P.cost_log))
            mode = "ai"
            # any candidate the model didn't return -> deterministic backstop
            for v in cand_values:
                if v not in match:
                    match[v] = _deterministic(target, aliases, [v])[v]
        except Exception as e:
            print(f"  CMATCH: AI match failed ({e}) -> deterministic fallback")
            match = _deterministic(target, aliases, cand_values)
            mode = "deterministic(fallback)"
    reporter.advance(1)

    _tmp = P.cellline_match + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump({"target": target, "mode": mode, "matches": match},
                  f, ensure_ascii=False, indent=1)
        f.flush(); os.fsync(f.fileno())
    os.replace(_tmp, P.cellline_match)

    # hybrid keep: GSM floor (already classified) OR agent-blessed cell-line value
    keep_gsms = {strip_rep(g) for g in sel.get("gsms", [])}
    match_vals = {v for v, info in match.items() if info.get("matches")}

    def is_line(r):
        if strip_rep((r.get("GEO_Accession (exp)") or "")) in keep_gsms:
            return True
        return any((r.get(c) or "").strip() in match_vals for c in cl_cols)

    # SPLICING SUITABILITY GATE (splicing modules only): keep a target-line run only if it is usable for
    # short-read splicing PSI — right strategy (RNA-Seq), short-read (not Nanopore/PacBio), a splicing-
    # friendly LibrarySelection, and not single-cell. See _splice_drop_reason for the rationale per check.
    # PERF: ONE streaming pass over the runtable — was two (a keep pass + a separate drop-reason pass) that each
    # re-evaluated is_line/_splice_drop_reason over every row (~210k for A549). is_line runs once per row,
    # _splice_drop_reason once per target-line row; keep_rows / _drop / the log are bit-identical to before.
    from collections import Counter as _Counter
    # STREAM the runtable row-by-row. It can be GBs (this study set's SraRunTable_all.csv was 3.9 GB), and
    # list(csv.DictReader(...)) inflated it several-fold and blew past RAM -> MemoryError on a 15 GB box. We
    # keep only the target-line subset, so peak memory is O(kept), not O(all runs). n_all just counts the scan.
    keep_rows, _drop, n_all = [], [], 0
    with open(P.runtable_all_csv, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            n_all += 1
            if not is_line(r):
                continue
            why = _splice_drop_reason(r, assay_keep) if assay_keep else None
            if why is None:
                keep_rows.append(r)
            else:
                _drop.append((r, why))
    if assay_keep and _drop:
        _by = _Counter(why.split(":")[0] for _, why in _drop)
        _ex = {}
        for _, why in _drop:                      # one example detail per reason, for the log
            k, _, v = why.partition(":")
            _ex.setdefault(k, v)
        print(f"  CMATCH SPLICE GATE: dropped {len(_drop)} target-line run(s) unfit for short-read "
              f"splicing PSI -> {dict(_by)} (keep strategy={sorted(assay_keep)}; e.g. {_ex})")

    if not keep_rows:
        print("  " + "!" * 70)
        print(f"  CMATCH WARNING: ZERO runs matched target={target!r} "
              f"(mode={mode}; scanned {n_all} runs, "
              f"{len(keep_gsms)} GSM floor, {len(match_vals)} matched value(s)).")
        print("  CMATCH WARNING: emitting an EMPTY SraAccList.txt — nothing will be downloaded.")
        print("  " + "!" * 70)

    # filtered run table -- ONE row per unique RUN. The reconstruction emits a row per (run x series), so a
    # heavily-reused line like K562 (8,421 runs shared across ~1,128 series -> ~136 rows/run -> 1.15M rows)
    # balloons the table. The Run Selector format is one-row-per-run, and everything that reads this table
    # (annotate, the workbook, PSI grouping) wants unique runs -- collapse duplicates here. The acc list
    # already dedups, and by_study still sees every run's series because _write_acc_lists gets full keep_rows.
    keys = set()
    for r in keep_rows:
        keys.update(r)
    cols = order_columns(keys, add_gse=True) if keys else order_columns(set(), add_gse=True)
    slug = _slug(target)
    out_csv = _safe_open(P.runtable_filtered_csv(slug))
    _seen, _ndup = set(), 0
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in keep_rows:
            acc = r.get("Run", "")
            if acc and acc in _seen:                    # same run under another series -> already written
                _ndup += 1
                continue
            _seen.add(acc)
            w.writerow(r)
    if _ndup:
        print(f"  CMATCH: filtered table collapsed {_ndup:,} duplicate (run x series) rows -> "
              f"{len(_seen):,} unique runs")

    combined, by_study = _write_acc_lists(P, keep_rows)

    matched_names = sorted(match_vals)
    print(f"  CMATCH ({mode}): target={target!r} | matched values={matched_names} | "
          f"kept {len(keep_rows)}/{n_all} runs across {len(by_study)} studies")
    print(f"  MAIN OUTPUT -> {P.sra_acc_list} ({len(combined)} run accessions)")
    reporter.set_detail(f"{len(combined)} runs kept ({mode}); matched: {', '.join(matched_names) or 'GSM-only'}")
    return {"target": target, "slug": slug, "mode": mode,
            "n_runs_all": n_all, "n_runs_kept": len(keep_rows),
            "n_accessions": len(combined), "n_studies": len(by_study),
            "matched_values": matched_names, "filtered_csv": out_csv}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--provider", default="anthropic", choices=list(llm_providers.PROVIDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--skip-ai", action="store_true")
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    with open(P.cellline_selection, encoding="utf-8") as f:
        sel = json.load(f)
    run(P, sel, {"provider": a.provider, "model": a.model}, a.skip_ai)


if __name__ == "__main__":
    main()
