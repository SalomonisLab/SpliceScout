#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""rank_concordance.py -- turn splicingConcordance_advanced.py's concordance.txt into a human-readable,
RANKED reversal-candidate report. Pure text parsing (no AltAnalyze deps), python 2.7 AND 3 compatible.

concordance.txt cell format (from the scorer): "<cor>|<anti>:<N>" per (drug row x subtype column), or
"0.5|0.5" when the overlap is below the scorer's floor. cor in [0,1]: 1 = the drug MIMICS the subtype's
splicing (bad), 0 = the drug REVERSES it (therapeutic). N = overlapping splicing events.

SIGNIFICANCE (when the scorer's pair_stats.tsv sits beside concordance.txt): each pair is tested against its OWN
analytic null pi0 (the directional marginals within the shared events -- score_with_null.py), two-sided exact
binomial, Benjamini-Hochberg within this atlas, for pairs with N >= --min-overlap. Candidates = significant
reversals (C < pi0) / mimics (C > pi0). Without pair_stats.tsv (an older scorer) it falls back to the fixed
C < THRESHOLD / C > 0.70 cut-points.

The report is written so NO scored compound is invisible: the candidate tables show at most --per-drug rows per
drug (a handful of drugs with huge -- often batch-inflated -- signatures used to fill every row, which made a
run that scored 100+ compounds look as if it had scored 2-3), and a final section lists EVERY scored compound
once with its strongest reversal and mimic. all_scored_pairs.tsv (beside the summary) holds every pair.

Optional patient counts per subtype come from --counts (a subtype<TAB>patients tsv, e.g. the AML Leucegene
table) or --mergedresult (an OncoSplice cluster membership matrix; count = members per R*-V* cluster).
"""
from __future__ import print_function
import sys, re, os, getopt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # score_with_null.py ships beside this file
try:
    import score_with_null as swn
except Exception:                                                  # pragma: no cover -- degrade to legacy
    swn = None
try:
    import study_coverage                                          # per-STUDY coverage section (ships beside this)
except Exception:                                                  # pragma: no cover
    study_coverage = None


def parse_concordance(path):
    f = open(path)
    try:
        lines = f.read().splitlines()
    finally:
        f.close()
    if not lines:
        return [], []
    subs = lines[0].split('\t')[1:]
    rows = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        p = ln.split('\t')
        drug = p[0]
        for sub, cell in zip(subs, p[1:]):
            if ':' not in cell or '|' not in cell:
                continue
            try:
                cor = float(cell.split('|')[0])
                rest = cell.split('|')[1]
                anti = float(rest.split(':')[0])
                n = int(rest.split(':')[1])
            except (ValueError, IndexError):
                continue
            rows.append((drug, sub, cor, anti, n))
    return subs, rows


def parse_pair_stats(path):
    """(raw drug signature, raw subtype signature) -> (n, same, drug_inclusion, subtype_inclusion)."""
    out = {}
    if not path or not os.path.isfile(path):
        return out
    f = open(path)
    try:
        hdr = None
        for ln in f:
            t = ln.rstrip('\r\n').split('\t')
            if hdr is None:
                hdr = t
                continue
            if len(t) < 7:
                continue
            try:
                out[(t[0], t[1])] = (int(t[2]), int(t[3]), int(t[5]), int(t[6]))
            except ValueError:
                pass
    finally:
        f.close()
    return out


def clean_name(name, kind):
    s = name
    s = re.sub(r'^PSI\.', '', s)
    s = re.sub(r'^Leucegene\.', '', s)
    if kind == 'drug':
        s = re.sub(r'_vs_.*$', '', s)
    else:
        s = re.sub(r'_vs_Others$', '', s)
        s = re.sub(r'_vs_.*$', '', s)
    return s


_RV = re.compile(r'R\d+[-_]?V\d+', re.I)


def rv_token(s):
    m = _RV.search(s or '')
    if not m:
        return None
    return m.group(0).upper().replace('_', '-')


def load_counts_tsv(path):
    d = {}
    f = open(path)
    try:
        for i, ln in enumerate(f):
            t = ln.rstrip('\n').split('\t')
            if len(t) < 2:
                continue
            if i == 0 and not t[1].strip().replace('.', '', 1).isdigit():
                continue   # header row
            try:
                d[t[0].strip()] = int(float(t[1]))
            except ValueError:
                pass
    finally:
        f.close()
    return d


def load_counts_mergedresult(path):
    # rows = samples, cols = clusters (e.g. "LUAD_DT:R1-V23") with 0/1 (or float) membership.
    f = open(path)
    try:
        hdr = f.readline().rstrip('\n').split('\t')
        counts = [0] * len(hdr)
        for ln in f:
            t = ln.rstrip('\n').split('\t')
            for j in range(1, min(len(t), len(hdr))):
                try:
                    if float(t[j]) >= 0.5:
                        counts[j] += 1
                except ValueError:
                    pass
    finally:
        f.close()
    d = {}
    for j in range(1, len(hdr)):
        if hdr[j].strip():
            d[hdr[j].strip()] = counts[j]
    return d


def build_lookup(counts):
    ci = {}
    rv = {}
    for k, v in counts.items():
        ci[k.lower()] = v
        tok = rv_token(k)
        if tok:
            rv[tok] = v
    return ci, rv


def lookup_count(sub, counts, ci, rv):
    if sub in counts:
        return counts[sub]
    if sub.lower() in ci:
        return ci[sub.lower()]
    tok = rv_token(sub)
    if tok and tok in rv:
        return rv[tok]
    return None


def _num(v, fmt):
    return '-' if v is None else (fmt % v)


def fmt_table(title, items, with_stats):
    if with_stats:
        out = [title, "  %-30s %-34s %6s %6s %8s %9s %9s" % ("drug", "cancer subtype", "conc", "pi0", "N_events",
                                                               "q (FDR)", "patients")]
        for r in items:
            out.append("  %-30s %-34s %6.2f %6s %8d %9s %9s" % (
                r['drug'][:30], r['sub'][:34], r['cor'], _num(r['pi0'], '%.2f'), r['n'], _num(r['q'], '%.2g'),
                ('-' if r['pt'] is None else str(r['pt']))))
    else:
        out = [title, "  %-30s %-34s %6s %8s %9s" % ("drug", "cancer subtype", "conc", "N_events", "patients")]
        for r in items:
            out.append("  %-30s %-34s %6.2f %8d %9s" % (r['drug'][:30], r['sub'][:34], r['cor'], r['n'],
                                                        ('-' if r['pt'] is None else str(r['pt']))))
    if len(items) == 0:
        out.append("  (none)")
    return "\n".join(out)


def cap_per_drug(items, k):
    """Keep at most k rows per drug (items already in priority order). Returns (kept, n_hidden)."""
    seen, kept, hidden = {}, [], 0
    for r in items:
        c = seen.get(r['drug'], 0)
        if c < k:
            kept.append(r)
            seen[r['drug']] = c + 1
        else:
            hidden += 1
    return kept, hidden


def compound_table(enriched, with_stats, threshold):
    """One line per drug contrast: every scored compound, visible."""
    per = {}
    for r in enriched:
        per.setdefault(r['drug'], []).append(r)
    lines = ["=== EVERY SCORED COMPOUND (one line each; strongest reversal = lowest C, strongest mimic = highest C) ===",
             "  %-30s %9s %5s %8s   %-40s   %-40s" % ("drug", "study", "n_sub", "median_C",
                                                       "strongest reversal: subtype (C, q)",
                                                       "strongest mimic: subtype (C, q)")]
    order = []
    for d, rs in per.items():
        rev = min(rs, key=lambda r: r['cor'])
        mim = max(rs, key=lambda r: r['cor'])
        cs = sorted(r['cor'] for r in rs)
        med = cs[len(cs) // 2] if len(cs) % 2 else 0.5 * (cs[len(cs) // 2 - 1] + cs[len(cs) // 2])
        sig_rev = sum(1 for r in rs if r.get('sig') and r['cor'] < (r['pi0'] if r['pi0'] is not None else 0.5))
        order.append((-sig_rev, rev['cor'], d, rs, rev, mim, med))
    order.sort(key=lambda t: (t[0], t[1], t[2]))
    for _neg, _c, d, rs, rev, mim, med in order:
        m = re.search(r'GSE\d+', d)
        def cell(r):
            q = ('q=%.2g' % r['q']) if (with_stats and r.get('q') is not None) else ''
            return ('%s (%.2f%s)' % (r['sub'], r['cor'], (', ' + q) if q else ''))[:40]
        lines.append("  %-30s %9s %5d %8.2f   %-40s   %-40s" % (d[:30], m.group(0) if m else '-', len(rs), med,
                                                                cell(rev), cell(mim)))
    if not order:
        lines.append("  (none)")
    return "\n".join(lines), len(order)


def main():
    opts, _ = getopt.getopt(sys.argv[1:], '', ['concordance=', 'atlas=', 'threshold=', 'out=',
                                               'counts=', 'mergedresult=', 'pair-stats=', 'min-overlap=',
                                               'alpha=', 'per-drug=', 'psi-root=', 'concordance-root='])
    concordance = atlas = out = pair_stats = psi_root = conc_root = None
    threshold = 0.3
    min_overlap, alpha, per_drug = 25, 0.05, 3
    counts_tsv = merged = None
    for o, a in opts:
        if o == '--concordance':
            concordance = a
        elif o == '--atlas':
            atlas = a
        elif o == '--threshold':
            threshold = float(a)
        elif o == '--out':
            out = a
        elif o == '--counts':
            counts_tsv = a
        elif o == '--mergedresult':
            merged = a
        elif o == '--pair-stats':
            pair_stats = a
        elif o == '--min-overlap':
            min_overlap = int(a)
        elif o == '--alpha':
            alpha = float(a)
        elif o == '--per-drug':
            per_drug = max(1, int(a))
        elif o == '--psi-root':
            psi_root = a
        elif o == '--concordance-root':
            conc_root = a
    if not concordance or not out:
        print("usage: rank_concordance.py --concordance F --out F [--atlas NAME --threshold 0.3 "
              "--counts tsv | --mergedresult matrix] [--pair-stats F --min-overlap 25 --alpha 0.05 --per-drug 3]")
        sys.exit(2)
    atlas = atlas or 'atlas'
    if pair_stats is None:                     # default: the scorer's pair_stats.tsv beside concordance.txt
        pair_stats = os.path.join(os.path.dirname(os.path.abspath(concordance)), 'pair_stats.tsv')

    counts = {}
    csrc = "none"
    try:
        if counts_tsv:
            counts = load_counts_tsv(counts_tsv); csrc = "tsv:%s" % counts_tsv
        elif merged:
            counts = load_counts_mergedresult(merged); csrc = "mergedresult:%s" % merged
    except Exception as e:
        sys.stderr.write("rank_concordance: could not load counts (%s) -> ranking without patient counts\n" % e)
        counts = {}
    ci, rv = build_lookup(counts)

    _subs, rows = parse_concordance(concordance)
    stats = parse_pair_stats(pair_stats) if swn is not None else {}
    with_stats = bool(stats)
    enriched = []
    for drug, sub, cor, anti, n in rows:
        sn = clean_name(sub, 'subtype')
        r = {'raw_drug': drug, 'raw_sub': sub, 'drug': clean_name(drug, 'drug'), 'sub': sn, 'cor': cor, 'n': n,
             'pt': lookup_count(sn, counts, ci, rv) if counts else None, 'pi0': None, 'p': None, 'q': None,
             'sig': False}
        st = stats.get((drug, sub))
        if st and st[0] > 0:
            nn, same, inc_d, inc_s = st
            r['pi0'] = swn.null_pi0(inc_d / float(nn), inc_s / float(nn))
            r['p'] = swn.binom_two_sided_p(same, nn, r['pi0'])
        enriched.append(r)

    if with_stats:
        testable = [r for r in enriched if r['p'] is not None and r['n'] >= min_overlap]
        for r, q in zip(testable, swn.bh_qvalues([r['p'] for r in testable])):
            r['q'] = q
            r['sig'] = q < alpha
        reversal = sorted([r for r in enriched if r['sig'] and r['cor'] < r['pi0']],
                          key=lambda r: (r['q'], r['cor'], -r['n']))
        mimic = sorted([r for r in enriched if r['sig'] and r['cor'] > r['pi0']],
                       key=lambda r: (r['q'], -r['cor'], -r['n']))
        rule_rev = "significant REVERSAL: C below the pair's own null pi0, BH q < %g, N >= %d" % (alpha, min_overlap)
        rule_mim = "significant MIMIC / AVOID: C above pi0, BH q < %g, N >= %d" % (alpha, min_overlap)
    else:
        reversal = sorted([r for r in enriched if r['cor'] < threshold], key=lambda r: (-r['n'], -(r['pt'] or 0)))
        mimic = sorted([r for r in enriched if r['cor'] > 0.7], key=lambda r: (-r['n'], -(r['pt'] or 0)))
        rule_rev = "REVERSAL CANDIDATES (legacy cut-point: concordance < %.2f; no pair_stats.tsv -> no null test)" % threshold
        rule_mim = "MIMIC / AVOID (legacy cut-point: concordance > 0.70)"
    rev_shown, rev_hidden = cap_per_drug(reversal, per_drug)
    mim_shown, mim_hidden = cap_per_drug(mimic, per_drug)

    # ---- COMPLETE long-form export (every scored pair, never silently invisible) ---------------
    all_tsv = os.path.join(os.path.dirname(os.path.abspath(out)), 'all_scored_pairs.tsv')
    n_all = 0
    try:
        af = open(all_tsv, 'w')
        try:
            af.write("atlas\tdrug\tsubtype\tconcordance\tn_overlap\tpatients\tdirection\tnull_pi0\tp_value\tq_atlas\n")
            for r in sorted(enriched, key=lambda r: (r['cor'], -r['n'])):
                ref = r['pi0'] if r['pi0'] is not None else 0.5
                d = 'reversal' if r['cor'] < ref else ('mimic' if r['cor'] > ref else 'null')
                af.write("%s\t%s\t%s\t%.4f\t%d\t%s\t%s\t%s\t%s\t%s\n" % (
                    atlas, r['drug'], r['sub'], r['cor'], r['n'], ('' if r['pt'] is None else r['pt']), d,
                    ('' if r['pi0'] is None else '%.4f' % r['pi0']), ('' if r['p'] is None else '%.4g' % r['p']),
                    ('' if r['q'] is None else '%.4g' % r['q'])))
                n_all += 1
        finally:
            af.close()
    except Exception as e:
        sys.stderr.write("rank_concordance: could not write %s (%s)\n" % (all_tsv, e))
    # ---- the ranked tables as TSV (every row, no per-drug cap): the .txt summary is aligned for reading, not
    # for spreadsheets -- this is what to hand on / open in Excel
    sig_tsv = os.path.join(os.path.dirname(os.path.abspath(out)), 'significant_pairs.tsv')
    try:
        sf = open(sig_tsv, 'w')
        try:
            sf.write("atlas\tcall\tdrug\tsubtype\tconcordance\tn_overlap\tpatients\tnull_pi0\tp_value\tq_atlas\n")
            for call, items in (('reversal', reversal), ('mimic', mimic)):
                for r in items:
                    sf.write("%s\t%s\t%s\t%s\t%.4f\t%d\t%s\t%s\t%s\t%s\n" % (
                        atlas, call, r['drug'], r['sub'], r['cor'], r['n'], ('' if r['pt'] is None else r['pt']),
                        ('' if r['pi0'] is None else '%.4f' % r['pi0']), ('' if r['p'] is None else '%.4g' % r['p']),
                        ('' if r['q'] is None else '%.4g' % r['q'])))
        finally:
            sf.close()
    except Exception as e:
        sys.stderr.write("rank_concordance: could not write %s (%s)\n" % (sig_tsv, e))

    comp_txt, n_drugs = compound_table(enriched, with_stats, threshold)
    studies = set()
    for r in enriched:
        m = re.search(r'GSE\d+', r['drug'])
        if m:
            studies.add(m.group(0))
    n_subs = len(set(r['sub'] for r in enriched))
    legacy_below = len([r for r in enriched if r['cor'] < threshold])
    legacy_above = len([r for r in enriched if r['cor'] > 0.7])
    head = (
        "%s concordance ranking (drug splicing signature vs cancer subtypes)\n" % atlas +
        "concordance C: 1 = drug MIMICS the subtype (bad), 0 = drug REVERSES it (therapeutic).\n" +
        ("Significance: exact binomial test of C against each pair's analytic null pi0 (the two signatures'\n"
         "inclusion marginals within the shared events), Benjamini-Hochberg within this atlas.\n" if with_stats else
         "No pair_stats.tsv beside concordance.txt (older scorer) -> legacy fixed cut-points, no significance test.\n") +
        "N_events = overlapping splicing events; patients = subtype cohort size (%s).\n\n" % csrc +
        "SCORED: %d drug x subtype pairs | %d distinct drug contrasts from %d studies | %d subtypes\n"
        % (len(enriched), n_drugs, len(studies), n_subs) +
        ("SIGNIFICANT (q < %g, N >= %d): %d reversal, %d mimic   [legacy cut-points: %d pairs < %.2f, %d > 0.70]\n"
         % (alpha, min_overlap, len(reversal), len(mimic), legacy_below, threshold, legacy_above) if with_stats else
         "CANDIDATES: %d reversal (< %.2f), %d mimic (> 0.70)\n" % (len(reversal), threshold, len(mimic))) +
        "Tables list at most %d subtypes per drug; EVERY scored compound is listed at the end; every pair is in\n"
        "all_scored_pairs.tsv (%d rows).\n\n" % (per_drug, n_all))
    body = (
        head +
        fmt_table("=== %s ===" % rule_rev, rev_shown, with_stats) +
        ("\n  (+%d more significant rows for drugs already listed %d times -> all_scored_pairs.tsv)"
         % (rev_hidden, per_drug) if rev_hidden else "") +
        "\n\n" +
        fmt_table("=== %s ===" % rule_mim, mim_shown, with_stats) +
        ("\n  (+%d more rows for drugs already listed %d times -> all_scored_pairs.tsv)" % (mim_hidden, per_drug)
         if mim_hidden else "") +
        "\n\n" + comp_txt + "\n"
    )
    # which STUDIES reached this ranking, and the step at which every other study dropped out (PSI plan ->
    # BED -> AltAnalyze-written comparisons -> study-matched gather -> scored). Makes "all compounds come from 2
    # studies" explainable from the summary itself instead of looking like missing data.
    if study_coverage is not None and (psi_root or conc_root):
        try:
            per, hc, hk = study_coverage.study_funnel(psi_root=psi_root, conc_root=conc_root,
                                                      concordance_files=[concordance])
            if per:
                body += "\n" + study_coverage.report(per, hc, hk, detail_limit=40) + "\n"
        except Exception as e:
            sys.stderr.write("rank_concordance: study coverage skipped (%s)\n" % e)
    try:
        f = open(out, 'w')
        try:
            f.write(body)
        finally:
            f.close()
    except Exception as e:
        # was uncaught -> a write failure (unwritable dir / disk full) crashed the ranker with a bare
        # traceback and left no ranked output + no clear reason. Log distinctly and fail visibly instead.
        sys.stderr.write("rank_concordance: FAILED to write ranked output %s (%s)\n" % (out, e))
        sys.exit(1)
    print("rank_concordance: %d pairs, %d compounds (%d reversal, %d mimic candidates%s) -> %s"
          % (len(enriched), n_drugs, len(reversal), len(mimic), ", analytic null" if with_stats else ", legacy cut-points",
             out))


if __name__ == '__main__':
    main()
