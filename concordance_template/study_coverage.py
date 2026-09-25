#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""study_coverage.py -- WHICH STUDIES MADE IT INTO THE CONCORDANCE RANKING, and where did the others drop out?

A drug's splicing signature only reaches the ranking if its STUDY survives every step:
  1 planned   the PC grouping gave the study a replicated drug condition AND a control comparison
              (own controls = study-matched; otherwise a borrowed control from another study = cross-study)
  2 after BED the comparison survived the cluster-side BED intersection (both groups still >= 2 samples)
  3 written   AltAnalyze actually WROTE the comparison's dPSI file (Events-dPSI_*/PSI.<cond>_vs_<ctrl>.txt)
  4 gathered  the concordance stage kept it (cross-study contrasts are quarantined when STUDY_MATCHED_ONLY=1)
  5 scored    it shared >= 5 events with at least one cancer subtype (a cell with ':' in concordance.txt)
This prints a funnel of STUDY counts at each step plus the step at which each study was lost, and writes a
per-study TSV. Pure stdlib, python 2.7 AND 3 -- runs on the cluster (it ships in the concordance bundle and the
concordance job appends its report to every ranked summary) or anywhere the folders are readable.

Usage (on the cluster, for an EXISTING run -- nothing is modified):
  python study_coverage.py --root <PIPELINE_ROOT>/<instance>_<cellline>      (the folder holding psi/, concordance/)
  optional: --out coverage.tsv   --psi-root DIR   --concordance-root DIR
On the PC, for a run folder (only the plan -- step 1 -- is available there):
  python study_coverage.py --run-dir runs/<run>
"""
from __future__ import print_function, division
import getopt
import glob
import io
import os
import re
import sys

_GSE = re.compile(r"GSE\d+")


def gse_of(label):
    m = _GSE.search(label or "")
    return m.group(0) if m else ""


def _read_tsv(path, ncol):
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.rstrip("\r\n")
            if not ln.strip() or ln.startswith("#"):
                continue
            t = ln.split("\t")
            if len(t) >= ncol:
                rows.append(t)
    return rows


def _first(pattern):
    hits = sorted(glob.glob(pattern))
    return hits[0] if hits else None


def is_control_label(label):
    return label == "control" or label.endswith(".control") or label.endswith("not_drug_treated")


def plan_from_groups(groups_rows, comps_rows, min_per_group=2):
    """(num->label, label->set(samples), [(exp_label, base_label)]) from sample_groups.tsv-style rows (sample,
    num, label) and comps rows (exp_num, base_num). No comps -> every group with >= min_per_group samples vs the
    lowest-numbered one (build_groups.sh's pooled default)."""
    lab_of, samples = {}, {}
    for t in groups_rows:
        s, num, lab = t[0], t[1].strip(), (t[2].strip() if len(t) > 2 else "group" + t[1].strip())
        lab_of[num] = lab
        samples.setdefault(lab, set()).add(s)
    comps = []
    if comps_rows:
        for t in comps_rows:
            e, b = t[0].strip(), t[1].strip()
            if e in lab_of and b in lab_of:
                comps.append((lab_of[e], lab_of[b]))
    elif lab_of:
        ok = [n for n in lab_of if len(samples.get(lab_of[n], ())) >= min_per_group]
        try:
            ok.sort(key=lambda x: int(x))
        except ValueError:
            ok.sort()
        if ok:
            comps = [(lab_of[n], lab_of[ok[0]]) for n in ok[1:]]
    return lab_of, samples, comps


def study_funnel(root=None, psi_root=None, conc_root=None, concordance_files=None):
    """concordance_files: the concordance.txt file(s) that define 'scored' (default: every results/*/ atlas)."""
    psi_root = psi_root or (os.path.join(root, "psi") if root else None)
    conc_root = conc_root or (os.path.join(root, "concordance") if root else None)
    per = {}

    def S(g):
        return per.setdefault(g or "(no GSE)", {
            "planned_conditions": 0, "unreplicated_conditions": 0, "control_samples": 0,
            "planned_own": 0, "planned_crossstudy": 0, "after_bed": 0, "written": 0,
            "gathered": 0, "quarantined": 0, "scored": 0})

    # ---- 1 PLAN (shipped by the PC: sample_groups.tsv + optional sample_comps.tsv) ----
    sg = _read_tsv(os.path.join(psi_root, "sample_groups.tsv") if psi_root else None, 3)
    sc = _read_tsv(os.path.join(psi_root, "sample_comps.tsv") if psi_root else None, 2)
    _lab_of, samples, comps = plan_from_groups(sg, sc)
    compared = set(e for e, _b in comps)
    for lab, ss in samples.items():
        g = gse_of(lab)
        if is_control_label(lab):
            S(g)["control_samples"] += len(ss)
        else:
            S(g)["planned_conditions"] += 1
            if lab not in compared:
                S(g)["unreplicated_conditions"] += 1
    for e, b in comps:
        ge, gb = gse_of(e), gse_of(b)
        cross = ("MULTISTUDY" in b) or (bool(ge) and bool(gb) and ge != gb)
        S(ge)["planned_crossstudy" if cross else "planned_own"] += 1

    # ---- 2 AFTER BED (cluster build_groups.sh: groups/comps actually handed to AltAnalyze) ----
    have_cluster = False
    if psi_root:
        gpath = _first(os.path.join(psi_root, "output", "ExpressionInput", "groups.*.txt"))
        cpath = _first(os.path.join(psi_root, "output", "ExpressionInput", "comps.*.txt"))
        if gpath or cpath:
            have_cluster = True
            _l2, _s2, comps2 = plan_from_groups(_read_tsv(gpath, 3), _read_tsv(cpath, 2))
            for e, b in comps2:
                S(gse_of(e))["after_bed"] += 1

        # ---- 3 WRITTEN (AltAnalyze dPSI files; PSI_COMPARISONS.tsv when the new PSI job wrote it) ----
        written = set()
        for f in glob.glob(os.path.join(psi_root, "output", "AltResults", "AlternativeOutput", "Events-dPSI_*", "PSI.*_vs_*.txt*")):
            b = os.path.basename(f)
            b = b[:-3] if b.endswith(".gz") else b
            written.add(b)
        for b in written:
            S(gse_of(b.split("_vs_")[0]))["written"] += 1

    # ---- 4 GATHERED / QUARANTINED, 5 SCORED (concordance stage) ----
    have_conc = False
    if conc_root and os.path.isdir(conc_root):
        have_conc = True
        for f in glob.glob(os.path.join(conc_root, "drug_signatures", "PSI.*_vs_*.txt")):
            S(gse_of(os.path.basename(f).split("_vs_")[0]))["gathered"] += 1
        for f in glob.glob(os.path.join(conc_root, "drug_signatures_cross_study", "PSI.*_vs_*.txt")):
            S(gse_of(os.path.basename(f).split("_vs_")[0]))["quarantined"] += 1
        scored = set()
        for cf in (concordance_files or glob.glob(os.path.join(conc_root, "results", "*", "concordance.txt"))):
            with io.open(cf, encoding="utf-8", errors="replace") as fh:
                fh.readline()
                for ln in fh:
                    t = ln.rstrip("\r\n").split("\t")
                    if len(t) > 1 and any(":" in c for c in t[1:]):
                        scored.add(t[0])
        for d in scored:
            S(gse_of(d.split("_vs_")[0]))["scored"] += 1
    return per, have_cluster, have_conc


def verdict(p, have_cluster, have_conc):
    if have_conc and p["scored"] > 0:
        return "IN RANKING"
    if p["planned_conditions"] == 0:
        return "no drug condition (all Undetermined / control-only)"
    if p["planned_own"] == 0 and p["planned_crossstudy"] == 0:
        return "all conditions un-replicated (1 sample each)"
    if p["planned_own"] == 0:
        return "no usable controls of its OWN (< 2 control samples) -> cross-study only -> quarantined"
    if have_cluster and p["after_bed"] == 0:
        return "lost at BED intersection (a control or condition group fell below 2 samples with a BED)"
    if have_cluster and p["written"] == 0:
        return "comparisons requested but AltAnalyze WROTE NONE (PSI run incomplete / crashed / killed)"
    if have_conc and p["gathered"] == 0:
        return "no dPSI file gathered (all quarantined as cross-study, or PSI output not reachable)"
    if have_conc and p["scored"] == 0:
        return "signatures too small to score (< 5 shared events with every subtype)"
    return "IN RANKING" if have_conc else ("comparisons written" if have_cluster else "planned (same-study)")


COLS = ("study", "verdict", "planned_conditions", "unreplicated_conditions", "control_samples", "planned_own",
        "planned_crossstudy", "after_bed", "written", "gathered", "quarantined", "scored")


def report(per, have_cluster, have_conc, out_tsv=None, detail_limit=60):
    rows = []
    for g, p in per.items():
        rows.append(dict(p, study=g, verdict=verdict(p, have_cluster, have_conc)))
    order = {"IN RANKING": 0}
    rows.sort(key=lambda r: (order.get(r["verdict"], 1), r["verdict"], r["study"]))
    n = lambda f: sum(1 for r in rows if f(r))
    lines = ["=== STUDY COVERAGE (which studies reached this ranking, and where the others dropped out) ==="]
    lines.append("  studies with a drug condition in the PSI plan .............. %d" % n(lambda r: r["planned_conditions"] > 0))
    lines.append("    ...with a SAME-study control comparison planned .......... %d   (%d cross-study only, %d un-replicated)"
                 % (n(lambda r: r["planned_own"] > 0), n(lambda r: r["planned_own"] == 0 and r["planned_crossstudy"] > 0),
                    n(lambda r: r["planned_conditions"] > 0 and r["planned_own"] == 0 and r["planned_crossstudy"] == 0)))
    if have_cluster:
        lines.append("    ...still comparable after STAR/BED ....................... %d" % n(lambda r: r["after_bed"] > 0))
        lines.append("    ...with dPSI comparison files WRITTEN by AltAnalyze ...... %d   (%d files)"
                     % (n(lambda r: r["written"] > 0), sum(r["written"] for r in rows)))
    if have_conc:
        lines.append("    ...gathered for concordance (study-matched) .............. %d   (%d contrasts quarantined as cross-study)"
                     % (n(lambda r: r["gathered"] > 0), sum(r["quarantined"] for r in rows)))
        lines.append("    ...scored against >= 1 cancer subtype ..................... %d" % n(lambda r: r["scored"] > 0))
    lost = [r for r in rows if r["verdict"] not in ("IN RANKING",)]
    reasons = {}
    for r in lost:
        reasons[r["verdict"]] = reasons.get(r["verdict"], 0) + 1
    if reasons:
        lines.append("  why studies were lost:")
        for why, k in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append("    %4d  %s" % (k, why))
    lines.append("  per study (first %d):" % detail_limit)
    lines.append("    %-11s %5s %5s %5s %6s %6s %6s %6s  %s" % ("study", "cond", "own", "xstd", "afterB", "writtn",
                                                                  "gathrd", "scored", "verdict"))
    for r in rows[:detail_limit]:
        lines.append("    %-11s %5d %5d %5d %6s %6s %6s %6s  %s" % (
            r["study"][:11], r["planned_conditions"], r["planned_own"], r["planned_crossstudy"],
            r["after_bed"] if have_cluster else "-", r["written"] if have_cluster else "-",
            r["gathered"] if have_conc else "-", r["scored"] if have_conc else "-", r["verdict"]))
    if len(rows) > detail_limit:
        lines.append("    ... (+%d more studies in the TSV)" % (len(rows) - detail_limit))
    if out_tsv:
        with io.open(out_tsv, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(u"\t".join(COLS) + u"\n")
            for r in rows:
                fh.write(u"\t".join(u"%s" % r[c] for c in COLS) + u"\n")
        lines.append("  per-study table -> %s" % out_tsv)
    return "\n".join(lines)


def main(argv=None):
    opts, _ = getopt.getopt(argv if argv is not None else sys.argv[1:], "",
                            ["root=", "psi-root=", "concordance-root=", "run-dir=", "out="])
    root = psi_root = conc_root = run_dir = out = None
    for o, a in opts:
        if o == "--root":
            root = a
        elif o == "--psi-root":
            psi_root = a
        elif o == "--concordance-root":
            conc_root = a
        elif o == "--run-dir":
            run_dir = a
        elif o == "--out":
            out = a
    if run_dir:                                   # PC run folder: the PSI bundle holds the plan
        psi_root = psi_root or os.path.join(run_dir, "runtable", "psi")
        conc_root = conc_root or "__none__"
    if not (root or psi_root):
        print(__doc__)
        return 2
    per, have_cluster, have_conc = study_funnel(root, psi_root, None if conc_root == "__none__" else conc_root)
    if not per:
        print("study_coverage: no sample_groups.tsv / PSI outputs found under %s" % (root or psi_root))
        return 1
    print(report(per, have_cluster, have_conc, out_tsv=out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
