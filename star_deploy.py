# -*- coding: utf-8 -*-
"""
STAR alignment module deploy — the analysis stage that runs AFTER the SRA download on the cluster.

When the analysis module is "bulk_rna_seq" and the cluster is on, this fills the vendored
star_template/ config.sh from the form + the download run, ships it to <download_root>/star, and
AUTO-CHAINS it: a launcher job (star_launch.sh) waits for the download's PIPELINE_COMPLETE.txt
(LSF `-w ended(<download watchdog>)` + a sentinel poll), then runs ./run_star_pipeline.sh, which
reads the downloaded *.fastq.gz, resolves/builds a STAR genome index by organism, and aligns.

Reuses cluster_deploy's fill_config / SSH transport / diagnose_failure (the download path is
unchanged). STAR consumes FASTQ, never .sra.
"""
import os
import csv
import json
import shutil
from collections import Counter

from progress import NULL
import cluster_deploy

HERE = os.path.dirname(os.path.abspath(__file__))
STAR_TEMPLATE_DIR = os.path.join(HERE, "star_template")
REGISTRY_FILE = os.path.join(HERE, "star_index_registry.json")

# config.sh vars we fill (name -> default). Numerics are written unquoted.
STAR_CONFIG_DEFAULTS = {
    "FASTQ_INPUT_DIR": "/data/CHANGE_ME/fastqs",
    "BAM_OUT": "/data/CHANGE_ME/STAR_bams",
    "GENOME_DIR": "",
    "SJDB_GTF": "",
    "SJDB_OVERHANG": 100,
    "RUNTABLE": "",
    "SCRATCH": "/scratch/$USER",
    "THREADS": 5,    # LSF slots per STAR job; 5 packs 16-core nodes 3-up and divides a 125-slot cap evenly (25 jobs)
    "SORT_RAM": 20000000000,
    "STAR_EXTRA_ARGS": "",
    "MEM_MB": 64000,
    "MEM_RUSAGE": 10000,
    "WALL": "1108:00",   # default -W: queue MAX (66480 min) so jobs never die to walltime
    "LSF_QUEUE": "",
    "JOB_TAG": "star",
    "WATCHDOG_INTERVAL_MIN": 30,
    "MAX_STALL_PASSES": 2,
    "DELETE_FASTQ_AFTER_BAM": "1",   # UI toggle: delete a sample's source FASTQ(s) once its BAM is verified
    "STAR_MODULE": "STAR/2.7.10b",
    "SAMTOOLS_MODULE": "samtools",
    "ORGANISM": "Homo sapiens",
    "STAR_INDEX_ROOT": "",
    "REF_FASTA_URL": "",
    "REF_GTF_URL": "",
    "BUILD_THREADS": 16,
    "BUILD_MEM_MB": 64000,
    "BUILD_MEM_RUSAGE": 4000,
    "BUILD_WALL": "1108:00",   # genomeGenerate -W: queue MAX (66480 min)
}
STAR_NUMERIC = {"SJDB_OVERHANG", "THREADS", "SORT_RAM", "MEM_MB", "MEM_RUSAGE",
                "WATCHDOG_INTERVAL_MIN", "MAX_STALL_PASSES",
                "BUILD_THREADS", "BUILD_MEM_MB", "BUILD_MEM_RUSAGE"}


def _resolve_star_cfg(star_cfg):
    vals = dict(STAR_CONFIG_DEFAULTS)
    for k in STAR_CONFIG_DEFAULTS:
        if star_cfg and k in star_cfg and str(star_cfg[k]).strip() != "":
            vals[k] = star_cfg[k]
    return vals


def _find_runtable(P, slug):
    for cand in (P.runtable_filtered_csv(slug), P.runtable_filtered_csv(slug).replace(".csv", "_v2.csv")):
        if os.path.exists(cand):
            return cand
    return None


def detect_organism(P, sel, override=None):
    """Dominant Organism for the selected line's runs (the reconstructed run table has an Organism
    column); explicit override wins; default 'Homo sapiens' (the default query is human)."""
    if override and str(override).strip():
        return str(override).strip()
    slug = cluster_deploy._slug((sel or {}).get("canonical", ""))
    for cand in (_find_runtable(P, slug), P.runtable_all_csv):
        if cand and os.path.exists(cand):
            try:
                rows = list(csv.DictReader(open(cand, encoding="utf-8")))
                c = Counter((r.get("Organism") or "").strip() for r in rows if (r.get("Organism") or "").strip())
                if c:
                    return c.most_common(1)[0][0]
            except Exception:
                pass
    return "Homo sapiens"


def _url_registry_for(organism):
    try:
        data = json.load(open(REGISTRY_FILE, encoding="utf-8")).get("reference_urls", {})
        key = " ".join((organism or "").lower().split())
        norm = {" ".join(k.lower().split()): v for k, v in data.items()}
        return norm.get(key, {})
    except Exception:
        return {}


def _star_launch_sh(download_root, star_tag, check_min=30, max_wait_hours=336):
    """SELF-RESCHEDULING launcher (mirrors the watchdog pattern). Each pass is a short LSF job: if the
    SRA download has finished it launches STAR, otherwise it re-queues itself for +CHECK_MIN minutes.
    It lives entirely on the cluster, so SpliceScout can be CLOSED right after the upload — the cluster
    keeps checking across a possibly multi-day download and starts STAR on its own when it completes.

    Hardening:
      * If the download finalized STALLED (not COMPLETE), STAR still aligns whatever downloaded, but the
        STAR root is marked PIPELINE_INCOMPLETE_UPSTREAM.txt so STAR's finalize disables destructive
        cleanup / FASTQ-purge and writes an HONEST partial marker (stops the silent data-loss cascade).
      * Bounded wait: give up (PIPELINE_LAUNCH_TIMEOUT.txt) only after MAX_WAIT_HOURS *and* the upstream
        watchdog.log has gone stale, so a legitimately long multi-day download is never aborted but a
        DEAD upstream no longer polls a queue slot forever."""
    dl = download_root.rstrip("/")
    return (
        "#!/usr/bin/env bash\n"
        "# star_launch.sh -- generated by SpliceScout. Self-rescheduling: checks if the SRA download is\n"
        "# done; if so launches STAR, else re-queues itself. Runs as short LSF jobs on the cluster, so you\n"
        "# can close SpliceScout after the upload -- it survives multi-day downloads with no laptop on.\n"
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f"DL_ROOT={cluster_deploy.shq(dl)}\n"
        f"CHECK_MIN={int(check_min)}\n"
        f"MAX_WAIT_HOURS={int(max_wait_hours)}\n"
        f"JT={cluster_deploy.shq(star_tag)}\n"
        'STAR_ROOT="$DL_ROOT/STAR_bams"\n'
        "command -v bsub >/dev/null 2>&1 || { echo '[star_launch] no bsub here' >&2; exit 0; }\n"
        "# STAR already finished -> nothing to do\n"
        'if [ -f "$STAR_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$STAR_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        '  echo "[star_launch] STAR already finalized -> stop"; exit 0\n'
        "fi\n"
        "# download finished (or stalled -> align whatever downloaded) -> TRY to launch STAR. If the launch\n"
        "# (setup / index resolve / sample-list) FAILS -- e.g. a transient cluster hiccup -- DON'T give up:\n"
        "# fall through to reschedule + retry next pass (run_star_pipeline.sh is idempotent). Stop only on success.\n"
        'if [ -f "$DL_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$DL_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        "  # upstream STALLED (not COMPLETE) -> this run is PARTIAL: mark it so STAR finalize keeps the\n"
        "  # un-downloaded samples' inputs (no by_study purge) and reports honestly.\n"
        '  if [ ! -f "$DL_ROOT/PIPELINE_COMPLETE.txt" ] && [ -f "$DL_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        '    mkdir -p "$STAR_ROOT" 2>/dev/null\n'
        '    { echo "upstream download STALLED at $(date) -- not all accessions delivered."; \\\n'
        '      echo "This STAR run is PARTIAL: destructive cleanup + FASTQ/BAM deletion are DISABLED."; } \\\n'
        '      > "$STAR_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" 2>/dev/null\n'
        '    cp -f "$DL_ROOT/PIPELINE_STALLED.txt" "$STAR_ROOT/UPSTREAM_DOWNLOAD_STALLED.txt" 2>/dev/null\n'
        "  fi\n"
        '  echo "[star_launch] download finished -> launching STAR pipeline"\n'
        '  if bash "$HERE/run_star_pipeline.sh"; then\n'
        '    echo "[star_launch] STAR pipeline launched -> stop"; exit 0\n'
        "  fi\n"
        '  echo "[star_launch] run_star_pipeline.sh FAILED -> will retry in $CHECK_MIN min" >&2\n'
        "fi\n"
        + cluster_deploy.launch_wait_sh("star_launch", f"{dl}/star", '"$DL_ROOT/watchdog.log"', "the download")
        + "# download not done yet, OR a launch attempt just failed -> reschedule THIS launcher (+CHECK_MIN), then exit\n"
        "when=$(date -d \"+$CHECK_MIN min\" '+%Y:%m:%d:%H:%M' 2>/dev/null) || "
        "when=$(date -v+\"${CHECK_MIN}\"M '+%Y:%m:%d:%H:%M' 2>/dev/null)\n"
        'bsub -L /bin/bash -n 1 -M 1000 -W 66480 -b "$when" -J "${JT}_launch" \\\n'
        '     -o "$DL_ROOT/star/launch.out" -e "$DL_ROOT/star/launch.err" \\\n'
        '     "$HERE/star_launch.sh" >/dev/null 2>&1\n'
        'echo "[star_launch] not done / will retry -> next check scheduled for $when"\n'
    )


def _write_star_instructions(P, vals, download_root):
    txt = (
        "STAR alignment bundle (Bulk RNA-seq module)\n"
        "===========================================\n"
        f"Reads FASTQs from : {vals['FASTQ_INPUT_DIR']}\n"
        f"Writes BAMs to    : {vals['BAM_OUT']}\n"
        f"Organism          : {vals['ORGANISM']}\n"
        f"Genome index      : {vals.get('GENOME_DIR') or '(resolve by organism / build-once)'}\n\n"
        "Autonomous mode launches this automatically AFTER the download finishes. To run it manually:\n"
        f"  cd {download_root.rstrip('/')}/star\n"
        "  chmod +x *.sh\n"
        "  ./run_star_pipeline.sh\n"
        "Watch:  bash status.sh   (or tail -f <BAM_OUT>/watchdog.log)\n"
        "Done when <BAM_OUT>/PIPELINE_COMPLETE.txt appears.\n"
    )
    with open(os.path.join(P.star_dir, "RUN_STAR_ON_CLUSTER.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def build_star_bundle(P, sel, download_root, star_cfg, download_job_tag="sra", reporter=NULL):
    """Assemble runtable/star/ (filled config.sh + vendored scripts + registry + runtable + launcher),
    zip it, and return a summary. Returns None if the template is missing."""
    if not os.path.isdir(STAR_TEMPLATE_DIR):
        print(f"  STAR BUNDLE: vendored template missing at {STAR_TEMPLATE_DIR} -> skipping")
        return None
    reporter.set_detail("assembling STAR bundle…")
    if os.path.isdir(P.star_dir):
        shutil.rmtree(P.star_dir, ignore_errors=True)
    os.makedirs(P.star_dir, exist_ok=True)

    # vendored scripts (LF), everything except config.sh (generated)
    for name in sorted(os.listdir(STAR_TEMPLATE_DIR)):
        srcf = os.path.join(STAR_TEMPLATE_DIR, name)
        if os.path.isfile(srcf) and name != "config.sh":
            txt = open(srcf, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
            with open(os.path.join(P.star_dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)
    # ship the registry (organisms-block lookup happens on the cluster)
    if os.path.exists(REGISTRY_FILE):
        txt = open(REGISTRY_FILE, encoding="utf-8").read().replace("\r\n", "\n").replace("\r", "\n")
        with open(os.path.join(P.star_dir, "star_index_registry.json"), "w", encoding="utf-8", newline="\n") as f:
            f.write(txt)

    slug = cluster_deploy._slug((sel or {}).get("canonical", "") if sel else "")
    organism = detect_organism(P, sel, (star_cfg or {}).get("ORGANISM"))
    urls = _url_registry_for(organism)
    dl = download_root.rstrip("/")
    star_root = f"{dl}/star"

    vals = _resolve_star_cfg(star_cfg)
    vals["FASTQ_INPUT_DIR"] = f"{dl}/by_study"
    vals["BAM_OUT"] = f"{dl}/STAR_bams"
    vals["JOB_TAG"] = f"{(download_job_tag or 'sra')}_star"
    vals["ORGANISM"] = organism
    if urls.get("fasta_url") and not str(vals.get("REF_FASTA_URL", "")).strip():
        vals["REF_FASTA_URL"] = urls["fasta_url"]
    if urls.get("gtf_url") and not str(vals.get("REF_GTF_URL", "")).strip():
        vals["REF_GTF_URL"] = urls["gtf_url"]
    if urls.get("sjdb_overhang") and not (star_cfg or {}).get("SJDB_OVERHANG"):
        vals["SJDB_OVERHANG"] = urls["sjdb_overhang"]

    # copy the deep-dive run table (Run/BioSample/Organism) so STAR merges runs per BioSample
    src_rt = _find_runtable(P, slug)
    if src_rt:
        shutil.copyfile(src_rt, os.path.join(P.star_dir, f"SraRunTable_{slug}.csv"))
        vals["RUNTABLE"] = f"{star_root}/SraRunTable_{slug}.csv"
    else:
        vals["RUNTABLE"] = ""
        print("  STAR BUNDLE: no filtered run table found -> RUNTABLE empty (one BAM per run)")

    cfg_template = os.path.join(STAR_TEMPLATE_DIR, "config.sh")
    if not os.path.isfile(cfg_template):
        raise RuntimeError(f"STAR bundle: template config.sh missing at {cfg_template} -- "
                           "cannot build the bundle (reinstall/restore star_template/config.sh)")
    try:
        template = open(cfg_template, encoding="utf-8").read()
    except OSError as e:
        raise RuntimeError(f"STAR bundle: cannot read template config.sh ({cfg_template}): {e}")
    vals["ALERT_EMAIL"] = vals.get("ALERT_EMAIL") or cluster_deploy._alert_email()   # cluster-side email
    cluster_deploy.bake_diagnose_model(vals)                                         # optional diagnose-AI model path / cache dir
    with open(os.path.join(P.star_dir, "config.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(cluster_deploy.fill_config(template, vals, numeric=STAR_NUMERIC, defaults=STAR_CONFIG_DEFAULTS))
    with open(os.path.join(P.star_dir, "star_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_star_launch_sh(download_root, vals["JOB_TAG"]))

    _write_star_instructions(P, vals, download_root)
    try:
        cluster_deploy._zip_dir(P.star_dir, P.star_bundle_zip)
    except OSError as e:
        raise RuntimeError(f"STAR bundle: failed to write bundle zip {P.star_bundle_zip}: {e}")
    print(f"  STAR BUNDLE: organism={organism!r} index={vals.get('GENOME_DIR') or '(resolve/build)'} "
          f"-> {os.path.basename(P.star_bundle_zip)}")
    reporter.set_detail(f"STAR bundle ready (organism {organism})")
    return {"fastq_input": vals["FASTQ_INPUT_DIR"], "bam_out": vals["BAM_OUT"], "job_tag": vals["JOB_TAG"],
            "organism": organism, "runtable": vals["RUNTABLE"], "genome_dir": vals.get("GENOME_DIR", ""),
            "star_root": star_root}


def submit_star_over_ssh(P, cluster_cfg, secrets, download_root, reporter=NULL, prior_skipped=False):
    """Upload the STAR bundle and submit the auto-chain launcher (waits on the download). Non-fatal.
    prior_skipped=True (phase-range START at STAR): the download stage was skipped, so pre-create the
    download's PIPELINE_COMPLETE.txt sentinel the launcher polls -> STAR runs immediately."""
    cfg = cluster_cfg or {}
    secrets = secrets or {}
    host = (cfg.get("ssh_host") or "").strip()
    user = (cfg.get("ssh_user") or "").strip()
    port = str(cfg.get("ssh_port") or "22").strip() or "22"
    keyfile = (cfg.get("ssh_key") or "").strip()
    password = secrets.get("ssh_password") or ""
    dl_tag = (cfg.get("JOB_TAG") or "sra").strip() or "sra"
    star_tag = f"{dl_tag}_star"
    star_root = f"{download_root.rstrip('/')}/star"

    if not host or not user:
        print("  STAR SUBMIT: missing SSH host/user -> bundle still downloadable")
        return {"submitted": False, "reason": "missing host/user",
                "diagnosis": cluster_deploy.diagnose_failure("missing host/user")}
    if not os.path.isdir(P.star_dir):
        return {"submitted": False, "reason": "no bundle",
                "diagnosis": cluster_deploy.diagnose_failure("", "no bundle")}

    # Submit the SELF-RESCHEDULING launcher ONCE (no -w dependency: it re-queues itself every ~30 min
    # until the download writes PIPELINE_COMPLETE.txt, then launches STAR). It runs immediately, sees the
    # download isn't done yet, and arms the reschedule chain — so SpliceScout can be CLOSED after this.
    shq = cluster_deploy.shq
    # DETACH the launcher bsub (setsid + redirected fds, backgrounded inside a subshell so ONLY the bsub is
    # async — not the preceding tar/chmod) so the deploy ssh returns IMMEDIATELY. Under a saturated per-user
    # pending-job quota (a concurrent heavy run) this bsub BLOCKS on "Pending job threshold reached. Retrying
    # in 60s"; inline that hangs the deploy ssh until it times out ("stuck on launch star alignment"). setsid
    # keeps it retrying in the background after the ssh closes (plain nohup does NOT persist on this cluster).
    _lo = shq(star_root + '/launch.out')
    launch = (
        f"( setsid bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J {shq(star_tag + '_launch')} "
        f"-o {_lo} -e {shq(star_root + '/launch.err')} {shq(star_root + '/star_launch.sh')} "
        f"</dev/null >>{_lo} 2>&1 & )"
    )
    if prior_skipped:
        # START at STAR (download skipped): the launcher polls <download_root>/PIPELINE_COMPLETE.txt,
        # which no download will ever write -> pre-create it so STAR runs NOW on the FASTQs the user
        # already placed under <download_root>/by_study. Same remote shell, before the launcher bsub.
        # GUARDED (same rule as bed/psi_deploy): only when no download ever ran in that folder (no marker AND no
        # watchdog.log) -- touching a RUNNING download's marker makes its watchdog stop as "already finalized".
        _dr = download_root.rstrip("/")
        _dm = _dr + "/PIPELINE_COMPLETE.txt"; _dl = _dr + "/watchdog.log"
        launch = (f"if [ ! -f {shq(_dm)} ] && [ ! -f {shq(_dl)} ]; then mkdir -p {shq(_dr)} && "
                  f"touch {shq(_dm)}; fi; " + launch)
        print(f"  STAR SUBMIT: download skipped -> pre-touch {_dr}/PIPELINE_COMPLETE.txt ONLY if no download ran "
              "there (else STAR waits for the running/finished download)")
    print(f"=== STAR SUBMIT: {user}@{host}:{port} -> {star_root} ===")
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise RuntimeError("password auth needs paramiko (pip install paramiko) or use an SSH key")
            res = cluster_deploy._submit_paramiko(P, host, port, user, password, keyfile, star_root,
                                                  reporter, src_dir=P.star_dir, launch_cmd=launch)
        else:
            res = cluster_deploy._submit_systemssh(P, host, port, user, keyfile, star_root,
                                                   reporter, src_dir=P.star_dir, launch_cmd=launch)
        # WS3: only treat this as submitted on a CONFIRMED launch (the helper sets submitted=True
        # only after the bsub command returns success). A missing/falsey flag means NOT submitted,
        # so the orchestrator leaves it unmarked rather than skipping a never-armed launcher.
        if not (isinstance(res, dict) and res.get("submitted")):
            reason = (res or {}).get("reason", "submit not confirmed") if isinstance(res, dict) else "submit not confirmed"
            print(f"  STAR SUBMIT: launch not confirmed ({reason}) -> leaving unmarked")
            return {"submitted": False, "reason": reason,
                    "diagnosis": cluster_deploy.diagnose_failure("", reason)}
        print(f"  STAR SUBMIT: self-rescheduling launcher armed on {host} — STAR starts on the cluster "
              "when the download finishes (safe to close SpliceScout now)")
        return res
    except Exception as e:
        output = getattr(e, "output", "") or str(e)
        diag = cluster_deploy.diagnose_failure(output, str(e))
        print(f"  STAR SUBMIT FAILED: {e}  -> {diag['title']}")
        print("  -> the STAR bundle is still downloadable (RUN_STAR_ON_CLUSTER.txt).")
        reporter.set_detail(f"STAR submit failed: {diag['title']}")
        return {"submitted": False, "reason": str(e), "diagnosis": diag}
