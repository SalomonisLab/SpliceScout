#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rerun_deploy.py -- build a TARGETED RE-RUN kit (or a concordance-only RE-SCORE kit) for a cell line whose cluster chain
already ran, so a better PSI plan (re-annotation, fixed compound calls, more studies) or a changed concordance (atlas,
reagent filter, scorer) reaches the cluster WITHOUT re-running what is already done. Generalizes the one-off
2026-09-22 K562 / A549 kits (runs/_rerun_2026-09-22/, DEVELOPER_GUIDE "Targeted re-runs").

  full     (default) the run's CURRENT PSI plan (built here, like the psi_bundle stage) decides which samples are
           needed; on the cluster rerun_submit.sh re-delivers / aligns / converts only the samples that have no BED
           yet, then the new PSI + concordance stages run in NEW folders.
  rescore  concordance only, over the existing, finished PSI stage (rescore_submit.sh).

With promotion (default) the finished results then move into the ORIGINAL folders (<root>/psi, <root>/concordance, or
--final-psi / --final-conc); their previous contents are kept as <folder>.old_<date>, never deleted.

  python rerun_deploy.py --run-dir runs/<run> --cluster-root /data/.../SpliceScout_Test/<CELL_LINE> [options]

Output: <run>/runtable/rerun/SpliceScout_<mode>_<CELL_LINE>.zip (+ build_summary.json). Upload it, unzip it into
<cluster root>/rerun/ (full) or <cluster root>/rescore/ and run the submit script there -- DRY_RUN=1 first.
Nothing here touches the run's own psi/ or concordance/ bundles: the kit is built in <run>/runtable/rerun/build_<mode>/.
"""
import argparse
import csv
import datetime
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RERUN_TEMPLATE_DIR = os.path.join(HERE, "rerun_template")
csv.field_size_limit(10 ** 8)

# PSI settings a re-run uses unless the run's config.json / --psi-set say otherwise. The concordance reads the PSI
# output next, so the PSI's compress-when-done stays off; only samples of the plan enter AltAnalyze.
PSI_RERUN_DEFAULTS = {"PSI_GROUPED_BEDS_ONLY": "1", "COMPRESS_WHEN_DONE": "off"}
CONC_RERUN_DEFAULTS = {"EXCLUDE_REAGENTS": 1}
DEFAULT_WAIT_HOURS = 1344          # 8 weeks: download + STAR of thousands of samples can take weeks
TEXT_EXT = (".sh", ".txt", ".tsv", ".md")


def _sub_root(txt, old, new):
    """Replace the path `old` (as a whole path, not a prefix of a longer name) with `new`."""
    return re.sub(re.escape(old) + r"(?=[/\"'\s]|$)", lambda _m: new, txt)


def _set_var(txt, name, value):
    return re.sub(r'(?m)^%s=("[^"]*"|\S*)' % re.escape(name), lambda _m: '%s="%s"' % (name, value), txt, count=1)


def _cfg_var(txt, name):
    m = re.search(r'(?m)^%s="?([^"\n]*)"?' % re.escape(name), txt)
    return m.group(1).strip() if m else ""


def _lf_copy(src, dst):
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(open(src, encoding="utf-8").read().replace("\r\n", "\n"))


def _retarget(folder, pairs, cfg_sets=None):
    """Rewrite root paths (old -> new) in every text file of a bundle folder, and set config.sh variables."""
    for fn in os.listdir(folder):
        p = os.path.join(folder, fn)
        if not os.path.isfile(p) or not fn.endswith(TEXT_EXT):
            continue
        txt = open(p, encoding="utf-8", errors="replace").read()
        new = txt
        for old, nw in pairs:
            if old and nw and old != nw:
                new = _sub_root(new, old, nw)
        if fn == "config.sh":
            for k, v in (cfg_sets or {}).items():
                new = _set_var(new, k, v)
        if new != txt:
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(new)


def _assert_gone(folder, old_roots):
    for fn in os.listdir(folder):
        p = os.path.join(folder, fn)
        if os.path.isfile(p) and fn.endswith(TEXT_EXT):
            t = open(p, encoding="utf-8", errors="replace").read()
            for old in old_roots:
                if old and re.search(re.escape(old) + r"(?=[/\"'\s]|$)", t):
                    raise RuntimeError(f"{fn} still points at {old}")


def _cluster_path(label, value):
    """Undo Git Bash / MSYS path conversion (as pipeline._cluster_cfg_from_args does): on Windows the shell rewrites a
    POSIX argument like /data/salomonis-archive/... into C:/Program Files/Git/data/salomonis-archive/... before python
    sees it, and the kit would then point at a path that does not exist on the cluster."""
    v = (value or "").strip()
    m = re.search(r"^[A-Za-z]:[\\/].*?(/(?:data|home|scratch|gpfs|lustre|work)/.+)$", v.replace("\\", "/"))
    if m:
        print(f"  WARNING: {label} looks MSYS-mangled by the shell: got {v} -> using {m.group(1)}\n"
              "           (prefix the command with MSYS_NO_PATHCONV=1 to prevent this)")
        return m.group(1)
    return v


def _kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"--psi-set / --conc-set take KEY=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _run_cfg(run_dir):
    """psi_cfg / concordance_cfg / cluster_cfg saved with the run (config.json; secrets are not read)."""
    try:
        d = json.load(open(os.path.join(run_dir, "config.json"), encoding="utf-8"))
    except Exception:
        return {}, {}, {}
    return (dict(d.get("psi_cfg") or {}), dict(d.get("concordance_cfg") or {}), dict(d.get("cluster_cfg") or {}))


def build_rerun_kit(run_dir, cluster_root, mode="full", cell_line="", psi_dir="", conc_dir="", final_psi=None,
                    final_conc=None, promote=True, psi_tag="", conc_tag="", atlas="", bed_scripts="", bed_tag="",
                    sample_key="", psi_set=None, conc_set=None, alert_email="", wait_hours=DEFAULT_WAIT_HOURS,
                    lsf_queue=""):
    if mode not in ("full", "rescore"):
        raise ValueError("mode must be 'full' or 'rescore'")
    sys.path.insert(0, HERE)
    import cluster_deploy
    import concordance_deploy
    import psi_deploy
    import bed_deploy
    import runtable_annotate as ra
    from pipeline_paths import Paths

    P = Paths(run_dir)
    sel = json.load(open(P.cellline_selection, encoding="utf-8"))
    cl = cell_line or ra._slug(sel.get("canonical", "cellline"))
    root = cluster_root.rstrip("/")
    bam_out = root + "/STAR_bams"
    default_psi, default_conc = root + "/psi", root + "/concordance"
    stamp = datetime.date.today().strftime("%Y%m%d")
    if mode == "full":
        psi_dir = (psi_dir or f"{root}/psi_rerun_{stamp}").rstrip("/")
        conc_dir = (conc_dir or f"{root}/concordance_rerun_{stamp}").rstrip("/")
        psi_tag = psi_tag or f"{cl}r_psi"
        conc_base = conc_tag or f"{cl}r"
    else:
        psi_dir = (psi_dir or default_psi).rstrip("/")          # the EXISTING PSI, read-only
        conc_dir = (conc_dir or f"{root}/concordance_rescore_{stamp}").rstrip("/")
        conc_base = conc_tag or f"{cl}rs"
    if promote:
        final_conc = (final_conc or default_conc).rstrip("/")
        final_psi = (final_psi or default_psi).rstrip("/") if mode == "full" else ""
    else:
        final_psi, final_conc = "", ""
    for a, b in ((psi_dir, final_psi), (conc_dir, final_conc)):
        if b and a == b:
            raise ValueError(f"the new folder and the promotion target are the same ({a})")

    run_psi, run_conc, run_cluster = _run_cfg(run_dir)
    build = os.path.join(P.runtable_dir, "rerun", "build_" + mode)
    shutil.rmtree(build, ignore_errors=True)
    os.makedirs(build)
    # build the bundles in the staging folder, never over the run's own psi/ + concordance/ bundles
    P.psi_dir = os.path.join(build, "psi")
    P.psi_bundle_zip = os.path.join(build, "psi_bundle.zip")
    P.concordance_dir = os.path.join(build, "concordance")
    P.concordance_bundle_zip = os.path.join(build, "concordance_bundle.zip")
    summary = {"mode": mode, "cell_line": cl, "run_root": root, "psi_dir": psi_dir, "conc_dir": conc_dir,
               "final_psi": final_psi, "final_conc": final_conc, "built": datetime.datetime.now().isoformat()}

    # ---- PSI (full mode) ------------------------------------------------------------------------------------------
    res = None
    if mode == "full":
        pcfg = {k: v for k, v in run_psi.items() if str(v).strip() != ""}
        pcfg.update(PSI_RERUN_DEFAULTS)
        pcfg.update({"enabled": "1", "BED_INPUT_DIR": root + "/STAR_beds"})
        if sample_key:
            pcfg["SAMPLE_KEY"] = sample_key
        pcfg.update(_kv(psi_set) if isinstance(psi_set, list) else (psi_set or {}))
        res = psi_deploy.build_psi_bundle(P, sel, bam_out, pcfg, download_job_tag=cl)
        sets = {"JOB_TAG": psi_tag}
        if alert_email:
            sets["ALERT_EMAIL"] = alert_email
        _retarget(P.psi_dir, [(res["psi_root"], psi_dir)], sets)
        with open(os.path.join(P.psi_dir, "psi_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(psi_deploy._psi_launch_sh(bam_out, psi_dir, psi_tag, max_wait_hours=wait_hours))
        ptxt = open(os.path.join(P.psi_dir, "config.sh"), encoding="utf-8").read()
        for k, v in (("PIPELINE_ROOT", psi_dir), ("JOB_TAG", psi_tag)):
            if _cfg_var(ptxt, k) != v:
                raise RuntimeError(f"PSI config.sh: {k} is {_cfg_var(ptxt, k)!r}, expected {v!r}")
        if psi_dir != res["psi_root"]:
            _assert_gone(P.psi_dir, [res["psi_root"]])
        if not os.path.exists(os.path.join(P.psi_dir, "sample_groups.tsv")):
            raise RuntimeError("the PSI plan has no comparison groups (sample_groups.tsv) -- nothing to re-run")
        cluster_deploy._zip_dir(P.psi_dir, P.psi_bundle_zip)
        summary["psi"] = {k: res.get(k) for k in ("species", "grouped", "job_tag")}
        summary["psi"]["job_tag"] = psi_tag

    # ---- concordance ----------------------------------------------------------------------------------------------
    ccfg = {k: v for k, v in run_conc.items() if str(v).strip() != ""}
    ccfg.update(CONC_RERUN_DEFAULTS)
    ccfg["enabled"] = "1"
    if atlas:
        ccfg["CANCER_ATLAS"] = atlas
    ccfg.update(_kv(conc_set) if isinstance(conc_set, list) else (conc_set or {}))
    cres = concordance_deploy.build_concordance_bundle(P, sel, bam_out, ccfg, download_job_tag=conc_base, ai_cfg=None)
    csets = {"ALERT_EMAIL": alert_email} if alert_email else {}
    _retarget(P.concordance_dir, [(cres["concord_root"], conc_dir), (cres["psi_root"], psi_dir)], csets)
    ctxt = open(os.path.join(P.concordance_dir, "config.sh"), encoding="utf-8").read()
    ctag = _cfg_var(ctxt, "JOB_TAG")
    with open(os.path.join(P.concordance_dir, "concordance_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(concordance_deploy._concordance_launch_sh(psi_dir, conc_dir, ctag, max_wait_hours=wait_hours))
    for k, v in (("PSI_ROOT", psi_dir), ("PIPELINE_ROOT", conc_dir)):
        if _cfg_var(ctxt, k) != v:
            raise RuntimeError(f"concordance config.sh: {k} is {_cfg_var(ctxt, k)!r}, expected {v!r}")
    _assert_gone(P.concordance_dir, [r for r, n in ((cres["psi_root"], psi_dir), (cres["concord_root"], conc_dir))
                                     if r != n])
    cluster_deploy._zip_dir(P.concordance_dir, P.concordance_bundle_zip)
    summary["concordance"] = {"atlas": cres.get("atlas"), "n_queries": cres.get("n_queries"), "job_tag": ctag}

    # ---- the plan's runs: label -> run -> study (full mode) ---------------------------------------------------------
    plan = os.path.join(build, "rerun_plan_runs.tsv")
    if mode == "full":
        groups = [ln.split("\t") for ln in open(os.path.join(P.psi_dir, "sample_groups.tsv"), encoding="utf-8")
                  .read().splitlines() if ln.strip()]
        labels = {g[0] for g in groups}
        table = ra._find_filtered_csv(P, ra._slug(sel["canonical"]))
        if not table:
            raise RuntimeError("no filtered run table in the run folder -- cannot map plan samples to runs")
        rows = list(csv.DictReader(open(table, encoding="utf-8")))
        header = list(rows[0].keys()) if rows else []
        key = psi_deploy._star_sample_key(P, sample_key)
        lab_col = psi_deploy._sample_col(header, key)
        run_col = psi_deploy._sample_col(header, "run")
        gse_col = psi_deploy._find_col(header, ("gse_series", "gse", "gse_accession (exp)", "gse_accession"))
        if not (lab_col and run_col and gse_col):
            raise RuntimeError(f"run table {os.path.basename(table)} lacks a sample / Run / GSE column")
        seen, n_rows = set(), 0
        with open(plan, "w", encoding="utf-8", newline="\n") as f:
            f.write("# label\trun\tstudy   (built %s from %s; labels = %s)\n"
                    % (datetime.date.today(), os.path.basename(table), key))
            for r in rows:
                lab, run = (r.get(lab_col) or "").strip(), (r.get(run_col) or "").strip()
                if lab in labels and run and (lab, run) not in seen:
                    seen.add((lab, run))
                    f.write("%s\t%s\t%s\n" % (lab, run, (r.get(gse_col) or "").strip()))
                    n_rows += 1
        comps = os.path.join(P.psi_dir, "sample_comps.tsv")
        summary.update({"plan_samples": len(labels), "plan_runs": n_rows, "sample_key": key,
                        "groups": len({g[1] for g in groups}),
                        "comparisons": (len(open(comps, encoding="utf-8").read().split()) // 2
                                        if os.path.exists(comps) else None)})

    # ---- rerun.env ----------------------------------------------------------------------------------------------------
    env = {"RERUN_MODE": mode, "CELL_LINE": cl, "RUN_ROOT": root, "PSI_DIR": psi_dir, "CONC_DIR": conc_dir,
           "PSI_TAG": psi_tag if mode == "full" else "", "CONC_TAG": ctag,
           "FINAL_PSI_DIR": final_psi or "", "FINAL_CONC_DIR": final_conc or "", "LSF_QUEUE": lsf_queue or ""}
    with open(os.path.join(build, "rerun.env"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# generated by rerun_deploy.py %s -- sourced by the kit's submit + promote scripts\n"
                % datetime.date.today())
        for k, v in env.items():
            f.write('%s=%s\n' % (k, cluster_deploy.shq(v)))

    # ---- the kit --------------------------------------------------------------------------------------------------------
    kit = os.path.join(build, "kit")
    os.makedirs(kit)
    tpl = ["promote_launch.sh", "README.txt"] + (["rerun_submit.sh", "recover_nested.sh"] if mode == "full"
                                                 else ["rescore_submit.sh"])
    for fn in tpl:
        _lf_copy(os.path.join(RERUN_TEMPLATE_DIR, fn), os.path.join(kit, fn))
    ship = ["rerun.env", "concordance_bundle.zip"]
    if mode == "full":
        for d in ("download", "star", "bed"):
            os.makedirs(os.path.join(kit, d))
        for fn in ("watchdog.sh", "fetch_missing.sh", "prefetch_job.sh", "fasterqdump_job.sh", "lib.sh"):
            _lf_copy(os.path.join(HERE, "cluster_template", fn), os.path.join(kit, "download", fn))
        for fn in ("build_sample_list.sh", "make_sample_list.py"):
            _lf_copy(os.path.join(HERE, "star_template", fn), os.path.join(kit, "star", fn))
        _lf_copy(os.path.join(HERE, "bed_template", "build_bam_list.sh"), os.path.join(kit, "bed", "build_bam_list.sh"))
        # STAR's finalize kicks <BAM_OUT>/bed/bed_launch.sh; it must run the BED stage's scripts wherever they live
        # (older deployments keep them in <root>/bed, not <BAM_OUT>/bed)
        bl = bed_deploy._bed_launch_sh(bam_out, bed_tag or f"{cl}_star_bed", max_wait_hours=wait_hours)
        here_line = 'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        if here_line not in bl:
            raise RuntimeError("bed_launch.sh: HERE line not found")
        bl = bl.replace(here_line, "HERE=%s   # the BED stage's scripts\n"
                        % cluster_deploy.shq((bed_scripts or bam_out + "/bed").rstrip("/")))
        with open(os.path.join(kit, "bed_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(bl)
        ship += ["rerun_plan_runs.tsv", "psi_bundle.zip"]
    for fn in ship:
        shutil.copyfile(os.path.join(build, fn), os.path.join(kit, fn))
    with open(os.path.join(build, "build_summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1, default=str)
    shutil.copyfile(os.path.join(build, "build_summary.json"), os.path.join(kit, "build_summary.json"))
    zp = os.path.join(P.runtable_dir, "rerun", "SpliceScout_%s_%s.zip" % (mode, cl))
    cluster_deploy._zip_dir(kit, zp)
    summary["kit"] = zp
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip(),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True, help="the PC run folder (runs/<run>) with the current annotation")
    ap.add_argument("--cluster-root", required=True, help="the cell line's run root on the cluster (holds STAR_bams/)")
    ap.add_argument("--mode", choices=("full", "rescore"), default="full")
    ap.add_argument("--cell-line", default="", help="short name for job tags / the kit name (default: from the run)")
    ap.add_argument("--psi-dir", default="", help="NEW PSI folder (full; default <root>/psi_rerun_<date>) or the "
                                                   "EXISTING PSI to re-score (rescore; default <root>/psi)")
    ap.add_argument("--conc-dir", default="", help="NEW concordance folder (default <root>/concordance_rerun|rescore_<date>)")
    ap.add_argument("--no-promote", action="store_true", help="leave the results in the new folders")
    ap.add_argument("--final-psi", default="", help="promotion target for the PSI (default <root>/psi)")
    ap.add_argument("--final-conc", default="", help="promotion target for the concordance (default <root>/concordance)")
    ap.add_argument("--psi-tag", default="", help="LSF job tag of the new PSI stage (default <CELL>r_psi)")
    ap.add_argument("--conc-tag", default="", help="job-tag base of the new concordance (default <CELL>r / <CELL>rs)")
    ap.add_argument("--atlas", default="", help="cancer atlas key (cancer_atlas_registry.json); default: the run's")
    ap.add_argument("--bed-scripts", default="", help="where the BED stage's scripts live (default <root>/STAR_bams/bed)")
    ap.add_argument("--bed-tag", default="", help="job-name prefix of the regenerated BED launcher")
    ap.add_argument("--sample-key", choices=("", "auto", "biosample", "run"), default="",
                    help="plan sample labels (default auto: Run when STAR ran without a run table)")
    ap.add_argument("--psi-set", action="append", default=[], metavar="KEY=VALUE", help="PSI config.sh override")
    ap.add_argument("--conc-set", action="append", default=[], metavar="KEY=VALUE", help="concordance config.sh override")
    ap.add_argument("--alert-email", default="", help="email the cluster jobs alert (default: the PC settings)")
    ap.add_argument("--lsf-queue", default="", help="queue for the kit's own jobs (default: the cluster default)")
    ap.add_argument("--wait-hours", type=int, default=DEFAULT_WAIT_HOURS, help="launcher bounded wait (default 1344 = 8 weeks)")
    a = ap.parse_args()
    for opt in ("cluster_root", "psi_dir", "conc_dir", "final_psi", "final_conc", "bed_scripts"):
        setattr(a, opt, _cluster_path("--" + opt.replace("_", "-"), getattr(a, opt)))
    for opt in ("psi_set", "conc_set"):                     # KEY=/data/... values get rewritten too
        setattr(a, opt, [(kv.split("=", 1)[0] + "=" + _cluster_path(kv.split("=", 1)[0], kv.split("=", 1)[1]))
                         if "=" in kv else kv for kv in getattr(a, opt)])
    s = build_rerun_kit(a.run_dir, a.cluster_root, mode=a.mode, cell_line=a.cell_line, psi_dir=a.psi_dir,
                        conc_dir=a.conc_dir, final_psi=a.final_psi, final_conc=a.final_conc, promote=not a.no_promote,
                        psi_tag=a.psi_tag, conc_tag=a.conc_tag, atlas=a.atlas, bed_scripts=a.bed_scripts,
                        bed_tag=a.bed_tag, sample_key=a.sample_key, psi_set=a.psi_set, conc_set=a.conc_set,
                        alert_email=a.alert_email, wait_hours=a.wait_hours, lsf_queue=a.lsf_queue)
    print(json.dumps({k: v for k, v in s.items() if k not in ("psi", "concordance")}, indent=1, default=str))
    sub = "rerun_submit.sh" if a.mode == "full" else "rescore_submit.sh"
    where = "rerun" if a.mode == "full" else "rescore"
    print(f"\nkit -> {s['kit']}  ({os.path.getsize(s['kit']) // 1024} KB)")
    print(f"on the cluster: unzip it into {a.cluster_root.rstrip('/')}/{where}/ , then\n"
          f"  DRY_RUN=1 bash {a.cluster_root.rstrip('/')}/{where}/{sub}\n"
          f"  bash {a.cluster_root.rstrip('/')}/{where}/{sub}")


if __name__ == "__main__":
    main()
