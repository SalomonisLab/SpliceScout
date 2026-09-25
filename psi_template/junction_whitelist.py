#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""junction_whitelist.py -- JUNCTION PREVALENCE FILTER (manuscript Methods, "Junction prevalence filtering").

Artefactual junctions are library-PRIVATE while biological junctions RECUR, so the filter is defined on cohort
prevalence, not read depth. With n(j) = the number of libraries detecting junction j and N the cohort size:

    W(tau) = { j : n(j) >= tau * N }        (the threshold is round(tau * N): A549 N=6,313 -> 63, K562 2,483 -> 25)

Junction identity = '<chromosome>:<donor>-<acceptor>', taken from the BED name field AltAnalyze's
BAMtoJunctionBED writes ('JUNC<n>:<donor>-<acceptor>'; column 1 = chromosome), de-duplicated WITHIN each library
(coordinates are not unique within a BED: distinct read overhangs collapse onto one junction).

Without it the union of a large public cohort explodes (A549: 125M distinct junctions, only 3.2% annotated;
AltAnalyze ran 50 h / 509 GB and emitted no table); with tau = 1% the annotated fraction returned to 18.3% at
~6.6% per-library cost and the run completed. A single-pass in-memory tabulation (peak ~34 GB for N = 6,363).

Writes FILTERED COPIES (never touches the originals, which are usually symlinks into STAR_beds):
  <outdir>/<sample>__junction.bed       junction lines whose key is in W(tau) (+ the BED header lines)
  <outdir>/<sample>__intronJunction.bed symlinked through UNFILTERED (intron-retention reads use a different key)
  <outdir>/junction_whitelist.txt       the retained junction keys
  <outdir>/junction_prevalence_summary.tsv   N, tau, threshold, union, retained, per-library kept fraction
  <outdir>/.whitelist_done              "N=<N> tau=<tau> threshold=<t>"  (idempotency marker)
Exit codes: 0 = filtered dir ready; 3 = filter not needed (threshold <= 1 -- every junction already passes; the
caller keeps the unfiltered dir); anything else = failure (the caller falls back to the unfiltered BEDs).
Pure stdlib, python 2.7 AND 3.
"""
from __future__ import print_function, division
import getopt
import glob
import io
import os
import shutil
import sys

SUFFIX = "__junction.bed"
INTRON_SUFFIX = "__intronJunction.bed"


def junction_key(line):
    """'chr1\t...\t...\tJUNC12:14830-14970\t...' -> 'chr1:14830-14970' (None for headers/malformed lines)."""
    if not line or line.startswith(("track", "browser", "#")):
        return None
    parts = line.split("\t", 4)
    if len(parts) < 4:
        return None
    name = parts[3]
    i = name.find(":")
    coords = name[i + 1:] if i >= 0 else (parts[1] + "-" + parts[2])
    return parts[0] + ":" + coords


def threshold_for(tau, n):
    return max(1, int(round(tau * n)))


def main(argv=None):
    opts, _ = getopt.getopt(argv if argv is not None else sys.argv[1:], "", ["beddir=", "outdir=", "tau="])
    beddir = outdir = None
    tau = 0.01
    for o, a in opts:
        if o == "--beddir":
            beddir = a
        elif o == "--outdir":
            outdir = a
        elif o == "--tau":
            tau = float(a)
    if not beddir or not outdir:
        print("usage: junction_whitelist.py --beddir DIR --outdir DIR [--tau 0.01]", file=sys.stderr)
        return 2
    libs = sorted(glob.glob(os.path.join(beddir, "*" + SUFFIX)))
    n = len(libs)
    if n == 0:
        print("[whitelist] no *%s under %s" % (SUFFIX, beddir), file=sys.stderr)
        return 1
    thr = threshold_for(tau, n)
    tag = "N=%d tau=%g threshold=%d" % (n, tau, thr)
    marker = os.path.join(outdir, ".whitelist_done")
    if thr <= 1:
        print("[whitelist] %s -> every detected junction already passes (cohort too small to filter); skipping" % tag)
        return 3
    if os.path.isfile(marker):
        try:
            if io.open(marker, encoding="utf-8").read().strip() == tag:
                print("[whitelist] %s already built in %s -> reuse" % (tag, outdir))
                return 0
        except Exception:
            pass
    if not os.path.isdir(outdir):
        os.makedirs(outdir)
    for stale in glob.glob(os.path.join(outdir, "*.bed")) + [marker]:
        try:
            os.remove(stale)
        except OSError:
            pass

    # ---- pass 1: prevalence n(j) = #libraries detecting j (de-duplicated within each library) ----
    counts = {}
    per_lib_in = []
    for k, path in enumerate(libs):
        seen = set()
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key = junction_key(line)
                if key is not None:
                    seen.add(key)
        for key in seen:
            counts[key] = counts.get(key, 0) + 1
        per_lib_in.append(len(seen))
        if (k + 1) % 250 == 0:
            print("[whitelist] pass 1: %d/%d libraries, %d distinct junctions so far" % (k + 1, n, len(counts)))
            sys.stdout.flush()
    union = len(counts)
    white = set(key for key, c in counts.items() if c >= thr)
    counts = None                                    # free the tabulation before pass 2
    print("[whitelist] %s: union %d distinct junctions -> %d retained (%.2f%%)"
          % (tag, union, len(white), 100.0 * len(white) / max(1, union)))

    # ---- pass 2: rewrite each library keeping only whitelisted junctions (temp + rename: never half-written) ----
    per_lib = []
    for path, n_in in zip(libs, per_lib_in):
        dest = os.path.join(outdir, os.path.basename(path))
        tmp = dest + ".tmp"
        kept_keys = set()
        with io.open(path, encoding="utf-8", errors="replace") as fh, io.open(tmp, "w", encoding="utf-8",
                                                                             newline="\n") as out:
            for line in fh:
                key = junction_key(line)
                if key is None:
                    if line.startswith("track") or line.startswith("browser") or line.startswith("#"):
                        out.write(line)
                    continue
                if key in white:
                    out.write(line)
                    kept_keys.add(key)
        os.rename(tmp, dest)
        per_lib.append((os.path.basename(path)[:-len(SUFFIX)], n_in, len(kept_keys)))

    # intron-retention BEDs pass through unfiltered (their reads are keyed differently)
    for ipath in sorted(glob.glob(os.path.join(beddir, "*" + INTRON_SUFFIX))):
        link = os.path.join(outdir, os.path.basename(ipath))
        try:
            if os.path.lexists(link):
                os.remove(link)
            try:
                os.symlink(os.path.realpath(ipath), link)
            except (OSError, AttributeError, NotImplementedError):
                shutil.copy2(ipath, link)            # filesystem without symlinks: copy (same content)
        except (OSError, IOError) as e:
            print("[whitelist] could not link/copy %s (%s)" % (ipath, e), file=sys.stderr)
            return 1

    with io.open(os.path.join(outdir, "junction_whitelist.txt"), "w", encoding="utf-8", newline="\n") as fh:
        for key in sorted(white):
            fh.write(key + u"\n")
    fracs = sorted((k / float(i)) for _s, i, k in per_lib if i > 0)
    med = fracs[len(fracs) // 2] if fracs else 0.0
    with io.open(os.path.join(outdir, "junction_prevalence_summary.tsv"), "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write(u"# N_libraries\t%d\n# tau\t%g\n# threshold_libraries\t%d\n# union_junctions\t%d\n"
                 u"# retained_junctions\t%d\n# median_per_library_kept_fraction\t%.4f\n"
                 % (n, tau, thr, union, len(white), med))
        fh.write(u"sample\tjunctions_in\tjunctions_kept\tkept_fraction\n")
        for s, i, k in per_lib:
            fh.write(u"%s\t%d\t%d\t%.4f\n" % (s, i, k, (k / float(i)) if i else 0.0))
    with io.open(marker, "w", encoding="utf-8") as fh:
        fh.write(tag + u"\n")
    print("[whitelist] done: %d filtered libraries in %s (median per-library cost %.1f%%)"
          % (len(per_lib), outdir, 100.0 * (1.0 - med)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
