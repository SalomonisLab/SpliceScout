"""
Stage 7 — BUILD: assemble the cell-line-grouped tables from the extracted data + AI maps.

Consumes (all from the run dir):
  ncbi_raw.json            canonical sample set (every GSM + title)
  structured_samples.jsonl per-sample structured cell tag / treatments / spots
  compound_map.json        {raw_compound: {name, is_drug}}              (AI pass 1)
  sample_map.json          {raw_title_or_cellvalue: {cell_line, category, drug_treated}} (AI pass 2)
  study_protocol.json      {gse: {protocol, strategy, selection, instrument}}

Emits into tables/: ncbi_final.csv/.md (all protocols), ncbi_final_splicing.csv/.md (HEADLINE),
ncbi_final_truseq.csv/.md, ncbi_protocol_audit.csv.  build_all(P) produces all of them.
"""
import csv
import json
import os
import re
from collections import defaultdict, Counter

from normalize_v2 import clean_compound
from cell_utils import clean_struct_cell, extract_cell_line

# regex cell-line bucket -> Sample Type category
REGEX_CATEGORY = {
    "UNRESOLVED": "Unknown", "PATIENT_SAMPLE": "Patient/Tumor", "ORGANOID": "Organoid",
    "iPSC": "iPSC/ESC", "WHOLE_BLOOD": "Immune/PBMC", "PRIMARY_HEPATOCYTE": "Primary cells",
    "TCGA_TISSUE": "Tissue", "ANIMAL_TISSUE": "Tissue",
}

NONSPLICING_CLASSES = {"sci-Plex (plate)", "plate-seq", "10x/droplet scRNA",
                       "3'-tag bulk (QuantSeq/DRUG-seq/DGE)", "single-cell/nuclei"}
SC_TITLE = re.compile(r"scrna|single[\s_-]?cell|single[\s_-]?nucle|\bsnrna\b|\bsn-?seq\b|"
                      r"sci[\s_-]?(plex|rna|atac)|\b10x\b|chromium|drop[\s_-]?seq|"
                      r"cel[\s_-]?seq|mars[\s_-]?seq|plate[\s_-]?seq|\bnuclei\b|quant[\s_-]?seq", re.I)
SMARTSEQ = re.compile(r"smart[\s_-]?seq", re.I)


# Analysis modules: the selected module owns the library-prep FILTER (which protocols are kept in the
# headline table) and, downstream, the analysis pipeline. Each maps to the build() "mode" whose keep-rule
# defines its headline table. Adding a module = add an entry here (+ a keep-branch in build() if it needs
# a NEW filter). bulk_rna_seq == the original splicing-amenable filter (drop 10x/single-cell/3'-tag, keep
# Smart-seq), so output is byte-identical to before when module='bulk_rna_seq'.
MODULES = {
    "bulk_rna_seq": {"label": "Bulk RNA-seq (STAR)", "mode": "splicing"},
}
DEFAULT_MODULE = "bulk_rna_seq"


def module_mode(module):
    """The build() headline filter mode for an analysis module (falls back to the default module)."""
    return MODULES.get(module, MODULES[DEFAULT_MODULE])["mode"]


_AI_NO_ANSWER = {"", "unknown", "unresolved", "n/a", "na", "none"}


def _ai_cell(sm):
    """The AI's cell_line for a sample-map entry, or None when the AI gave no usable answer ('Unknown' --
    also the fill value for a dropped/salvaged batch). Lets callers fall back to the deterministic value
    so an AI failure is never WORSE than running without AI."""
    v = ((sm or {}).get("cell_line") or "").strip()
    return None if v.lower() in _AI_NO_ANSWER else v


def protocol_class(text):
    t = (text or "").lower()
    if re.search(r"drug-?seq|\bbrb-?seq\b|quant-?seq|cel-?seq|mars-?seq|\btag-?seq\b|"
                 r"strt-?seq|3'\s*-?\s*dge\b|3'\s*digital gene expression|"
                 r"digital gene expression", t):
        return "3'-tag bulk (QuantSeq/DRUG-seq/DGE)"
    if "sci-plex" in t or "sciplex" in t:
        return "sci-Plex (plate)"
    if "plate-seq" in t or "plateseq" in t or "plate seq" in t:
        return "plate-seq"
    if "10x" in t or "chromium" in t or "drop-seq" in t or "dropseq" in t:
        return "10x/droplet scRNA"
    if "smart-seq" in t or "smartseq" in t or "smart seq" in t:
        return "Smart-seq"
    if "single cell" in t or "single-cell" in t or "scrna" in t or "snrna" in t or "nuclei" in t:
        return "single-cell/nuclei"
    if "truseq" in t:
        return "TruSeq"
    if not t.strip():
        return "no protocol text"
    return "other bulk kit (NEBNext/KAPA/etc.)"


def build(P, mode="", is_headline=False):
    """mode in {'', 'splicing', 'truseq'} (the library-prep filter); is_headline marks the selected
    module's headline pass, which also writes cellline_index.json (the deep-dive input). Returns a
    summary dict."""
    struct = {}
    _bad = 0
    for line in open(P.samples_jsonl, encoding="utf-8"):
        if not line.strip():
            continue
        try:
            r = json.loads(line)   # was unguarded -> ONE malformed line crashed the whole build; now skip+count
        except Exception:
            _bad += 1
            continue
        if r.get("gsm"):
            struct[r["gsm"]] = r
    if _bad:
        print(f"  build: skipped {_bad} malformed line(s) in {os.path.basename(P.samples_jsonl)}")
    if not os.path.exists(P.raw_json):
        raise RuntimeError(
            f"raw_json not found: {P.raw_json} (produced by the FETCH/extract stage that writes "
            f"ncbi_raw.json) — cannot build tables without the canonical sample set")
    try:
        raw = json.load(open(P.raw_json, encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"raw_json is not valid JSON: {P.raw_json} ({e}) — re-run the FETCH stage")
    result = raw.get("result")
    if result is None:
        raise RuntimeError(f"raw_json missing 'result' key: {P.raw_json} — re-run the FETCH stage")

    compound_map, sample_map, study_protocol = {}, {}, {}
    if os.path.exists(P.compound_map):
        compound_map = json.load(open(P.compound_map, encoding="utf-8"))
    if os.path.exists(P.sample_map):
        sample_map = json.load(open(P.sample_map, encoding="utf-8"))
    if os.path.exists(P.study_protocol):
        study_protocol = json.load(open(P.study_protocol, encoding="utf-8"))

    def disp(name):
        return " + ".join((p.strip()[:1].upper() + p.strip()[1:])
                          for p in str(name).split("+") if p.strip())

    def canon_compound(raw):
        c = clean_compound(raw)
        if not c:
            return None
        info = compound_map.get(c)
        if info is None:
            return c
        if not info.get("is_drug", True):
            return None
        return disp(info.get("name") or c)

    def is_truseq(gse):
        p = study_protocol.get(gse, {})
        return "truseq" in (p.get("protocol", "") + " " + p.get("selection", "")).lower()

    def is_splicing_amenable(gse, title, category):
        ptext = study_protocol.get(gse, {}).get("protocol", "")
        if SMARTSEQ.search(ptext) or SMARTSEQ.search(title or ""):
            return True
        if protocol_class(ptext) in NONSPLICING_CLASSES:
            return False
        if category == "Single-cell":
            return False
        if SC_TITLE.search(title or ""):
            return False
        return True

    study_maxreads = defaultdict(int)
    cl = defaultdict(lambda: {"compounds": set(), "studies": set(), "total": 0,
                              "treated": 0, "not": 0, "pending": 0,
                              "max_spots": 0, "total_spots": 0,
                              "gsms": set(), "gsms_by_study": {}, "raw_tags": {},
                              "uids_by_study": {}})
    cat_counter = defaultdict(Counter)
    removed_class = Counter()
    n_struct_cell = n_ai_cell = n_regex_cell = 0

    uids = result.get("uids")
    if uids is None:
        raise RuntimeError(
            f"raw_json result missing 'uids' list: {P.raw_json} (produced by the FETCH stage) — "
            f"the file is malformed; re-run the FETCH stage")
    n_missing = sum(1 for u in uids if not isinstance(result.get(u), dict))
    if n_missing:
        # FETCH lists every search id in "uids" but writes a record only when its esummary succeeded
        print(f"  BUILD: {n_missing} study id(s) have no esummary record (failed during FETCH) -> skipped")
    for u in uids:
        item = result.get(u)
        if not isinstance(item, dict):
            continue
        gse = item.get("accession", "")
        for s in item.get("samples", []):
            gsm = s.get("accession", "")
            title = s.get("title", "")
            sd = struct.get(gsm)
            tag = (sd or {}).get("cell_line", "")

            # ---- cell line + sample-type category ----
            cleaned_tag = clean_struct_cell(tag)
            if cleaned_tag:
                sm = sample_map.get(tag)
                if sm and _ai_cell(sm):
                    cell, category = _ai_cell(sm), sm.get("category") or "Cell line"
                else:
                    # no AI answer, OR the AI could not resolve it ("Unknown" -- also what a dropped/salvaged
                    # batch is filled with): keep the depositor's own structured tag, exactly as skip-AI would.
                    # (It used to adopt "Unknown", so an AI hiccup was WORSE than no AI: a real A549 tag fell
                    # into the Unknown bucket and out of the deep dive.)
                    cell, category = cleaned_tag, "Cell line"
                n_struct_cell += 1
            else:
                sm = sample_map.get(title)
                if sm and _ai_cell(sm):
                    cell, category = _ai_cell(sm), sm.get("category") or "Cell line"
                    n_ai_cell += 1
                else:
                    cell = extract_cell_line(title, gse)
                    category = REGEX_CATEGORY.get(cell, "Cell line")
                    n_regex_cell += 1

            # ---- protocol filter ----
            if mode == "truseq" and not is_truseq(gse):
                removed_class[protocol_class(study_protocol.get(gse, {}).get("protocol", ""))] += 1
                continue
            if mode == "splicing" and not is_splicing_amenable(gse, title, category):
                removed_class[protocol_class(study_protocol.get(gse, {}).get("protocol", ""))] += 1
                continue

            # ---- compounds (canonical, non-drugs dropped) ----
            comps = []
            if sd and sd.get("treatments_raw"):
                comps = sorted({c for v in sd["treatments_raw"] for c in [canon_compound(v)] if c})

            # ---- reads ----
            sp = sd.get("spots", 0) if sd else 0
            if sp:
                study_maxreads[gse] = max(study_maxreads[gse], sp)

            # ---- drug-treated (hybrid) ----
            if sd and sd.get("treatments_raw"):
                status = "treated" if comps else "not"
            else:
                ai = sample_map.get(title)
                dt = (ai or {}).get("drug_treated", "")
                if dt in ("Drug Treated", "Not Drug Treated"):
                    status = "treated" if dt == "Drug Treated" else "not"
                else:
                    status = "pending"

            d = cl[cell]
            d["compounds"].update(comps)
            d["studies"].add(gse)
            if gsm:
                d["gsms"].add(gsm)
                d["gsms_by_study"].setdefault(gse, set()).add(gsm)
            if tag:
                d["raw_tags"][tag] = d["raw_tags"].get(tag, 0) + 1
            if gse and gse[3:].isdigit():
                d["uids_by_study"][gse] = 200000000 + int(gse[3:])
            d["total"] += 1
            d[status] += 1
            if sp:
                d["max_spots"] = max(d["max_spots"], sp)
                d["total_spots"] += sp
            cat_counter[cell][category] += 1

    # collapse cell-line spelling variants (MDS-L == MDSL, A549 == A-549 == 'A549 cells') BEFORE both the
    # index and the ranking, so the same line never splits into two rows. (The deep-dive consolidate also
    # merges variants, but only AFTER selection; this fixes the ranking + index the user actually reads.)
    cl, cat_counter = _merge_variants(cl, cat_counter)
    if is_headline:
        _write_cellline_index(P, cl, cat_counter)
    return _write_tables(P, mode, cl, cat_counter, study_maxreads, removed_class,
                         n_struct_cell, n_ai_cell, n_regex_cell)


def _cl_norm(s):
    """Normalized cell-line key: lowercase, alphanumerics only -> 'MDS-L'/'MDSL'/'mds l' all == 'mdsl'
    (same rule as cellline_match._norm; A549 == A-549 == 'A549 cells')."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _merge_variants(cl, cat_counter):
    """Merge cell-line entries whose normalized name collides, keeping the spelling with the MOST samples
    (tie -> longest, then lexical) as the display name. Returns (cl, cat_counter) rekeyed by that display
    name. A no-op (returns the originals) when no two raw names normalize to the same key."""
    groups = defaultdict(list)        # norm_key -> [raw cell names]
    for cell in cl:
        groups[_cl_norm(cell) or cell].append(cell)
    if all(len(v) == 1 for v in groups.values()):
        return cl, cat_counter        # nothing collides -> unchanged
    _SET_KEYS = ("compounds", "studies", "gsms")
    _INT_KEYS = ("total", "treated", "not", "pending", "total_spots")
    new_cl, new_cat = {}, defaultdict(Counter)
    for cells in groups.values():
        disp = sorted(cells, key=lambda c: (cl[c]["total"], len(c), c))[-1]
        if len(cells) == 1:
            new_cl[disp] = cl[cells[0]]
            new_cat[disp] = cat_counter.get(cells[0], Counter())
            continue
        m = {"compounds": set(), "studies": set(), "gsms": set(), "total": 0, "treated": 0, "not": 0,
             "pending": 0, "max_spots": 0, "total_spots": 0, "gsms_by_study": {}, "raw_tags": {},
             "uids_by_study": {}}
        cc = Counter()
        for c in cells:
            d = cl[c]
            for k in _SET_KEYS:
                m[k] |= d[k]
            for k in _INT_KEYS:
                m[k] += d[k]
            m["max_spots"] = max(m["max_spots"], d["max_spots"])
            for g, s in d["gsms_by_study"].items():
                m["gsms_by_study"].setdefault(g, set()).update(s)
            for t, n in d["raw_tags"].items():
                m["raw_tags"][t] = m["raw_tags"].get(t, 0) + n
            m["uids_by_study"].update(d["uids_by_study"])
            cc.update(cat_counter.get(c, Counter()))
        new_cl[disp] = m
        new_cat[disp] = cc
    return new_cl, new_cat


def _safe_open(path):
    """Return a writable path, falling back to *_v2 if the target is locked (Excel)."""
    try:
        open(path, "a", encoding="utf-8").close()
        return path
    except PermissionError:
        alt = path.replace(".csv", "_v2.csv").replace(".md", "_v2.md")
        print(f"  ** {os.path.basename(path)} locked -> writing {os.path.basename(alt)} **")
        return alt


def _write_tables(P, mode, cl, cat_counter, study_maxreads, removed_class,
                  n_struct_cell, n_ai_cell, n_regex_cell):
    suffix = {"": "", "splicing": "_splicing", "truseq": "_truseq"}[mode]
    out_csv = _safe_open(P.final_csv.replace(".csv", f"{suffix}.csv"))
    out_md = _safe_open(P.final_md.replace(".md", f"{suffix}.md"))

    ranked = sorted(cl.items(), key=lambda x: (len(x[1]["compounds"]), x[1]["total"]), reverse=True)

    def studies_str(studies):
        ss = sorted(studies)
        return "; ".join(ss[:10]) + (f" (+{len(ss)-10} more)" if len(ss) > 10 else "")

    def reads_str(studies):
        parts = []
        for g in sorted(studies)[:10]:
            mr = study_maxreads.get(g, 0)
            parts.append(f"{g}:{mr:,}" if mr else f"{g}:N/A")
        if len(studies) > 10:
            parts.append(f"(+{len(studies)-10} more)")
        return "; ".join(parts)

    def comps_str(comps, cap=50):
        cs = sorted(comps)
        return "; ".join(cs[:cap]) + (f"; (+{len(cs)-cap} more)" if len(cs) > cap else "")

    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Cell Line", "Sample Type", "# Unique Compounds (Total)", "# Studies",
                    "All Compounds", "Drug Treated", "Not Drug Treated", "Undetermined",
                    "Total Samples", "Max Reads/Sample (spots)", "Total Reads (spots)",
                    "Study Accessions", "SRA Read Counts"])
        for i, (cell, d) in enumerate(ranked, 1):
            stype = cat_counter[cell].most_common(1)[0][0] if cat_counter[cell] else "Cell line"
            w.writerow([i, cell, stype, len(d["compounds"]), len(d["studies"]),
                        comps_str(d["compounds"]), d["treated"], d["not"], d["pending"],
                        d["total"], d["max_spots"], d["total_spots"],
                        studies_str(d["studies"]), reads_str(d["studies"])])

    total = sum(d["total"] for _, d in ranked)
    treated = sum(d["treated"] for _, d in ranked)
    not_t = sum(d["not"] for _, d in ranked)
    undet = sum(d["pending"] for _, d in ranked)
    all_comp = set()
    for _, d in ranked:
        all_comp.update(d["compounds"])
    label = {"": "all protocols", "splicing": "splicing-amenable (HEADLINE)",
             "truseq": "TruSeq-only"}[mode]
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(f"# NCBI RNA-seq — Cell-Line Compound Analysis ({label})\n\n")
        f.write(f"- Cell lines/buckets: {len(ranked)}\n- Unique compounds: {len(all_comp)}\n")
        f.write(f"- Total samples: {total:,}\n- Drug Treated: {treated:,} | "
                f"Not Drug Treated: {not_t:,} | Undetermined: {undet:,}\n")
        f.write(f"- Cell source: structured tag {n_struct_cell:,} | AI title {n_ai_cell:,} | regex {n_regex_cell:,}\n\n")
        f.write("| Rank | Cell Line | Type | #Cmpd | #Studies | Treated | Not | Undet | Total | Top Compounds |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|\n")
        for i, (cell, d) in enumerate(ranked[:60], 1):
            stype = cat_counter[cell].most_common(1)[0][0] if cat_counter[cell] else "Cell line"
            cs = sorted(d["compounds"])
            top = ", ".join(cs[:6]) + (f" (+{len(cs)-6})" if len(cs) > 6 else "")
            f.write(f"| {i} | {cell} | {stype} | {len(d['compounds'])} | {len(d['studies'])} | "
                    f"{d['treated']} | {d['not']} | {d['pending']} | {d['total']} | {top} |\n")

    msg = f"[{label}] cell lines={len(ranked)} compounds={len(all_comp)} samples={total:,} " \
          f"treated={treated:,} not={not_t:,} undet={undet:,}"
    if mode:
        msg += " | removed: " + ", ".join(f"{n} {c}" for c, n in removed_class.most_common())
    print(msg)
    print(f"  wrote {out_csv}")
    return {"cell_lines": len(ranked), "compounds": len(all_comp), "samples": total,
            "removed": dict(removed_class)}


def _write_protocol_audit(P):
    if not os.path.exists(P.study_protocol):
        return
    sp = json.load(open(P.study_protocol, encoding="utf-8"))
    path = _safe_open(P.protocol_audit)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Study", "Protocol Class", "Library Strategy", "Library Selection",
                    "Instrument", "Protocol (snippet)"])
        for gse, p in sorted(sp.items()):
            w.writerow([gse, protocol_class(p.get("protocol", "")), p.get("strategy", ""),
                        p.get("selection", ""), p.get("instrument", ""),
                        (p.get("protocol", "") or "")[:200]])
    print(f"  wrote {path}")


def _write_cellline_index(P, cl, cat_counter):
    """Persist the FULL per-cell-line aggregate (studies + GSMs, untruncated) for the deep dive.

    The CSV truncates studies to 10 and never lists GSMs; the deep-dive selection + run-table
    filter need the complete picture, so we dump it here from the (splicing) grouping.
    """
    index = {}
    for cell, d in cl.items():
        stype = cat_counter[cell].most_common(1)[0][0] if cat_counter[cell] else "Cell line"
        index[cell] = {
            "sample_type": stype,
            "compounds": sorted(d["compounds"]),
            "studies": sorted(d["studies"]),
            "total": d["total"],
            "treated": d["treated"],
            "not": d["not"],
            "undetermined": d["pending"],
            "total_spots": d["total_spots"],
            "max_spots": d["max_spots"],
            "gsms": sorted(d["gsms"]),
            "gsms_by_study": {g: sorted(v) for g, v in d["gsms_by_study"].items()},
            "raw_tags": d["raw_tags"],
            "uids_by_study": d["uids_by_study"],
        }
    try:
        tmp = str(P.cellline_index) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, P.cellline_index)
        print(f"  wrote {P.cellline_index} ({len(index)} cell lines)")
    except Exception as e:
        print(f"  ** could not write cellline_index.json: {e} **")


def skipped_no_sra(P):
    """Studies FETCHED from GEO (they have GEO sample records) that yielded ZERO downloadable samples in
    extraction — i.e. they have NO raw SRA sequencing runs (microarray, processed-only, or computational
    re-analysis studies), so a download→align→splice pipeline has nothing to fetch for them. This is why a
    study you can see in GEO can legitimately show "0 samples" here. Returns a list of
    {gse, title, n_geo_samples, gdstype} sorted by size. Reads ncbi_raw.json + structured_samples.jsonl
    (+ study_protocol.json to gate on PROCESSED studies), so it works for any run post-extraction."""
    if not os.path.exists(P.raw_json):
        return []
    try:
        raw = json.load(open(P.raw_json, encoding="utf-8")).get("result", {})
    except Exception:
        return []
    studies = {}
    for uid in raw.get("uids", []):
        s = raw.get(uid) or {}
        acc = (s.get("accession") or "").strip()
        if acc.startswith("GSE"):
            studies[acc] = {"gse": acc, "title": (s.get("title") or "").strip(),
                            "n_geo_samples": int(s.get("n_samples", 0) or 0),
                            "gdstype": (s.get("gdstype") or "").strip()}
    extracted = set()
    if os.path.exists(P.samples_jsonl):
        for line in open(P.samples_jsonl, encoding="utf-8"):
            try:
                g = (json.loads(line).get("study") or "").strip()
            except Exception:
                g = ""
            if g:
                extracted.add(g)
    processed = None
    try:                       # only flag PROCESSED studies, so a mid-run doesn't list not-yet-extracted ones
        processed = set(json.load(open(P.study_protocol, encoding="utf-8")).keys())
    except Exception:
        processed = None
    out = [info for acc, info in studies.items()
           if info["n_geo_samples"] > 0 and acc not in extracted
           and (processed is None or acc in processed)]
    out.sort(key=lambda d: -d["n_geo_samples"])
    return out


def _write_skipped_no_sra(P):
    """Per-run transparency report: which fetched studies were skipped for having no SRA runs, + why."""
    rows = skipped_no_sra(P)
    path = _safe_open(os.path.join(P.tables_dir, "skipped_no_sra.csv"))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Study", "GEO Samples", "Assay Type (gdsType)", "Reason", "Title"])
        for r in rows:
            w.writerow([r["gse"], r["n_geo_samples"], r["gdstype"],
                        "0 SRA samples extracted (array=none; sequencing-labeled may need re-extract)",
                        r["title"]])
    n_studies, n_samples = len(rows), sum(r["n_geo_samples"] for r in rows)
    print(f"  wrote {path} ({n_studies} studies / {n_samples:,} GEO samples skipped: no SRA runs)")
    return {"studies": n_studies, "geo_samples": n_samples,
            "csv": os.path.relpath(path, P.run_dir).replace("\\", "/")}


def build_all(P, module=DEFAULT_MODULE):
    """Emit the selected module's headline table (its library-prep filter) + the all-protocols and
    TruSeq reference tables. Returns a summary for the front end. bulk_rna_seq maps to mode 'splicing',
    so output is byte-identical to before when module='bulk_rna_seq'."""
    headline_mode = module_mode(module)
    splicing = build(P, headline_mode, is_headline=True)   # headline + cellline_index (deep-dive input)
    allp = build(P, "")
    truseq = build(P, "truseq")
    _write_protocol_audit(P)
    skipped = _write_skipped_no_sra(P)   # transparency: fetched-but-no-SRA studies (why a GEO study shows 0 samples)
    return {"splicing": splicing, "all": allp, "truseq": truseq, "module": module, "skipped_no_sra": skipped}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--mode", default="all", choices=["all", "", "splicing", "truseq"])
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    if a.mode == "all":
        build_all(P)
    else:
        build(P, a.mode, is_headline=(a.mode == module_mode(DEFAULT_MODULE)))


if __name__ == "__main__":
    main()
