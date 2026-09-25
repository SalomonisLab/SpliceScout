# -*- coding: utf-8 -*-
"""
Stages 13-14 — CLUSTER HANDOFF.

The deep-dive produced per-study accession lists at runtable/by_study/<GSE>/SraAccList.txt. The user's
LSF download pipeline (vendored into cluster_template/) consumes exactly that layout, configured only
through config.sh.

build_bundle  (stage 'cluster_bundle'): assemble a ready-to-run bundle under runtable/cluster/ —
  the vendored scripts + a config.sh filled from the user's settings + the PER-STUDY by_study/<GSE>/
  folders (each holding only that study's SraAccList.txt; the combined all-runs list is NEVER placed
  in by_study/, so the cluster downloads/converts each study INDEPENDENTLY) + RUN_ON_CLUSTER.txt, all
  zipped to cluster_bundle.zip.

submit_over_ssh (stage 'cluster_submit', autonomous only): ssh mkdir -> scp the bundle -> ssh run
  ./run_pipeline.sh. System ssh/scp (key/agent) by default; paramiko if a password is supplied and
  importable. Non-fatal: any failure leaves the downloadable bundle intact.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import zipfile

from progress import NULL

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "cluster_template")

# config.sh EDIT-THESE values we fill: name -> default. Numerics are written unquoted.
CONFIG_DEFAULTS = {
    "PIPELINE_ROOT": "/data/CHANGE_ME/A_PROJECT_FOLDER",
    "SCRATCH_DIR": "/scratch/$USER",
    "SRATOOLKIT_MODULE": "sratoolkit/3.0.0",
    "ASPERA_MODULE": "aspera/3.9.1",
    "LSF_QUEUE": "",
    "THREADS": 5,
    "MEM_MB": 32000,
    "WALL": "1108:00",   # default -W: normal-queue MAX (66480 min) so jobs never die to walltime
    "PREFETCH_MEM_MB": 132000,
    "WATCHDOG_INTERVAL_MIN": 30,
    "JOB_TAG": "sra",
}
NUMERIC = {"THREADS", "MEM_MB", "PREFETCH_MEM_MB", "WATCHDOG_INTERVAL_MIN"}


# A "safe expandable" config value: a pure path whose only `$` uses are allowlisted shell vars
# ($USER/$HOME/$SCRATCH/$TMPDIR) and which contains NO other shell-active char. Such values keep
# expansion (e.g. SCRATCH_DIR="/scratch/$USER"); everything else is escaped to an inert literal.
_SAFE_EXPAND_RE = re.compile(r'^(?:[A-Za-z0-9_./:+-]|\$(?:USER|HOME|SCRATCH|SCRATCHDIR|TMPDIR)\b)*$')


def shq(value):
    """POSIX single-quote a value for safe interpolation into a REMOTE SHELL COMMAND line.
    Closes shell injection: $, `, $(...), ;, |, &, spaces, embedded quotes are all inert."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def launch_wait_sh(who, marker_dir, upstream, what):
    """Bash for the BOUNDED WAIT of a self-rescheduling stage launcher (star/bed/psi/concordance_launch.sh share it).
    It gives up (PIPELINE_LAUNCH_TIMEOUT.txt in `marker_dir`) only once BOTH hold: MAX_WAIT_HOURS have passed since
    this launcher first ran, AND the whole upstream chain has gone quiet for 3 check intervals.
      * `upstream` = bash words (paths) whose mtime shows the chain is alive: every upstream stage's watchdog.log
        and the .launch_heartbeat each upstream launcher touches on every pass while it waits. Before, only the
        ADJACENT stage's log counted, so a stage that had not started yet (or the stale log of an earlier run)
        looked dead while the download two stages up was busy -- a long download timed out the BED/PSI launchers.
      * A heartbeat older than 12 h means this launcher is starting over (re-armed for a re-run): its first-seen
        stamp is renewed, else the stamp of the earlier run timed it out on its first pass.
    `who` = log prefix (e.g. 'bed_launch'); `what` = the upstream stage, for the timeout message."""
    return (
        "# Bounded wait: give up only after MAX_WAIT_HOURS AND once the whole upstream chain has gone quiet (a dead\n"
        "# chain). Alive = the freshest of the upstream watchdog logs + the heartbeats of upstream launchers still\n"
        "# waiting. A heartbeat of this launcher older than 12 h = re-armed (a re-run) -> the wait starts over.\n"
        f"LMARK={shq(marker_dir)}\n"
        'mkdir -p "$LMARK" 2>/dev/null\n'
        'STAMP="$HERE/.launch_first_seen"; BEAT="$LMARK/.launch_heartbeat"\n'
        "now=$(date +%s)\n"
        '_beat=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)\n'
        'if [ ! -f "$STAMP" ] || [ "$(( now - _beat ))" -gt 43200 ]; then echo "$now" > "$STAMP" 2>/dev/null; fi\n'
        ': > "$BEAT" 2>/dev/null\n'
        'first=$(cat "$STAMP" 2>/dev/null || echo "$now")\n'
        "up_age=999999999\n"
        f"for _up in {upstream}; do\n"
        '  [ -e "$_up" ] || continue\n'
        '  _a=$(( now - $(stat -c %Y "$_up" 2>/dev/null || echo 0) ))\n'
        '  [ "$_a" -lt "$up_age" ] && up_age=$_a\n'
        "done\n"
        'if [ "$(( now - first ))" -gt "$(( MAX_WAIT_HOURS * 3600 ))" ] && [ "$up_age" -gt "$(( CHECK_MIN * 180 ))" ]; then\n'
        f'  echo "{who} gave up at $(date): {what} never finalized and the upstream chain went quiet (waited >${{MAX_WAIT_HOURS}}h)." \\\n'
        '    > "$LMARK/PIPELINE_LAUNCH_TIMEOUT.txt" 2>/dev/null\n'
        f'  echo "[{who}] upstream dead -> giving up (PIPELINE_LAUNCH_TIMEOUT.txt written)" >&2; exit 0\n'
        "fi\n"
    )


# ---------- config.sh generation ----------
def _shval(name, value, numeric=NUMERIC, defaults=CONFIG_DEFAULTS):
    if name in numeric:
        try:
            return str(int(value))
        except Exception:
            return str(defaults.get(name, value))
    s = str(value)
    # Keep DOUBLE quotes (so every config.sh reader stays unchanged), but neutralize every
    # shell-active character inside them UNLESS the value is a trusted pure path that legitimately
    # relies on an allowlisted $VAR (then we leave $ alone so it still expands when sourced).
    if "$" in s and _SAFE_EXPAND_RE.match(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{s}"'


def fill_config(template_text, vals, numeric=NUMERIC, defaults=CONFIG_DEFAULTS):
    """Replace each EDIT-THESE assignment in the vendored config.sh with the user's value.
    `numeric`/`defaults` let other templates (e.g. the STAR config.sh) reuse this with their own var
    sets; the download call sites keep the module defaults, so their output is unchanged."""
    out = template_text
    for name, value in vals.items():
        rep = f"{name}={_shval(name, value, numeric, defaults)}"
        out = re.sub(rf"(?m)^{re.escape(name)}=.*$", lambda m, r=rep: r, out, count=1)
    return out


def _resolve_cfg(cluster_cfg):
    cfg = cluster_cfg or {}
    vals = dict(CONFIG_DEFAULTS)
    for k in CONFIG_DEFAULTS:
        if k in cfg and cfg[k] is not None and str(cfg[k]).strip() != "":
            vals[k] = cfg[k]
    if not str(vals.get("PIPELINE_ROOT", "")).strip():
        vals["PIPELINE_ROOT"] = CONFIG_DEFAULTS["PIPELINE_ROOT"]
    return vals


def _alert_email():
    """The user's alert email from the PC settings, baked into each stage's config.sh at deploy so a
    CLUSTER job can email the user DIRECTLY on an error/milestone via the cluster's own `mail` -- the PC
    poller only ever saw PIPELINE_* MARKER files, so a RUN-but-frozen job (and any cluster error) emailed
    nothing. Best-effort; '' = email OFF."""
    try:
        p = os.path.join(os.path.expanduser("~"), ".geo_pipeline_settings.json")
        return str((json.load(open(p, encoding="utf-8")).get("alert_email") or "")).strip()
    except Exception:
        return ""


def _diagnose_model_cfg():
    """Optional diagnose-AI model settings from the PC settings file, baked into each stage's config.sh so the
    cluster CPU LLM can find a model without re-uploading one every run:
      diagnose_model_path -> a specific .gguf to use (a CLUSTER path; overrides the search).
      diagnose_model_dir  -> a dir to CACHE the model in so future runs reuse it ('' -> per-stage
                             <PIPELINE_ROOT>/.splicescout_ai/models, auto-populated from the shared install on
                             first need by diagnose_job.sh).
    Returns (model_path, model_dir); '' for either when unset. Best-effort."""
    try:
        p = os.path.join(os.path.expanduser("~"), ".geo_pipeline_settings.json")
        s = json.load(open(p, encoding="utf-8"))
        return (str((s.get("diagnose_model_path") or "")).strip(),
                str((s.get("diagnose_model_dir") or "")).strip())
    except Exception:
        return "", ""


def bake_diagnose_model(vals):
    """Stamp the optional diagnose-AI model settings into `vals` (only when configured) so fill_config writes
    them into the stage's config.sh. No-op when neither is set (the template defaults '' stay, and the model
    is found via the shared install + auto-cached into the pipeline dir). Returns `vals` for chaining."""
    mp, md = _diagnose_model_cfg()
    if mp:
        vals["DIAGNOSE_MODEL_PATH"] = mp
    if md:
        vals["DIAGNOSE_MODEL_DIR"] = md
    return vals


def _slug(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("_") or "cellline"


def project_job_tag(base_tag, sel):
    """Scope an instance-level JOB_TAG by the cell line so REUSING one instance name for a DIFFERENT
    project gets a DISTINCT cluster folder + LSF job names (no interference), while the SAME project (same
    line) stays STABLE across runs/phase-starts (it resumes its folder). The cell-line slug is NORMALIZED
    (alnum, lowercase) so it is immune to the AI spelling the line differently between runs (MDS-L == MDSL
    == 'MDS-L cells' -> 'mdsl') — the exact fragmentation that made plain cell-line keying unsafe before.
    Idempotent: a tag already ending in the slug (or an instance name that already IS the line, e.g. an
    'A549' instance analysing A549) is returned unchanged."""
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(base_tag or "").strip()).strip("_")
    name = (sel or {}).get("canonical", "") if isinstance(sel, dict) else ""
    slug = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    slug = re.sub(r"(?:celllines?|cells?)$", "", slug) or slug   # 'MDS-L cells' == 'MDS-L' (AI suffix variance)
    if not slug:
        return base                                  # no cell line known -> instance tag unchanged
    if not base:
        return slug
    if base.lower() == slug or base.lower().endswith("_" + slug):
        return base                                  # already project-scoped / instance name == the line
    return f"{base}_{slug}"


def _effective_root(cluster_cfg, sel=None):
    """The form's PIPELINE_ROOT + a per-PROJECT subfolder = the JOB_TAG scoped by the (normalized) cell
    line (`project_job_tag`). So reusing ONE instance name for DIFFERENT cell lines gives each its own
    folder (they never share/clobber), while the SAME line resumes the SAME folder regardless of how the
    AI spells it that run (MDS-L/MDSL -> 'mdsl'). Used only as the FALLBACK root for a FRESH run; an
    already-deployed run resolves its real folder from the baked config.sh via `_read_config_root`, so this
    can never relocate an in-flight run."""
    cfg = _resolve_cfg(cluster_cfg)
    base = cfg["PIPELINE_ROOT"].rstrip("/")
    raw = str(cfg.get("JOB_TAG", "") or "").strip()
    tag = project_job_tag(raw, sel) if (raw or (sel or {}).get("canonical")) else ""
    return f"{base}/{tag}" if tag else base


def _read_config_root(P):
    """The PIPELINE_ROOT actually baked into the generated bundle's config.sh (single source of truth
    for where submit_over_ssh uploads + launches)."""
    cfgsh = os.path.join(P.cluster_dir, "config.sh")
    if os.path.exists(cfgsh):
        try:
            m = re.search(r'(?m)^PIPELINE_ROOT="?(.*?)"?\s*$', open(cfgsh, encoding="utf-8").read())
        except Exception:
            return None
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def _read_config_jobtag(P):
    """The JOB_TAG actually baked into the deployed download bundle's config.sh (the project-scoped tag, if
    this run was deployed by a build that scopes by cell line). Lets a resume / status read the SAME tag the
    cluster jobs were launched under, instead of re-deriving and possibly mismatching."""
    cfgsh = os.path.join(P.cluster_dir, "config.sh")
    if os.path.exists(cfgsh):
        try:
            m = re.search(r'(?m)^JOB_TAG="?(.*?)"?\s*$', open(cfgsh, encoding="utf-8").read())
        except Exception:
            return None
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


# ---------- stage 13: build the bundle ----------
def _gse_list(sel):
    out = []
    for s in (sel or {}).get("studies", []) or []:
        if isinstance(s, dict):
            g = s.get("gse") or s.get("accession") or s.get("GSE_Series") or s.get("GSE")
            if g:
                out.append(str(g))
        elif s:
            out.append(str(s))
    return out


def _write_cellline_marker(P, sel, vals):
    """Stamp the resolved cell line into the cluster folder. The folder is named by the STABLE instance
    tag (not the cell line), so this CELL_LINE.txt is how you map folder -> cell line on the cluster:
    `cat <root>/CELL_LINE.txt` or `grep -H . <PIPELINE_ROOT>/*/CELL_LINE.txt`."""
    root = vals["PIPELINE_ROOT"]
    canonical = (sel or {}).get("canonical", "") if sel else ""
    gses = _gse_list(sel)
    body = (
        f"{canonical or '(cell line unknown)'}\n"
        f"cluster folder (instance tag): {os.path.basename(root.rstrip('/'))}\n"
        f"PIPELINE_ROOT: {root}\n"
        + (f"GEO studies: {', '.join(gses)}\n" if gses else "")
    )
    with open(os.path.join(P.cluster_dir, "CELL_LINE.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(body)


def _write_instructions(P, vals, n_studies, n_acc, canonical=""):
    root = vals["PIPELINE_ROOT"]
    txt = (
        "GEO -> SRA cluster download bundle\n"
        "==================================\n"
        f"Cell line: {canonical or '(unknown)'}    (this folder is named by the instance tag, "
        "NOT the cell line -- see CELL_LINE.txt)\n"
        f"{n_studies} studies, {n_acc} run accessions, organized one folder per study under by_study/.\n\n"
        "Run on your LSF cluster:\n"
        f"  1. Put this bundle on the cluster at:  {root}\n"
        f"       unzip cluster_bundle.zip -d {root}\n"
        f"     (or:  scp -r cluster/*  <user>@<host>:{root}/ )\n"
        "  2. On the LSF submit host:\n"
        f"       cd {root}\n"
        "       chmod +x *.sh\n"
        "       ./run_pipeline.sh\n"
        f"  3. Watch:  ./status.sh   (or  tail -f {root}/watchdog.log )\n"
        f"     Done when  {root}/PIPELINE_COMPLETE.txt  appears.\n\n"
        f"This run is isolated in its own per-INSTANCE folder ({root}, named by the instance tag), so it never\n"
        "mixes with other instances -- and EVERY stage (download/STAR/BED/PSI) plus every re-run or\n"
        "phase-start of THIS instance lands in this same folder, regardless of how the cell line is named.\n\n"
        f"config.sh is pre-filled (PIPELINE_ROOT={root}, THREADS={vals['THREADS']}, "
        f"MEM_MB={vals['MEM_MB']}, WALL={vals['WALL']}). Edit it if your cluster's module names / "
        "queue / limits differ. Each study under by_study/ is downloaded and converted INDEPENDENTLY "
        "(per-study prefetch jobs, per-accession fasterq-dump) -> per-study *.fastq.gz.\n"
    )
    with open(os.path.join(P.cluster_dir, "RUN_ON_CLUSTER.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(txt)


def _zip_dir(src_dir, zip_path):
    if os.path.exists(zip_path):
        os.remove(zip_path)
    # An I/O failure mid-write would otherwise leave a silent partial zip; on any error remove the
    # half-written file and raise a clear message so build_bundle fails loudly instead of "succeeding".
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(src_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    z.write(fp, os.path.relpath(fp, src_dir))   # bundle contents at the zip root
    except Exception as e:
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
        except OSError:
            pass
        raise RuntimeError(f"failed to write cluster bundle zip {zip_path}: {e}")


def build_bundle(P, sel, cluster_cfg, reporter=NULL):
    """Assemble runtable/cluster/ (+ zip). Returns a summary dict."""
    if not os.path.isdir(TEMPLATE_DIR):
        print(f"  CLUSTER BUNDLE: vendored template missing at {TEMPLATE_DIR} -> skipping")
        return None
    reporter.set_detail("assembling cluster bundle…")
    # clean rebuild
    if os.path.isdir(P.cluster_dir):
        shutil.rmtree(P.cluster_dir, ignore_errors=True)
    os.makedirs(P.cluster_dir, exist_ok=True)

    # vendored scripts (everything except config.sh, which we generate) -> normalize to LF so the
    # bundle runs on the cluster even if a vendored file picked up CRLF on Windows (\r breaks bash)
    for name in sorted(os.listdir(TEMPLATE_DIR)):
        srcf = os.path.join(TEMPLATE_DIR, name)
        if os.path.isfile(srcf) and name != "config.sh":
            txt = open(srcf, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
            with open(os.path.join(P.cluster_dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(txt)

    # filled config.sh (LF newlines — it runs on the cluster). PIPELINE_ROOT gets a per-INSTANCE
    # subfolder (the JOB_TAG/instance tag) so every stage + re-run of one instance shares a STABLE folder
    # (the cell-line name, which the AI can resolve differently between runs, no longer steers the path).
    vals = _resolve_cfg(cluster_cfg)
    vals["PIPELINE_ROOT"] = _effective_root(cluster_cfg, sel)
    vals["ALERT_EMAIL"] = vals.get("ALERT_EMAIL") or _alert_email()   # cluster-side email on error/milestone
    bake_diagnose_model(vals)                                         # optional diagnose-AI model path / cache dir
    template_text = open(os.path.join(TEMPLATE_DIR, "config.sh"), encoding="utf-8").read()
    with open(os.path.join(P.cluster_dir, "config.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(fill_config(template_text, vals))

    # PER-STUDY accession lists only (never the combined list)
    bydir = os.path.join(P.cluster_dir, "by_study")
    os.makedirs(bydir, exist_ok=True)
    n_studies = n_acc = 0
    if os.path.isdir(P.by_study_dir):
        for gse in sorted(os.listdir(P.by_study_dir)):
            src_list = os.path.join(P.by_study_dir, gse, "SraAccList.txt")
            if os.path.isfile(src_list):
                os.makedirs(os.path.join(bydir, gse), exist_ok=True)
                # normalize to LF, one accession per line
                accs = [ln.strip() for ln in open(src_list, encoding="utf-8") if ln.strip()]
                with open(os.path.join(bydir, gse, "SraAccList.txt"), "w",
                          encoding="utf-8", newline="\n") as f:
                    f.write("\n".join(accs) + ("\n" if accs else ""))
                n_studies += 1
                n_acc += len(accs)

    _canonical = (sel or {}).get("canonical", "") if sel else ""
    _write_cellline_marker(P, sel, vals)          # CELL_LINE.txt -> uploaded to the folder root
    _write_instructions(P, vals, n_studies, n_acc, canonical=_canonical)
    _zip_dir(P.cluster_dir, P.cluster_bundle_zip)
    print(f"  CLUSTER BUNDLE: {n_studies} studies, {n_acc} accessions (split per study); "
          f"cell line {_canonical or '?'} -> {os.path.basename(P.cluster_bundle_zip)}")
    reporter.set_detail(f"{n_studies} studies / {n_acc} accessions zipped")
    return {"n_studies": n_studies, "n_accessions": n_acc, "pipeline_root": vals["PIPELINE_ROOT"],
            "cell_line": _canonical}


# ---------- stage 14: autonomous SSH submit ----------
class _SubmitError(RuntimeError):
    """Carries the full ssh/scp/remote output so diagnose_failure can read it."""
    def __init__(self, message, output=""):
        super().__init__(message)
        self.output = output or message


# ---------- log understanding: turn ssh/scp/remote output into a diagnosis ----------
def diagnose_failure(text, reason=""):
    """Read the ssh/scp/remote log and classify WHY the cluster upload failed.

    Returns {category, title, detail, suspect_fields} so the caller can tell the user what
    likely went wrong and which inputs to correct — then retry just the upload, no full rerun.
    suspect_fields use the same keys as the form/cluster_cfg:
    ssh_host | ssh_port | ssh_user | ssh_key | ssh_password | PIPELINE_ROOT.
    """
    t = ((text or "") + " " + (reason or "")).lower()

    def D(category, title, detail, fields):
        return {"category": category, "title": title, "detail": detail, "suspect_fields": fields}

    if "missing host/user" in t:
        return D("missing_credentials", "No SSH host/username given",
                 "Autonomous upload needs an SSH host and username — enter them to upload now.",
                 ["ssh_host", "ssh_user"])
    if any(s in t for s in ("could not resolve", "name or service not known", "nodename nor servname",
                            "name does not resolve", "temporary failure in name resolution")):
        return D("dns", "Host name could not be resolved",
                 "The SSH host didn't resolve. Check for a typo, use the cluster's real submit/login "
                 "host, and connect to the VPN if it's an internal host.", ["ssh_host"])
    if "no route to host" in t or "network is unreachable" in t:
        return D("network", "No network route to the host",
                 "The host is unreachable from here — usually a VPN/firewall issue or a wrong host.",
                 ["ssh_host"])
    if "connection refused" in t or "unable to connect to port" in t:
        return D("refused", "Connection refused",
                 "Nothing is accepting SSH at that host/port. Check the port (22 by default) and that "
                 "you're using the SSH-enabled host.", ["ssh_host", "ssh_port"])
    if "bad port" in t or "invalid port" in t:
        return D("port", "Invalid SSH port",
                 "The SSH port isn't a valid number (22 is the default).", ["ssh_port"])
    if any(s in t for s in ("timed out", "timeout", "error reading ssh protocol banner")):
        return D("timeout", "Connection timed out",
                 "Couldn't reach the host before timing out — usually VPN/firewall, or a wrong host/port.",
                 ["ssh_host", "ssh_port"])
    if "host key verification failed" in t or "remote host identification has changed" in t:
        return D("hostkey", "Host key verification failed",
                 "The host's SSH key changed or isn't trusted. Remove the stale entry for this host "
                 "from ~/.ssh/known_hosts, then retry.", [])
    if any(s in t for s in ("no such identity", "identity file", "could not load key",
                            "error loading key", "not accessible")):
        return D("keyfile", "Private key file problem",
                 "The private key path looks wrong or unreadable. Fix the key file path, or leave it "
                 "blank to use your SSH agent / a password.", ["ssh_key"])
    if "command not found" in t and ("bsub" in t or "run_pipeline" in t or "sra" in t):
        return D("wrong_submit_host", "Cluster tools not on PATH (wrong host?)",
                 "The job scheduler (e.g. bsub) wasn't found — you may be on the login node instead of "
                 "the LSF submit host. Use the submit host (e.g. bmiclusterp-head).", ["ssh_host"])
    if "mkdir" in t and ("permission denied" in t or "read-only" in t or "cannot create" in t):
        return D("root_perms", "Can't create the cluster folder",
                 "Couldn't create PIPELINE_ROOT on the cluster (no write permission or bad path). "
                 "Point it at a directory you can write to.", ["PIPELINE_ROOT"])
    if any(s in t for s in ("permission denied (publickey", "permission denied, please try again",
                            "authentication failed", "too many authentication failures",
                            "no authentication methods", "permission denied (")):
        return D("auth", "Authentication failed",
                 "The username/key/password was rejected. Check the username and either fix the key "
                 "file or provide the account password.", ["ssh_user", "ssh_key", "ssh_password"])
    if "needs paramiko" in t or ("paramiko" in t and "install" in t):
        return D("paramiko", "Password auth needs paramiko",
                 "Password login needs the 'paramiko' package (pip install paramiko), or switch to an "
                 "SSH key / agent and leave the password blank.", ["ssh_password", "ssh_key"])
    if "quota exceeded" in t or "no space left" in t or "disk quota" in t:
        return D("space", "Out of space / quota on the cluster",
                 "The cluster is out of disk or over quota at that path. Use a different PIPELINE_ROOT.",
                 ["PIPELINE_ROOT"])
    if "no such file or directory" in t and "mkdir" not in t:
        return D("root_path", "Path not found on the cluster",
                 "A path didn't exist on the cluster. Check PIPELINE_ROOT (and the private key path).",
                 ["PIPELINE_ROOT", "ssh_key"])
    if (any(s in t for s in ("winerror 2", "cannot find the file", "not recognized as",
                             "[errno 2]")) and "mkdir" not in t):
        return D("local_ssh", "ssh/scp not available locally",
                 "The local 'ssh'/'scp' command wasn't found. Install an OpenSSH client, or just use "
                 "the downloadable bundle instead.", [])
    return D("unknown", "Cluster upload failed",
             "Couldn't complete the upload — review the log line above, then correct the host, "
             "username, port, key, password, or PIPELINE_ROOT and retry.",
             ["ssh_host", "ssh_user", "ssh_port", "ssh_key", "ssh_password", "PIPELINE_ROOT"])


# The cluster emits a long SSH login banner (CCHMC acceptable-use policy) + a post-quantum warning on EVERY
# connection. Strip those lines from captured ssh/scp output so the GUI live log shows pipeline output (not
# the legal banner) and so diagnose_failure doesn't read the banner.
_SSH_NOISE = re.compile(
    r"post-quantum|store now|decrypt later|openssh\.com/pq|may need to be upgraded|vulnerable to|"
    r"II-105|Acceptable Use of Information|CCHMC Personnel|community physicians|business partner|"
    r"Medical Staff|Federal, state|disciplinary action|Refer to CCHMC|Personnel Policy|sanctioned|"
    r"cancellation of any contractual|denial of access", re.I)


def _strip_ssh_noise(text):
    """Drop the cluster's SSH login banner + post-quantum warning lines from captured ssh/scp output."""
    if not text:
        return text
    return "\n".join(ln for ln in text.splitlines() if not _SSH_NOISE.search(ln))


def _run(argv, timeout=180):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    out = _strip_ssh_noise((p.stdout or "") + (p.stderr or ""))
    if out.strip():
        print("    " + out.strip().replace("\n", "\n    "))
    if p.returncode != 0:
        raise _SubmitError(f"{os.path.basename(argv[0])} exit {p.returncode}: {out.strip()[:400]}", out)
    return out


def _make_bundle_tar(src_dir):
    """Pack src_dir's CONTENTS into ONE local .tar.gz (entries relative to src_dir) so the upload is a single
    transfer + a single remote untar, NOT thousands of per-file scp/sftp round-trips. A big run's by_study/
    holds one tiny SraAccList.txt per study (e.g. A549 = 766 files); scp -r of them is hundreds of handshakes
    that blow past the timeout ('stuck on upload'). Returns the temp tar path; the caller removes it."""
    import tarfile, tempfile
    fd, tarpath = tempfile.mkstemp(prefix="ss_bundle_", suffix=".tar.gz")
    os.close(fd)
    with tarfile.open(tarpath, "w:gz") as tf:
        for n in sorted(os.listdir(src_dir)):
            tf.add(os.path.join(src_dir, n), arcname=n)
    return tarpath


def _submit_systemssh(P, host, port, user, keyfile, root, reporter, src_dir=None,
                      launch_cmd="./run_pipeline.sh"):
    src_dir = src_dir or P.cluster_dir
    target = f"{user}@{host}"
    common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    ssh = ["ssh", "-p", str(port)] + common + (["-i", keyfile] if keyfile else [])
    scp = ["scp", "-P", str(port)] + common + (["-i", keyfile] if keyfile else [])
    _run(ssh + [target, f"mkdir -p {shq(root)}"])
    # ONE tar.gz transfer + a remote untar instead of scp -r of the whole tree (scp -r of a big by_study/ is
    # thousands of per-file round-trips -> times out). Untar is generous (many small files on NFS).
    tarpath = _make_bundle_tar(src_dir)
    try:
        _run(scp + [tarpath, f"{target}:{root}/_bundle.tar.gz"], timeout=600)
    finally:
        try: os.remove(tarpath)
        except OSError: pass
    out = _run(ssh + [target, f"cd {shq(root)} && tar xzf _bundle.tar.gz && rm -f _bundle.tar.gz "
                              f"&& chmod +x *.sh && {launch_cmd}"], timeout=900)
    print(f"  CLUSTER SUBMIT: launched on {target}:{root}")
    reporter.set_detail(f"launched on {host}")
    return {"submitted": True, "host": host, "root": root, "output": out[-2000:]}


def _submit_paramiko(P, host, port, user, password, keyfile, root, reporter, src_dir=None,
                     launch_cmd="./run_pipeline.sh"):
    src_dir = src_dir or P.cluster_dir
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": host, "port": int(port), "username": user, "timeout": 20}
    if password:
        kw["password"] = password
    if keyfile:
        kw["key_filename"] = keyfile
    cli.connect(**kw)

    def ex(cmd, timeout=900):
        _in, _out, _err = cli.exec_command(cmd, timeout=timeout)
        out = _strip_ssh_noise(_out.read().decode("utf-8", "replace") + _err.read().decode("utf-8", "replace"))
        # cap the exit-status wait so a stalled network can't hang the worker forever (generous: a remote untar
        # of many small files on NFS can take a bit)
        _out.channel.settimeout(120)
        rc = _out.channel.recv_exit_status()
        if out.strip():
            print("    " + out.strip().replace("\n", "\n    "))
        if rc != 0:
            raise _SubmitError(f"remote exit {rc}: {out.strip()[:400]}", out)
        return out

    tarpath = _make_bundle_tar(src_dir)
    try:
        ex(f"mkdir -p {shq(root)}")
        # ONE tar.gz upload + remote untar, NOT a per-file sftp.put walk (thousands of small files -> timeout)
        sftp = cli.open_sftp()
        sftp.get_channel().settimeout(600)   # so a stalled transfer can't hang the worker forever
        sftp.put(tarpath, f"{root}/_bundle.tar.gz")
        sftp.close()
        out = ex(f"cd {shq(root)} && tar xzf _bundle.tar.gz && rm -f _bundle.tar.gz && chmod +x *.sh && {launch_cmd}")
    finally:
        try: os.remove(tarpath)
        except OSError: pass
        cli.close()
    print(f"  CLUSTER SUBMIT: launched on {user}@{host}:{root}")
    reporter.set_detail(f"launched on {host}")
    return {"submitted": True, "host": host, "root": root, "output": out[-2000:]}


def submit_over_ssh(P, cluster_cfg, secrets, reporter=NULL):
    """Upload the bundle and launch ./run_pipeline.sh on the cluster. Non-fatal on failure."""
    cfg = cluster_cfg or {}
    secrets = secrets or {}
    host = (cfg.get("ssh_host") or "").strip()
    user = (cfg.get("ssh_user") or "").strip()
    port = str(cfg.get("ssh_port") or "22").strip() or "22"
    keyfile = (cfg.get("ssh_key") or "").strip()
    password = secrets.get("ssh_password") or ""
    # upload to exactly the PIPELINE_ROOT the bundle's config.sh declares (the per-instance subfolder)
    root = _read_config_root(P) or _resolve_cfg(cfg)["PIPELINE_ROOT"]

    if not host or not user:
        print("  CLUSTER SUBMIT: missing SSH host/user -> can't upload yet (bundle still downloadable)")
        reporter.set_detail("need SSH host/user")
        return {"submitted": False, "reason": "missing host/user",
                "diagnosis": diagnose_failure("missing host/user")}
    if not os.path.isdir(P.cluster_dir):
        print("  CLUSTER SUBMIT: no bundle to upload -> skipped")
        return {"submitted": False, "reason": "no bundle",
                "diagnosis": diagnose_failure("", "no bundle")}

    print(f"=== CLUSTER SUBMIT: {user}@{host}:{port} -> {root} ===")
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise RuntimeError("password auth needs paramiko (pip install paramiko) "
                                   "or use an SSH key / agent instead")
            return _submit_paramiko(P, host, port, user, password, keyfile, root, reporter)
        return _submit_systemssh(P, host, port, user, keyfile, root, reporter)
    except Exception as e:
        output = getattr(e, "output", "") or str(e)
        diag = diagnose_failure(output, str(e))
        print(f"  CLUSTER SUBMIT FAILED: {e}")
        print(f"  -> diagnosis: {diag['title']} — {diag['detail']}")
        if diag["suspect_fields"]:
            print(f"  -> likely wrong: {', '.join(diag['suspect_fields'])}")
        print("  -> the bundle is still downloadable; follow RUN_ON_CLUSTER.txt to run it manually.")
        reporter.set_detail(f"submit failed: {diag['title']}")
        return {"submitted": False, "reason": str(e), "diagnosis": diag}


# ---------- on-demand cluster status (read status.sh + watchdog.log over SSH) ----------
def _ssh_capture_systemssh(host, port, user, keyfile, command, timeout=60):
    target = f"{user}@{host}"
    common = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=20"]
    ssh = ["ssh", "-p", str(port)] + common + (["-i", keyfile] if keyfile else [])
    p = subprocess.run(ssh + [target, command], capture_output=True, text=True, timeout=timeout)
    out = _strip_ssh_noise((p.stdout or "") + (p.stderr or ""))
    if p.returncode != 0 and not (p.stdout or "").strip():
        raise _SubmitError(f"ssh exit {p.returncode}: {out.strip()[:400]}", out)
    return _strip_ssh_noise(p.stdout or "")


def _ssh_capture_paramiko(host, port, user, password, keyfile, command, timeout=60):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {"hostname": host, "port": int(port), "username": user, "timeout": 20}
    if password:
        kw["password"] = password
    if keyfile:
        kw["key_filename"] = keyfile
    cli.connect(**kw)
    try:
        _in, _out, _err = cli.exec_command(command, timeout=timeout)
        out = _out.read().decode("utf-8", "replace")
        err = _err.read().decode("utf-8", "replace")
    finally:
        cli.close()
    return _strip_ssh_noise(out or err)


def _eta_from_watchdog(wd_text, overall):
    """ETA (seconds) from timestamped 'progress: done/exp' lines in watchdog.log; None if unknown.
    Uses the conversion RATE between the first and last progress line, so the cluster's timezone is
    irrelevant — only the time DIFFERENCE matters."""
    if not wd_text or not overall or not overall.get("exp"):
        return None
    import calendar
    pts = []
    for m in re.finditer(
            r"\[(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})\][^\n]*progress:\s*(\d+)\s*/\s*(\d+)",
            wd_text):
        t = calendar.timegm(tuple(int(m.group(i)) for i in range(1, 7)) + (0, 0, 0))
        pts.append((t, int(m.group(7))))
    if len(pts) < 2:
        return None
    (t0, d0), (t1, d1) = pts[0], pts[-1]
    remaining = overall["exp"] - overall["done"]
    if remaining <= 0:
        return 0
    if t1 <= t0 or d1 <= d0:
        return None
    rate = (d1 - d0) / (t1 - t0)                      # runs per second
    return int(remaining / rate) if rate > 0 else None


def parse_status(text):
    """Parse the remote progress probe (a ROOT line + STUDY lines + ---META--- + ---WATCHDOG--- tail)
    into a dict. Per study: DOWNLOADED (a `.sra` exists in a per-accession subdir or flat, or it's
    already converted) and CONVERTED (a `.fastq.gz` exists) — so a study mid-download doesn't read as
    0. Unit-testable (no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    rm = re.search(r"(?m)^ROOT\s+(\S.*?)\s*$", body)
    root = rm.group(1).strip() if rm else ""
    per_study = []
    for m in re.finditer(r"(?m)^STUDY\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$", body):
        per_study.append({"gse": m.group(1), "exp": int(m.group(2)),
                          "downloaded": int(m.group(3)), "converted": int(m.group(4))})
    overall = None
    if per_study:
        exp = sum(s["exp"] for s in per_study)
        dl = sum(s["downloaded"] for s in per_study)
        cv = sum(s["converted"] for s in per_study)
        overall = {"exp": exp, "downloaded": dl, "converted": cv,
                   "pct": round(100 * cv / exp, 1) if exp else 0.0,
                   "dl_pct": round(100 * dl / exp, 1) if exp else 0.0}
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    eta_base = {"exp": overall["exp"], "done": overall["converted"]} if overall else None
    return {"overall": overall, "per_study": per_study, "root": root,
            "live_jobs": int(lm.group(1)) if lm else None,
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "partial": "PARTIAL" in meta, "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta,
            "eta_seconds": _eta_from_watchdog(wd, eta_base), "raw": text[-4000:]}


# Self-discovering progress probe: find the pipeline root from this instance's <TAG>_* LSF jobs (their
# bsub CWD), so status works even after the SpliceScout server restarted and the local run dir is gone
# (the user's manual ssh diagnostic does the same). Then count per study against each SraAccList.txt.
# DOWNLOADED = accessions with a `.sra` in a per-accession subdir (`<GSE>/<acc>/<acc>.sra`, prefetch's
# layout) OR flat, OR already converted; CONVERTED = `.fastq.gz`. (raw string -> literal globs.)
_STATUS_PROBE = r'''TAG=%TAG%; FB=%FB%
jobcwd() { bjobs -l "$1" 2>/dev/null | tr -d '\n' | sed -n 's/.*CWD <\([^>]*\)>.*/\1/p' | tr -d ' '; }
# studies dir = a pf/cs job's bsub CWD (that IS <root>/by_study), or dirname of an fqd job's CWD
SD=""
for pat in "${TAG}_pf_*" "${TAG}_cs_*" "${TAG}_fqd_*"; do
  JID=$(bjobs -noheader -o jobid -J "$pat" 2>/dev/null | head -1); [ -n "$JID" ] || continue
  C=$(jobcwd "$JID"); [ -n "$C" ] || continue
  case "$pat" in "${TAG}_fqd_*") C=$(dirname "$C") ;; esac
  SD="$C"; [ -d "$SD" ] && break
done
# root = watchdog's CWD, else dirname of the studies dir, else the caller's fallback
ROOT=""
WID=$(bjobs -noheader -o jobid -J "${TAG}_watchdog" 2>/dev/null | head -1)
[ -n "$WID" ] && ROOT=$(jobcwd "$WID")
[ -z "$ROOT" ] && [ -n "$SD" ] && ROOT=$(dirname "$SD")
[ -z "$ROOT" ] && ROOT="$FB"
[ -n "$SD" ] && [ -d "$SD" ] || SD="$ROOT/by_study"
echo "ROOT $ROOT"
echo "SDIR $SD"
if [ -d "$SD" ]; then
  cd "$SD"
  for d in */; do
    g=${d%/}; L="$g/SraAccList.txt"; [ -f "$L" ] || continue
    exp=$(grep -c . "$L")
    sra=$(ls "$g"/*/*.sra "$g"/*.sra "$g"/*/*.sralite "$g"/*.sralite 2>/dev/null | sed 's#.*/##; s/\.sralite$//; s/\.sra$//' | sort -u | grep -c .)
    cv=$(ls "$g"/*.fastq.gz 2>/dev/null | sed 's#.*/##; s/_[0-9]\.fastq\.gz$//; s/\.fastq\.gz$//' | sort -u | grep -c .)
    echo "STUDY $g $exp $((sra+cv)) $cv"
  done
fi
echo "---META---"
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_COMPLETE_PARTIAL.txt" ] && echo PARTIAL
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$ROOT" ] && [ -f "$ROOT/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "---WATCHDOG---"
[ -n "$ROOT" ] && tail -n 80 "$ROOT/watchdog.log" 2>/dev/null
true'''


def remote_status(host, user, port, keyfile, password, job_tag, fallback_root=""):
    """SSH to the LSF submit host, DISCOVER the pipeline root from this instance's `<job_tag>_*` jobs
    (works even after a server restart with no local run dir), then count per study DOWNLOADED (.sra in
    a per-accession subdir or flat, or already converted) and CONVERTED (.fastq.gz). Falls back to
    `fallback_root` if no jobs are live. Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip()
    user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user",
                "diagnosis": diagnose_failure("missing host/user")}
    port = str(port or "22").strip() or "22"
    keyfile = (keyfile or "").strip()
    cmd = (_STATUS_PROBE.replace("%TAG%", shq(job_tag or "sra"))
                        .replace("%FB%", shq(fallback_root or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko (pip install paramiko) or use an SSH key", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        output = getattr(e, "output", "") or str(e)
        return {"ok": False, "error": str(e), "diagnosis": diagnose_failure(output, str(e))}
    parsed = parse_status(text)
    parsed.update({"ok": True, "host": host, "job_tag": job_tag})
    return parsed


# ---------- cross-stage STALL/FAILURE alert scan (turn a silent stall into a heads-up) ----------
# The per-stage status probes already detect STALLED/ORPHANED/LAUNCH_TIMEOUT, but a downstream stall
# (e.g. BED) was invisible to the download-centric GUI check -> A549 sat dead for 5 days. This ONE find
# scans `root` for any stage's FAILURE marker so the GUI/Assistant can flag it RED on every poll.
# Callers MUST pass a per-RUN effective root (PIPELINE_ROOT/<JOB_TAG>), NOT the bare shared PIPELINE_ROOT:
# scanning the shared root would surface SIBLING/abandoned runs' stalls as false alerts on one run's panel.
# A second find lists stage dirs that reached PIPELINE_COMPLETE.txt so a STALLED marker that was later
# RESOLVED (re-armed -> completed, stale marker left behind) is suppressed instead of re-alerting forever.
_ALERTS_PROBE = r"""R=%ROOT%
if [ -n "$R" ] && [ -d "$R" ]; then
  find "$R" -maxdepth 6 \( -name PIPELINE_STALLED.txt -o -name PIPELINE_ORPHANED.txt -o -name PIPELINE_LAUNCH_TIMEOUT.txt -o -name PIPELINE_COMPRESS_FAILED.txt \) -printf 'MARK\t%p\t%TY-%Tm-%Td %TH:%TM\n' 2>/dev/null | sort
  find "$R" -maxdepth 6 -name PIPELINE_COMPLETE.txt -printf 'DONE\t%h\n' 2>/dev/null | sort
fi
true"""


def remote_alerts(host, user, port, keyfile, password, root):
    """SSH-scan `root` for ANY stage FAILURE marker (PIPELINE_STALLED / _ORPHANED / _LAUNCH_TIMEOUT) under
    it. Returns {"ok", "alerts":[{kind,stage,path,when}], "root", ...}. Cheap (two finds). Non-fatal. This
    is the 'same-hour heads-up' the silent 5-day BED stall lacked. `root` should be a per-RUN effective
    root (the caller scopes it) so one run's panel never inherits a sibling run's stall. A marker whose
    stage dir also holds PIPELINE_COMPLETE.txt is skipped (the stall was resolved -> stale marker)."""
    host = (host or "").strip(); user = (user or "").strip()
    root = (root or "").strip().rstrip("/")
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user", "alerts": []}
    if not root:
        return {"ok": False, "error": "no PIPELINE_ROOT configured to scan", "alerts": []}
    port = str(port or "22").strip() or "22"; keyfile = (keyfile or "").strip()
    cmd = _ALERTS_PROBE.replace("%ROOT%", shq(root))
    try:
        if password:
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e), "alerts": []}
    done_dirs = set(); raw_marks = []
    for line in (text or "").splitlines():
        line = line.rstrip("\n")
        if line.startswith("DONE\t"):
            d = line[5:].strip().rstrip("/")
            if d:
                done_dirs.add(d)
        elif line.startswith("MARK\t"):
            raw_marks.append(line[5:])
    alerts = []
    for rec in raw_marks:
        if "\t" not in rec or "PIPELINE_" not in rec:
            continue
        path, when = rec.split("\t", 1)
        if path.rsplit("/", 1)[0].rstrip("/") in done_dirs:   # stage later reached COMPLETE -> stale marker
            continue
        base = path.rsplit("/", 1)[-1]
        kind = ("STALLED" if "STALLED" in base else "ORPHANED" if "ORPHANED" in base
                else "LAUNCH_TIMEOUT" if "LAUNCH_TIMEOUT" in base
                else "COMPRESS_FAILED" if "COMPRESS_FAILED" in base else "FAILURE")
        low = path.lower()
        stage = ("CONCORDANCE" if "/concordance/" in low else "PSI" if "/psi/" in low
                 else "BED" if "/bed/" in low else "STAR" if "/star" in low else "download")
        alerts.append({"kind": kind, "stage": stage, "path": path, "when": when.strip()})
    return {"ok": True, "alerts": alerts, "root": root, "host": host}


def send_alert_email(host, user, port, keyfile, password, email, subject, body):
    """Send a plain-text alert to `email` using the CLUSTER's own `mail` (no SMTP/app-password needed on
    the PC). Body is base64'd to dodge shell-quoting. Returns True if the cluster accepted it (delivery to
    an external inbox still depends on the cluster's mail relay — may land in spam). Best-effort."""
    host = (host or "").strip(); user = (user or "").strip(); email = (email or "").strip()
    if not host or not user or not email:
        return False
    port = str(port or "22").strip() or "22"; keyfile = (keyfile or "").strip()
    b64 = base64.b64encode((body or "").encode("utf-8")).decode("ascii")
    cmd = ("printf %s " + shq(b64) + " | base64 -d | mail -s " + shq(subject or "SpliceScout alert")
           + " " + shq(email))
    try:
        if password:
            _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            _ssh_capture_systemssh(host, port, user, keyfile, cmd)
        return True
    except Exception:
        return False


# ---------- STAR alignment progress (the analysis stage after the download) ----------
# Given the STAR JOB_TAG (download tag + "_star") + its BAM_OUT, count aligned BAMs vs the sample list,
# note whether the launcher is still PENDing (waiting on the download) and whether a genome-index build
# is running, plus COMPLETE/STALLED + the watchdog tail (for an ETA). (raw string -> literal globs.)
_STAR_STATUS_PROBE = r'''TAG=%TAG%; BO=%BO%
echo "BAMOUT $BO"
if [ -d "$BO" ]; then
  L="$BO/sample_list.tsv"
  exp=0; [ -f "$L" ] && exp=$(grep -cve '^[[:space:]]*$' "$L")
  done=$(ls "$BO"/*.bam 2>/dev/null | grep -c .)
  echo "COUNT $exp $done"
fi
echo "---META---"
[ -n "$BO" ] && [ -f "$BO/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$BO" ] && [ -f "$BO/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$BO" ] && [ -f "$BO/PIPELINE_COMPLETE_PARTIAL.txt" ] && echo PARTIAL
[ -n "$BO" ] && [ -f "$BO/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$BO" ] && [ -f "$BO/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$BO" ] && [ -f "$BO/star/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "BUILD $(bjobs -noheader -o stat -J "${TAG}_staridx_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "---WATCHDOG---"
[ -n "$BO" ] && tail -n 40 "$BO/watchdog.log" 2>/dev/null
true'''


def parse_star_status(text):
    """Parse the STAR progress probe into a dict (unit-testable, no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    overall = None
    m = re.search(r"(?m)^COUNT\s+(\d+)\s+(\d+)", body)
    if m:
        exp, done = int(m.group(1)), int(m.group(2))
        overall = {"exp": exp, "done": done, "pct": round(100 * done / exp, 1) if exp else 0.0}
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    bd = re.search(r"(?m)^BUILD\s+(\d+)", meta)
    eta = _eta_from_watchdog(wd, {"exp": overall["exp"], "done": overall["done"]}) if overall else None
    return {"overall": overall, "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "building": bool(bd and int(bd.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "partial": "PARTIAL" in meta, "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "eta_seconds": eta, "raw": text[-2000:]}


def remote_star_status(host, user, port, keyfile, password, star_job_tag, bam_out=""):
    """SSH to the submit host and report STAR alignment progress for `star_job_tag` (= '<dlTag>_star').
    Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    cmd = (_STAR_STATUS_PROBE.replace("%TAG%", shq(star_job_tag or "sra_star"))
                             .replace("%BO%", shq(bam_out or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_star_status(text)
    parsed.update({"ok": True, "job_tag": star_job_tag})
    return parsed


# ---------- BAM->BED progress (the AltAnalyze junction/exon stage after STAR) ----------
# Given the BED JOB_TAG ('<dlTag>_star_bed') + STAR's BAM_OUT, count complete junction+exon BED PAIRS vs
# *.bam, note whether the launcher is still PENDing (waiting on STAR), plus COMPLETE/STALLED + the bed/
# watchdog tail (for an ETA). BED state lives in $BO/bed/. (raw string -> literal globs.)
_BED_STATUS_PROBE = r'''TAG=%TAG%; BO=%BO%; MODE=%MODE%
BEDDIR="$(dirname "$BO")/STAR_beds"
echo "BAMOUT $BO"
if [ -d "$BO" ]; then
  if [ -f "$BO/bed/bam_list.tsv" ]; then exp=$(grep -cve '^[[:space:]]*$' "$BO/bed/bam_list.tsv" 2>/dev/null); else exp=$(ls "$BO"/*.bam 2>/dev/null | grep -c .); fi
  done=$(ls "$BEDDIR"/*__junction.bed 2>/dev/null | while read -r j; do
           s="${j%__junction.bed}"; [ -s "$j" ] || continue
           case "$MODE" in
             exon) [ -s "${s}__exon.bed" ] && echo 1 ;;
             both) [ -s "${s}__exon.bed" ] && [ -e "${s}__intronJunction.bed" ] && echo 1 ;;
             *)    [ -e "${s}__intronJunction.bed" ] && echo 1 ;;
           esac
         done | grep -c .)
  echo "COUNT $exp $done"
fi
echo "---META---"
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_COMPLETE_PARTIAL.txt" ] && echo PARTIAL
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$BO" ] && [ -f "$BO/bed/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "---WATCHDOG---"
[ -n "$BO" ] && tail -n 40 "$BO/bed/watchdog.log" 2>/dev/null
true'''


def parse_bed_status(text):
    """Parse the BED progress probe into a dict (unit-testable, no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    overall = None
    m = re.search(r"(?m)^COUNT\s+(\d+)\s+(\d+)", body)
    if m:
        exp, done = int(m.group(1)), int(m.group(2))
        overall = {"exp": exp, "done": done, "pct": round(100 * done / exp, 1) if exp else 0.0}
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    eta = _eta_from_watchdog(wd, {"exp": overall["exp"], "done": overall["done"]}) if overall else None
    return {"overall": overall, "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "partial": "PARTIAL" in meta, "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "eta_seconds": eta, "raw": text[-2000:]}


def remote_bed_status(host, user, port, keyfile, password, bed_job_tag, bam_out="", bed_mode="intron"):
    """SSH to the submit host and report BAM->BED progress for `bed_job_tag` (= '<dlTag>_star_bed').
    `bed_mode` (intron|exon|both) selects which BED files count as 'done' (matches lib_bed.sh:bed_done).
    Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    _mode = (bed_mode or "intron").strip().lower()
    if _mode not in ("intron", "exon", "both"):
        _mode = "intron"
    cmd = (_BED_STATUS_PROBE.replace("%TAG%", shq(bed_job_tag or "sra_star_bed"))
                            .replace("%BO%", shq(bam_out or ""))
                            .replace("%MODE%", shq(_mode)))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_bed_status(text)
    parsed.update({"ok": True, "job_tag": bed_job_tag})
    return parsed


# ---------- AltAnalyze splicing (PSI) progress (the stage after BAM->BED) ----------
# Given the PSI JOB_TAG ('<dlTag>_psi') + its psi_root (= <download_root>/psi), note whether the PSI table
# was produced, whether the launcher is still PENDing (waiting on BED), plus COMPLETE/STALLED + the psi
# watchdog tail. PSI is a single AltAnalyze job, so there is no exp/done count -- just present-or-not.
_PSI_STATUS_PROBE = r'''TAG=%TAG%; PR=%PR%
echo "PSIROOT $PR"
if [ -n "$PR" ]; then
  if ls "$PR"/output/AltResults/AlternativeOutput/*EventAnnotation* >/dev/null 2>&1; then echo "PSITABLE 1"; else echo "PSITABLE 0"; fi
  # per-comparison dPSI files written vs requested (the per-sample table alone is NOT a finished run)
  C=0; for f in "$PR"/output/ExpressionInput/comps.*.txt; do [ -s "$f" ] && C=$(grep -c . "$f"); done
  P=0; for f in "$PR"/output/AltResults/AlternativeOutput/Events-dPSI_*/PSI.*_vs_*.txt*; do [ -e "$f" ] && P=$((P+1)); done
  echo "PSICOMPS $P $C"
fi
echo "---META---"
[ -n "$PR" ] && [ -f "$PR/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$PR" ] && [ -f "$PR/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$PR" ] && [ -f "$PR/ALTANALYZE_OK.txt" ] && echo CLEANEXIT
[ -n "$PR" ] && [ -f "$PR/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$PR" ] && [ -f "$PR/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$PR" ] && [ -f "$PR/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "JOBRUN $(bjobs -noheader -o stat -J "${TAG}_job" 2>/dev/null | grep -c RUN)"
echo "---WATCHDOG---"
[ -n "$PR" ] && tail -n 40 "$PR/watchdog.log" 2>/dev/null
true'''


def parse_psi_status(text):
    """Parse the PSI progress probe into a dict (unit-testable, no SSH)."""
    head, _, wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    table = bool(re.search(r"(?m)^PSITABLE\s+1", body))
    pc = re.search(r"(?m)^PSICOMPS\s+(\d+)\s+(\d+)", body)
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    jr = re.search(r"(?m)^JOBRUN\s+(\d+)", meta)
    return {"psi_table": table,
            "comparisons_written": int(pc.group(1)) if pc else None,
            "comparisons_requested": int(pc.group(2)) if pc else None,
            "clean_exit": "CLEANEXIT" in meta,
            "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "job_running": bool(jr and int(jr.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "raw": text[-2000:]}


def remote_psi_status(host, user, port, keyfile, password, psi_job_tag, psi_root=""):
    """SSH to the submit host and report AltAnalyze (PSI) progress for `psi_job_tag` (= '<dlTag>_psi').
    Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    cmd = (_PSI_STATUS_PROBE.replace("%TAG%", shq(psi_job_tag or "sra_psi"))
                            .replace("%PR%", shq(psi_root or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_psi_status(text)
    parsed.update({"ok": True, "job_tag": psi_job_tag})
    return parsed


_CONCORDANCE_STATUS_PROBE = r'''TAG=%TAG%; CR=%CR%
echo "CONCROOT $CR"
if [ -n "$CR" ]; then
  echo "NATLAS $(ls "$CR"/results/*/concordance.txt 2>/dev/null | wc -l)"
fi
echo "---META---"
[ -n "$CR" ] && [ -f "$CR/PIPELINE_COMPLETE.txt" ] && echo COMPLETE
[ -n "$CR" ] && [ -f "$CR/PIPELINE_STALLED.txt" ] && echo STALLED
[ -n "$CR" ] && [ -f "$CR/PIPELINE_INCOMPLETE_UPSTREAM.txt" ] && echo UPSTREAMPARTIAL
[ -n "$CR" ] && [ -f "$CR/PIPELINE_ORPHANED.txt" ] && echo ORPHANED
[ -n "$CR" ] && [ -f "$CR/PIPELINE_LAUNCH_TIMEOUT.txt" ] && echo LAUNCHTIMEOUT
echo "LIVE $(bjobs -noheader -o stat -J "${TAG}_*" 2>/dev/null | grep -cE 'RUN|PEND')"
echo "LAUNCHPEND $(bjobs -noheader -o stat -J "${TAG}_launch" 2>/dev/null | grep -c PEND)"
echo "JOBRUN $(bjobs -noheader -o stat -J "${TAG}_job" 2>/dev/null | grep -c RUN)"
echo "---WATCHDOG---"
[ -n "$CR" ] && tail -n 40 "$CR/watchdog.log" 2>/dev/null
true'''


def parse_concordance_status(text):
    """Parse the concordance progress probe into a dict (unit-testable, no SSH)."""
    head, _, _wd = text.partition("---WATCHDOG---")
    body, _, meta = head.partition("---META---")
    na = re.search(r"(?m)^NATLAS\s+(\d+)", body)
    lm = re.search(r"(?m)^LIVE\s+(\d+)", meta)
    lp = re.search(r"(?m)^LAUNCHPEND\s+(\d+)", meta)
    jr = re.search(r"(?m)^JOBRUN\s+(\d+)", meta)
    return {"n_atlases": int(na.group(1)) if na else None,
            "live_jobs": int(lm.group(1)) if lm else None,
            "launch_pending": bool(lp and int(lp.group(1)) > 0),
            "job_running": bool(jr and int(jr.group(1)) > 0),
            "complete": "COMPLETE" in meta, "stalled": "STALLED" in meta,
            "incomplete_upstream": "UPSTREAMPARTIAL" in meta,
            "orphaned": "ORPHANED" in meta, "launch_timeout": "LAUNCHTIMEOUT" in meta,
            "raw": text[-2000:]}


def remote_concordance_status(host, user, port, keyfile, password, concord_job_tag, concord_root=""):
    """SSH to the submit host and report splicing-concordance progress for `concord_job_tag`
    (= '<dlTag>_concordance'). Non-fatal (returns {"ok": False, ...} on SSH error)."""
    host = (host or "").strip(); user = (user or "").strip()
    if not host or not user:
        return {"ok": False, "error": "missing SSH host/user"}
    port = str(port or "22").strip() or "22"
    cmd = (_CONCORDANCE_STATUS_PROBE.replace("%TAG%", shq(concord_job_tag or "sra_concordance"))
                                    .replace("%CR%", shq(concord_root or "")))
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise _SubmitError("password auth needs paramiko", "")
            text = _ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            text = _ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    parsed = parse_concordance_status(text)
    parsed.update({"ok": True, "job_tag": concord_job_tag})
    return parsed


def main():
    import argparse
    import json
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--pipeline-root", default=None)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    sel = json.load(open(P.cellline_selection, encoding="utf-8")) if os.path.exists(P.cellline_selection) else {}
    cfg = {"PIPELINE_ROOT": a.pipeline_root} if a.pipeline_root else {}
    print(build_bundle(P, sel, cfg))


if __name__ == "__main__":
    main()
