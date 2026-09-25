#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_sample_list.py -- build a STAR sample list (3-column TSV) from a FASTQ tree.

    <SampleLabel>\t<R1[,R1...]>\t<R2[,R2...] | NA>

* Scans --input-dir RECURSIVELY for *.fastq.gz / *.fq.gz.
* Detects the mate convention PER RUN: <run>_R1/<run>_R2, <run>_1/<run>_2, or a
  bare <run>.fastq.gz (single-end). A study that mixes paired+single runs is fine.
* If --runtable (a CSV/TSV with Run + BioSample [+ LibraryLayout] columns) is
  given, labels by BioSample and MERGES all runs of one BioSample onto a single
  row (FASTQs comma-joined in run order) -> one BAM per biological sample. Runs
  not found in the runtable are still emitted, labelled by their own run
  accession, and listed in <out>.unmapped. Without --runtable, every run is its
  own sample (no merge).
* Paths in the output are ABSOLUTE. Single-end rows get "NA" in column 3.
* <run>_3 / _4 ... files (fasterq-dump --split-files writes one file per READ, so a run with index/barcode reads
  gets _3, _4) are EXTRA reads of <run>, not runs of their own: before, "<run>_3" became a separate single-end
  "sample". They are left out of the list and reported in <out>.extra.
* Side reports (nothing is ever silently dropped):
    <out>.orphans  -- a run with a mate missing on disk (R1 w/o R2 etc.)
    <out>.unmapped -- a run on disk with no BioSample in the runtable
    <out>.mixed    -- a run with conflicting roles, or a BioSample mixing layouts
    <out>.extra    -- extra read files (<run>_3, _4 ...) left out of the alignment

Pure Python 3 standard library (runs on the cluster's python3 3.6.8 -- no pandas,
no openpyxl). xlsx is intentionally unsupported: export it to CSV first.
"""
import argparse
import csv
import os
import re
import sys
from collections import OrderedDict

# longest / most-specific suffix FIRST so _R1 / _1 win over a bare .fastq.gz
SUFFIXES = [
    ("_R1.fastq.gz", "R1"), ("_R2.fastq.gz", "R2"),
    ("_R1.fq.gz", "R1"),    ("_R2.fq.gz", "R2"),
    ("_1.fastq.gz", "R1"),  ("_2.fastq.gz", "R2"),
    ("_1.fq.gz", "R1"),     ("_2.fq.gz", "R2"),
    (".fastq.gz", "SE"),    (".fq.gz", "SE"),
]

RUN_ALIASES = {"run", "run_accession", "accession", "srr", "run accession"}
BS_ALIASES = {"biosample", "bio_sample", "biosample accession", "biosample_accession",
              "sample", "sample_name", "sample accession", "sample_accession", "samn"}
LAY_ALIASES = {"librarylayout", "library_layout", "layout", "library layout"}


EXTRA_RE = re.compile(r"^(.+)_([3-9])\.(?:fastq|fq)\.gz$")    # <run>_3 / _4 ...: extra (index/barcode) reads


def classify(fname):
    m = EXTRA_RE.match(fname)
    if m:
        return m.group(1), "EXTRA"
    for suf, role in SUFFIXES:
        if fname.endswith(suf):
            return fname[:-len(suf)], role
    return None, None


def scan_disk(root):
    """run -> {'R1':abs,'R2':abs,'SE':abs,'dir':abs}; plus a list of role collisions."""
    disk = OrderedDict()
    collisions = []
    for base, _dirs, files in os.walk(root):
        for fn in sorted(files):
            run, role = classify(fn)
            if run is None:
                continue
            ap = os.path.abspath(os.path.join(base, fn))
            d = disk.setdefault(run, {"dir": os.path.abspath(base)})
            if role == "EXTRA":
                d.setdefault("EXTRA", []).append(ap)
                continue
            if role in d and role != "dir":
                collisions.append((run, "DUP_" + role, "%s | %s" % (d[role], ap)))
            d[role] = ap
    return disk, collisions


def _norm(s):
    return ("" if s is None else str(s)).strip()


def _delimiter(path):
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(8192)
        return csv.Sniffer().sniff(sample, delimiters=",\t;").delimiter
    except Exception:
        return "\t" if path.lower().endswith((".tsv", ".tab")) else ","


def load_runtable(path):
    """-> (mapping {run:(biosample,layout)}, colmap dict). sys.exit if cols missing."""
    delim = _delimiter(path)
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        rdr = csv.reader(fh, delimiter=delim)
        header = next(rdr, [])
        norm = [h.strip().lower() for h in header]

        def find(aliases):
            for i, h in enumerate(norm):
                if h in aliases:
                    return i
            return None
        ci_run, ci_bs, ci_lay = find(RUN_ALIASES), find(BS_ALIASES), find(LAY_ALIASES)
        colmap = {"delimiter": delim,
                  "Run": header[ci_run] if ci_run is not None else None,
                  "BioSample": header[ci_bs] if ci_bs is not None else None,
                  "LibraryLayout": header[ci_lay] if ci_lay is not None else None}
        if ci_run is None or ci_bs is None:
            sys.exit("ERROR: runtable %s needs a Run column and a BioSample column.\n"
                     "  headers seen: %s\n  delimiter: %r" % (path, header, delim))
        mapping = {}
        for row in rdr:
            if len(row) <= max(ci_run, ci_bs):
                continue
            run = _norm(row[ci_run])
            if not run:
                continue
            bs = _norm(row[ci_bs])
            lay = (_norm(row[ci_lay]).upper()
                   if (ci_lay is not None and len(row) > ci_lay) else "")
            mapping[run] = (bs, lay)
    return mapping, colmap


def run_layout(d):
    if "R1" in d and "R2" in d:
        return "PAIRED"
    if "SE" in d and "R1" not in d and "R2" not in d:
        return "SINGLE"
    if "R1" in d and "R2" not in d and "SE" not in d:
        return "ORPHAN_R1"
    if "R2" in d and "R1" not in d and "SE" not in d:
        return "ORPHAN_R2"
    return "AMBIGUOUS"


def main():
    ap = argparse.ArgumentParser(description="Build a STAR sample list from a FASTQ tree.")
    ap.add_argument("--input-dir", default=None)
    ap.add_argument("--runtable", default="")
    ap.add_argument("--out", default=None)
    ap.add_argument("--include-orphans-as-single", action="store_true",
                    help="emit R1-only runs as single-end rows instead of setting them aside")
    ap.add_argument("--inspect-runtable", action="store_true",
                    help="print the runtable column mapping and exit")
    args = ap.parse_args()

    if args.inspect_runtable:
        if not args.runtable:
            sys.exit("--inspect-runtable needs --runtable")
        m, colmap = load_runtable(args.runtable)
        print("runtable: %s" % args.runtable)
        print("  delimiter     -> %r" % colmap["delimiter"])
        for k in ("Run", "BioSample", "LibraryLayout"):
            print("  %-13s -> %s" % (k, colmap[k]))
        print("  rows mapped   -> %d" % len(m))
        return

    if not args.input_dir:
        sys.exit("ERROR: --input-dir is required (unless using --inspect-runtable)")
    root = os.path.abspath(args.input_dir)
    if not os.path.isdir(root):
        sys.exit("ERROR: --input-dir not found: %s" % root)
    out = os.path.abspath(args.out) if args.out else os.path.join(root, "sample_list.tsv")

    disk, collisions = scan_disk(root)
    meta = {}
    if args.runtable:
        meta, _cm = load_runtable(args.runtable)

    orphans, unmapped, mixed = [], [], list(collisions)
    bs2runs = OrderedDict()
    for run in sorted(disk):
        d = disk[run]
        lay = run_layout(d)
        if lay in ("ORPHAN_R1", "ORPHAN_R2", "AMBIGUOUS"):
            orphans.append((run, d["dir"], lay))
            if not (args.include_orphans_as_single and lay == "ORPHAN_R1"):
                continue
        bs = meta.get(run, ("", ""))[0]
        if not bs:
            if args.runtable:
                unmapped.append((run, d["dir"]))
            bs = run                       # fall back to the run's own accession
        bs2runs.setdefault(bs, []).append(run)

    rows = []
    n_paired = n_single = n_merged = 0
    for bs in sorted(bs2runs):
        runs = sorted(bs2runs[bs])
        layouts = set()
        for run in runs:
            l = run_layout(disk[run])
            if l == "ORPHAN_R1" and args.include_orphans_as_single:
                l = "SINGLE"
            layouts.add(l)
        if layouts == {"PAIRED"}:
            r1 = [disk[r]["R1"] for r in runs]
            r2 = [disk[r]["R2"] for r in runs]
            rows.append((bs, ",".join(r1), ",".join(r2))); n_paired += 1
        elif layouts == {"SINGLE"}:
            se = [(disk[r].get("SE") or disk[r].get("R1")) for r in runs]
            rows.append((bs, ",".join(se), "NA")); n_single += 1
        else:
            mixed.append((bs, "MIXED_LAYOUT:" + ",".join(sorted(layouts)), ",".join(runs)))
            continue
        if len(runs) > 1:
            n_merged += 1

    # ATOMIC write (T3.2): two run_star_pipeline.sh can run concurrently (the download watchdog kicks the
    # launcher while the launcher also self-polls), and this is the BUILD-ONCE denominator -- a torn/
    # interleaved sample_list.tsv would freeze a wrong exp_n for the whole run. tmp + os.replace is atomic
    # on the same volume, so a reader sees either the old or the complete new file, never a partial one.
    tmp = "%s.tmp.%d" % (out, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for bs, c2, c3 in rows:
            f.write("%s\t%s\t%s\n" % (bs, c2, c3))
    os.replace(tmp, out)

    def dump(suffix, header, items):
        p = out + suffix
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(header + "\n")
            for it in items:
                f.write("\t".join(str(x) for x in it) + "\n")
        return p, len(items)

    p_o, n_o = dump(".orphans", "run\tdir\tissue", sorted(orphans))
    p_u, n_u = dump(".unmapped", "run\tdir", sorted(unmapped))
    p_m, n_m = dump(".mixed", "item\tissue\tdetail", mixed)
    extra = [(run, f) for run in sorted(disk) for f in disk[run].get("EXTRA", [])]
    p_x, n_x = dump(".extra", "run\textra_read_file (not aligned)", extra)

    print("=" * 64)
    print("sample list : %s" % out)
    print("input dir   : %s" % root)
    print("runtable    : %s" % (args.runtable or "(none -- one BAM per run)"))
    print("-" * 64)
    print("runs on disk            : %d" % len(disk))
    print("samples written (rows)  : %d" % len(rows))
    print("   paired-end           : %d" % n_paired)
    print("   single-end           : %d" % n_single)
    print("   merged (>1 run)      : %d" % n_merged)
    print("-" * 64)
    print("orphan runs (no mate)   : %d -> %s" % (n_o, p_o))
    print("unmapped runs (no BS)   : %d -> %s" % (n_u, p_u))
    print("mixed / collisions      : %d -> %s" % (n_m, p_m))
    print("extra read files (_3..) : %d -> %s" % (n_x, p_x))
    print("=" * 64)
    if len(rows) == 0:
        print("WARNING: 0 samples -- check --input-dir and the FASTQ naming.")


if __name__ == "__main__":
    main()
