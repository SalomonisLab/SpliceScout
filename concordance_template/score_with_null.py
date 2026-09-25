#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""score_with_null.py -- the ANALYTIC NULL for splicing concordance (manuscript Methods, "Null model").

Concordance C(P,D) = |{e in E : dir_P(e) = dir_D(e)}| / |E| over the events E shared by a drug signature P and a
disease signature D. Under independence of direction, with p_P / p_D the inclusion fractions of P / D restricted to
E, each shared event agrees with probability
    pi0 = p_P * p_D + (1 - p_P) * (1 - p_D)
so the number of agreements is Binomial(|E|, pi0). Significance = two-sided exact binomial test of the observed
agreement count against pi0, Benjamini-Hochberg across all scored pairs. This conditions on BOTH the overlap size
and the directional marginals and supersedes the fixed 0.30/0.70 cut-points (kept only as descriptive labels).
Optionally, a one-sided Mann-Whitney rank-sum (normal approximation) tests whether a named agent set (e.g.
indisulam,gsk591) is non-randomly positioned in each subtype's ranking (rank 1 = strongest mimic).

Input : one or more pair_stats.tsv files written by splicingConcordance_advanced.py (one per atlas; the atlas name
        is the file's parent directory, i.e. results/<atlas>/pair_stats.tsv). Columns:
        drug_signature, subtype_signature, n_overlap, same, opposite, drug_inclusion, subtype_inclusion
Output: --out            scored_pairs_with_null.tsv  (one row per scored pair; the manuscript figure scripts read it)
        --by-compound    concordance_by_compound.tsv (one row per drug contrast: every scored compound, visible)
        --enrichment-out mechanism_enrichment.tsv    (only with --agents)

Pure stdlib, python 2.7 AND 3 (the concordance job runs under the cluster's python/2.7.5). Also importable: the
per-atlas ranker (rank_concordance.py) uses binom_two_sided_p / null_pi0 / bh_qvalues from here.
"""
from __future__ import print_function, division
import io
import math
import os
import re
import sys
import getopt


# ------------------------------------------------------------------ statistics
def null_pi0(p_drug, p_sub):
    """P(direction agreement) under independence, given the two inclusion fractions within E."""
    return p_drug * p_sub + (1.0 - p_drug) * (1.0 - p_sub)


def _log_pmf(k, n, p):
    if p <= 0.0:
        return 0.0 if k == 0 else float("-inf")
    if p >= 1.0:
        return 0.0 if k == n else float("-inf")
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
            + k * math.log(p) + (n - k) * math.log(1.0 - p))


def binom_two_sided_p(k, n, p):
    """Exact two-sided binomial p-value (the 'minlike' definition, as R's binom.test / scipy's binomtest):
    the total probability of every outcome no more likely than the observed k. O(|k - mode|) terms via the
    pmf recurrence, so it stays fast for overlaps of thousands of events."""
    if n <= 0:
        return 1.0
    if p <= 0.0:
        return 1.0 if k == 0 else 0.0
    if p >= 1.0:
        return 1.0 if k == n else 0.0
    lp_obs = _log_pmf(k, n, p)
    thr = lp_obs + 1e-7 * abs(lp_obs) + 1e-12          # relative tolerance, as the reference implementations
    mode = int(math.floor((n + 1) * p))
    mode = min(max(mode, 0), n)
    # walk outward from the mode while the pmf is MORE likely than observed: that central block is excluded
    lp_mode = _log_pmf(mode, n, p)
    if lp_mode <= thr:                                   # observed IS (tied with) the most likely outcome
        return 1.0
    lratio = math.log(p / (1.0 - p))

    def up(i, lp):                                       # log pmf(i+1) from log pmf(i)
        return lp + math.log((n - i) / (i + 1.0)) + lratio

    def down(i, lp):                                     # log pmf(i-1) from log pmf(i)
        return lp + math.log(i / (n - i + 1.0)) - lratio

    # the pmf is unimodal: {k : pmf(k) > pmf(observed)} is one interval around the mode. Find its two edges.
    hi, lp_hi = mode, lp_mode
    while hi < n and up(hi, lp_hi) > thr:
        lp_hi = up(hi, lp_hi)
        hi += 1
    lo, lp_lo = mode, lp_mode
    while lo > 0 and down(lo, lp_lo) > thr:
        lp_lo = down(lo, lp_lo)
        lo -= 1

    # p = the mass OUTSIDE that interval, summed directly from each edge outward in log space. (It used to be
    # 1 - central mass, which cannot go below ~1e-16: every strong pair printed p = 0 and q = 0.)
    def tail(i, lp, step):                               # log of sum pmf(i), pmf(i+step), ... to the end
        lp0, total = lp, 0.0
        while True:
            term = math.exp(lp - lp0)
            total += term
            if term < 1e-17 * total or not 0 <= i + step <= n:
                break
            lp = up(i, lp) if step > 0 else down(i, lp)
            i += step
        return lp0 + math.log(total)

    logs = [t for t in (tail(hi + 1, up(hi, lp_hi), 1) if hi < n else None,
                        tail(lo - 1, down(lo, lp_lo), -1) if lo > 0 else None) if t is not None]
    if not logs:
        return 1.0
    m = max(logs)
    return max(5e-324, min(1.0, math.exp(m + math.log(sum(math.exp(x - m) for x in logs)))))


def bh_qvalues(pvals):
    """Benjamini-Hochberg q-values (monotone), same order as the input."""
    m = len(pvals)
    if not m:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvals[i] * m / float(rank))
        q[i] = running
    return q


def mann_whitney(ranks, n_total):
    """One-sided rank-sum test of a set of `ranks` (1..n_total, rank 1 = strongest mimic) vs the rest, normal
    approximation without continuity correction (the manuscript's Table 4). Returns (z, p_one_sided) where z < 0
    = concentrated at the MIMIC pole and p = P(Z beyond |z| in the observed direction)."""
    n1 = len(ranks)
    n2 = n_total - n1
    if n1 == 0 or n2 <= 0:
        return 0.0, 1.0
    r1 = float(sum(ranks))
    mu = n1 * (n_total + 1) / 2.0
    sd = math.sqrt(n1 * n2 * (n_total + 1) / 12.0)
    if sd == 0:
        return 0.0, 1.0
    z = (r1 - mu) / sd
    return z, 0.5 * math.erfc(abs(z) / math.sqrt(2.0))


# ------------------------------------------------------------------ names
def clean_signature(name):
    """'PSI.U2AF1-S34(R2-C26)_vs_Others' -> 'U2AF1-S34(R2-C26)_vs_Others' (the figure scripts' subtype key)."""
    s = re.sub(r"^PSI\.", "", name or "")
    return re.sub(r"^Leucegene\.", "", s)


def drug_of(contrast):
    """'GSE123.Indisulam_1uM_vs_GSE123.control' -> 'GSE123.Indisulam_1uM' (the condition = the drug arm)."""
    return re.sub(r"_vs_.*$", "", clean_signature(contrast))


def study_of(contrast):
    m = re.search(r"GSE\d+", contrast or "")
    return m.group(0) if m else ""


# ------------------------------------------------------------------ io
PAIR_COLS = ("drug_signature", "subtype_signature", "n_overlap", "same", "opposite",
             "drug_inclusion", "subtype_inclusion")


def read_pair_stats(path, atlas=None):
    """-> list of dicts with the derived null columns (no FDR yet)."""
    atlas = atlas or os.path.basename(os.path.dirname(os.path.abspath(path))) or "atlas"
    out = []
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        hdr = None
        for ln in fh:
            t = ln.rstrip("\r\n").split("\t")
            if hdr is None:
                hdr = t
                continue
            if len(t) < len(hdr):
                continue
            r = dict(zip(hdr, t))
            try:
                n = int(r["n_overlap"]); same = int(r["same"]); opp = int(r["opposite"])
                inc_d = int(r["drug_inclusion"]); inc_s = int(r["subtype_inclusion"])
            except (KeyError, ValueError):
                continue
            if n <= 0:
                continue
            p_d, p_s = inc_d / float(n), inc_s / float(n)
            pi0 = null_pi0(p_d, p_s)
            out.append({"atlas": atlas, "contrast": clean_signature(r["drug_signature"]),
                        "drug": drug_of(r["drug_signature"]), "study": study_of(r["drug_signature"]),
                        "subtype": clean_signature(r["subtype_signature"]),
                        "concordance": same / float(n), "n_overlap": n, "same": same, "opposite": opp,
                        "incl_frac_drug": p_d, "incl_frac_subtype": p_s, "null_pi0": pi0,
                        "p_value": binom_two_sided_p(same, n, pi0)})
    return out


def score(rows, min_overlap=25, alpha=0.05):
    """Apply the overlap floor, then BH across ALL remaining pairs (all atlases together, as in the manuscript).
    Adds FDR_q, direction (reversal/mimic relative to the pair's OWN null) and significant."""
    kept = [r for r in rows if r["n_overlap"] >= min_overlap]
    qs = bh_qvalues([r["p_value"] for r in kept])
    for r, q in zip(kept, qs):
        r["FDR_q"] = q
        r["direction"] = "reversal" if r["concordance"] < r["null_pi0"] else (
            "mimic" if r["concordance"] > r["null_pi0"] else "null")
        r["significant"] = "yes" if q < alpha else "no"
    return kept


try:                                                      # python 2 (the cluster's python/2.7.5)
    _TEXT = unicode                                       # noqa: F821
except NameError:
    _TEXT = str


def _fmt(v):
    if isinstance(v, float):
        return u"%.6g" % v
    # names are unicode (read via io.open); py2's str() would raise on a non-ASCII one ('TGF-β', 'µM')
    return v if isinstance(v, _TEXT) else _TEXT(v)


OUT_COLS = ("atlas", "subtype", "contrast", "drug", "study", "concordance", "n_overlap", "same", "opposite",
            "incl_frac_drug", "incl_frac_subtype", "null_pi0", "p_value", "FDR_q", "direction", "significant")


def write_tsv(path, rows, cols):
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(u"\t".join(cols) + u"\n")
        for r in rows:
            fh.write(u"\t".join(_fmt(r.get(c, "")) for c in cols) + u"\n")


def by_compound(rows):
    """One row per drug contrast: every scored compound with its strongest significant reversal / mimic."""
    per = {}
    for r in rows:
        per.setdefault((r["contrast"]), []).append(r)
    out = []
    for contrast, rs in per.items():
        rev = [r for r in rs if r["direction"] == "reversal"]
        mim = [r for r in rs if r["direction"] == "mimic"]
        best_rev = min(rev, key=lambda r: (r["FDR_q"], r["concordance"])) if rev else None
        best_mim = min(mim, key=lambda r: (r["FDR_q"], -r["concordance"])) if mim else None
        cs = sorted(r["concordance"] for r in rs)
        med = cs[len(cs) // 2] if len(cs) % 2 else 0.5 * (cs[len(cs) // 2 - 1] + cs[len(cs) // 2])
        out.append({
            "contrast": contrast, "drug": rs[0]["drug"], "study": rs[0]["study"],
            "atlases": ",".join(sorted(set(r["atlas"] for r in rs))),
            "n_subtypes_scored": len(rs), "median_concordance": med,
            "n_sig_reversal": sum(1 for r in rev if r["significant"] == "yes"),
            "n_sig_mimic": sum(1 for r in mim if r["significant"] == "yes"),
            "best_reversal_subtype": (best_rev["atlas"] + ":" + best_rev["subtype"]) if best_rev else "",
            "best_reversal_C": best_rev["concordance"] if best_rev else "",
            "best_reversal_q": best_rev["FDR_q"] if best_rev else "",
            "best_mimic_subtype": (best_mim["atlas"] + ":" + best_mim["subtype"]) if best_mim else "",
            "best_mimic_C": best_mim["concordance"] if best_mim else "",
            "best_mimic_q": best_mim["FDR_q"] if best_mim else ""})
    out.sort(key=lambda r: (-(r["n_sig_reversal"]), r["best_reversal_q"] if r["best_reversal_q"] != "" else 2.0,
                            r["contrast"]))
    return out


BYC_COLS = ("contrast", "drug", "study", "atlases", "n_subtypes_scored", "median_concordance", "n_sig_reversal",
            "n_sig_mimic", "best_reversal_subtype", "best_reversal_C", "best_reversal_q", "best_mimic_subtype",
            "best_mimic_C", "best_mimic_q")


def enrichment(rows, agents):
    """Mann-Whitney per (atlas, subtype): are contrasts naming one of `agents` non-randomly placed in the
    concordance ranking (rank 1 = highest C = strongest mimic)?"""
    pat = re.compile("|".join(re.escape(a) for a in agents if a), re.I)
    groups = {}
    for r in rows:
        groups.setdefault((r["atlas"], r["subtype"]), []).append(r)
    out = []
    for (atlas, sub), rs in sorted(groups.items()):
        rs = sorted(rs, key=lambda r: -r["concordance"])
        ranks = [i + 1 for i, r in enumerate(rs) if pat.search(r["contrast"])]
        if not ranks:
            continue
        z, p = mann_whitney(ranks, len(rs))
        out.append({"atlas": atlas, "subtype": sub, "n_agent": len(ranks), "n_scored": len(rs),
                    "ranks": ",".join(str(x) for x in ranks), "z": z, "p_one_sided": p,
                    "pole": "mimic" if z < 0 else "reversal"})
    return out


ENR_COLS = ("atlas", "subtype", "n_agent", "n_scored", "ranks", "z", "p_one_sided", "pole")


def read_universe(pair_stats_path):
    """(drug signatures, subtype signatures) the scorer compared, from concordance.txt beside pair_stats.tsv: its
    header lists EVERY subtype and it has one row per drug. pair_stats.tsv only holds pairs with >= 1 shared event,
    so without this a subtype a drug never touches is simply absent -- indistinguishable from a dropped one."""
    conc = os.path.join(os.path.dirname(os.path.abspath(pair_stats_path)), "concordance.txt")
    drugs, subs = [], []
    if not os.path.isfile(conc):
        return drugs, subs
    with io.open(conc, encoding="utf-8", errors="replace") as fh:
        for i, ln in enumerate(fh):
            t = ln.rstrip("\r\n").split("\t")
            if i == 0:
                subs = [s for s in t[1:] if s]
            elif t and t[0]:
                drugs.append(t[0])
    return drugs, subs


def complete_table(files, rows, min_overlap):
    """EVERY drug x subtype pair of every atlas, with a plain-language `result`: significant reversal / mimic, not
    significant, below the overlap floor (not tested), or no shared events. `rows` = read_pair_stats output after
    score() (the scored ones carry FDR_q)."""
    by = dict(((r["atlas"], r["contrast"], r["subtype"]), r) for r in rows)
    out = []
    for f in files:
        atlas = os.path.basename(os.path.dirname(os.path.abspath(f))) or "atlas"
        drugs, subs = read_universe(f)
        if not drugs or not subs:                          # no concordance.txt: at least every pair that was seen
            seen = [r for r in rows if r["atlas"] == atlas]
            drugs = sorted(set(r["contrast"] for r in seen))
            subs = sorted(set(r["subtype"] for r in seen))
        for d in drugs:
            c = clean_signature(d)
            for s in subs:
                sc = clean_signature(s)
                r = by.get((atlas, c, sc))
                if r is None:
                    out.append({"atlas": atlas, "contrast": c, "drug": drug_of(d), "study": study_of(d),
                                "subtype": sc, "n_overlap": 0, "same": 0, "opposite": 0,
                                "result": "no shared events"})
                elif "FDR_q" in r:
                    res = (("significant " + r["direction"]) if r["significant"] == "yes"
                           else ("not significant (" + r["direction"] + ")"))
                    out.append(dict(r, result=res))
                else:
                    out.append(dict(r, result="below overlap floor (n < %d): not tested" % min_overlap))
    out.sort(key=lambda r: (r["atlas"], r["contrast"], r["subtype"]))
    return out


COMPLETE_COLS = ("atlas", "contrast", "drug", "study", "subtype", "n_overlap", "same", "opposite", "concordance",
                 "incl_frac_drug", "incl_frac_subtype", "null_pi0", "p_value", "FDR_q", "result")


def main(argv=None):
    opts, files = getopt.getopt(argv if argv is not None else sys.argv[1:], "",
                                ["out=", "by-compound=", "min-overlap=", "alpha=", "agents=", "enrichment-out=",
                                 "complete="])
    out = byc = enr_out = complete = None
    min_overlap, alpha, agents = 25, 0.05, []
    for o, a in opts:
        if o == "--out":
            out = a
        elif o == "--by-compound":
            byc = a
        elif o == "--min-overlap":
            min_overlap = int(a)
        elif o == "--alpha":
            alpha = float(a)
        elif o == "--agents":
            agents = [x.strip() for x in a.split(",") if x.strip()]
        elif o == "--enrichment-out":
            enr_out = a
        elif o == "--complete":
            complete = a
    if not out or not files:
        print("usage: score_with_null.py --out scored_pairs_with_null.tsv [--by-compound F] [--complete F] "
              "[--min-overlap 25] [--alpha 0.05] [--agents indisulam,gsk591 --enrichment-out F] "
              "results/*/pair_stats.tsv")
        return 2
    rows = []
    for f in files:
        if os.path.isfile(f):
            rows.extend(read_pair_stats(f))
    scored = score(rows, min_overlap=min_overlap, alpha=alpha)
    scored.sort(key=lambda r: (r["atlas"], r["subtype"], r["FDR_q"], r["concordance"]))
    write_tsv(out, scored, OUT_COLS)
    n_sig = sum(1 for r in scored if r["significant"] == "yes")
    n_rev = sum(1 for r in scored if r["significant"] == "yes" and r["direction"] == "reversal")
    mean_pi0 = (sum(r["null_pi0"] for r in scored) / len(scored)) if scored else float("nan")
    print("score_with_null: %d pairs read, %d with n_overlap >= %d scored; mean pi0 = %.3f; %d significant at "
          "FDR q < %g (%d reversal, %d mimic) -> %s"
          % (len(rows), len(scored), min_overlap, mean_pi0, n_sig, alpha, n_rev, n_sig - n_rev, out))
    if byc:
        comp = by_compound(scored)
        write_tsv(byc, comp, BYC_COLS)
        print("score_with_null: %d distinct drug contrasts scored -> %s" % (len(comp), byc))
    if complete:
        full = complete_table([f for f in files if os.path.isfile(f)], rows, min_overlap)
        write_tsv(complete, full, COMPLETE_COLS)
        print("score_with_null: complete drug x subtype table: %d pairs (%d with no shared event, %d below the "
              "overlap floor) -> %s" % (len(full), sum(1 for r in full if r["n_overlap"] == 0),
                                        sum(1 for r in full if r["result"].startswith("below")), complete))
    if agents and enr_out:
        enr = enrichment(scored, agents)
        write_tsv(enr_out, enr, ENR_COLS)
        print("score_with_null: agent-set enrichment for %s in %d subtype(s) -> %s" % (",".join(agents), len(enr), enr_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
