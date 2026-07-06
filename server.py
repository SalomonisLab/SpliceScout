"""
Local web front end for the NCBI GEO RNA-seq pipeline.

    python server.py                 # serve on 127.0.0.1:8765 and open a browser
    python server.py --port 9000     # custom port
    python server.py --no-open       # don't auto-open the browser
    python server.py --host 0.0.0.0  # expose on the LAN (use with care: keys are posted)

A single-page UI collects everything a run needs — the GEO search query, whether to scan
ALL matching studies or cap at N, the Anthropic API key (or skip AI cleaning), an optional
NCBI key, model, and concurrency — then starts the pipeline in a background thread and shows
a live 7-stage progress stepper with a progress bar, an ETA, and a streaming log. When the
run finishes it lists the output tables for download.

Stdlib only (http.server) plus the existing pipeline modules — no web framework needed.
Runs ONE pipeline at a time; the UI blocks starting a second run while one is active.
"""
import argparse
import atexit
import hmac
import io
import json
import os
import re
import secrets as pysecrets
import sys
import threading
import traceback
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from pipeline_paths import Paths
from progress import RunReporter
import progress
import pipeline
import llm_providers
import cluster_deploy
import plot_data
import stage_docs
import chat_assist

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- shared state: exactly one active run at a time ----
_LOCK = threading.Lock()
_REPORTER = None     # current/last RunReporter
_WORKER = None       # current worker thread
_RUN_DIR = None      # current/last run dir (download root)

# ---- access control (the server is a remote-control plane: /api/start launches cluster runs) ----
# On the default 127.0.0.1 bind, only the CSRF Origin check applies (blocks drive-by browser POSTs).
# On a non-loopback bind (--host 0.0.0.0 / a LAN IP) a random token is REQUIRED on every request: the
# banner prints the URL with ?token=..., the served page embeds it, and /api/* verify it.
_AUTH_TOKEN = ""     # "" => loopback bind, no token required
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def _host_of(value):
    """Extract the bare host from a URL / Origin / Host header (no scheme, port, path, or userinfo)."""
    h = (value or "").strip().lower()
    if "//" in h:
        h = h.split("//", 1)[1]
    h = h.rsplit("@", 1)[-1]
    h = h.split("/", 1)[0]
    if h.startswith("["):                       # bracketed IPv6
        return h.split("]", 1)[0] + "]"
    return h.split(":", 1)[0]

# ---- per-instance identity: each launched server claims a cluster JOB_TAG ----
# The launcher prompts for an instance NAME and exports it as $SPLICESCOUT_INSTANCE; that name
# becomes this instance's cluster JOB_TAG, so concurrent projects' LSF jobs never collide. With no
# name given it falls back to the lowest free sra1 / sra2 / sra3 ... among LIVE instances (a
# closed/dead instance frees its tag). The tag is claimed atomically via a per-tag lock file.
_INSTANCE_TAG = "sra1"
_INSTANCE_LOCK_PATH = None


def _instances_dir():
    return os.path.join(os.path.expanduser("~"), ".geo_pipeline_instances")


def _sanitize_tag(s):
    """Make a user-supplied instance name safe both as an LSF job-name prefix AND as a literal inside
    the `grep -E "^<tag>_..."` patterns the cluster scripts use: keep [A-Za-z0-9_], collapse any run
    of other characters to a single '_', trim leading/trailing '_', cap length. '' if nothing usable."""
    import re
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(s or "")).strip("_")[:24]


def _resolve_instance_name():
    """The user-chosen instance name from the launcher (env var $SPLICESCOUT_INSTANCE), or None for
    the automatic sraN fallback. The launcher prompts and exports it because server.py may run
    windowless (under the system tray) and so must never block on input()."""
    return (os.environ.get("SPLICESCOUT_INSTANCE") or "").strip() or None


def _pid_alive(pid):
    """True if a process with this PID is currently running (cross-platform, no extra deps)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return code.value == 259                # STILL_ACTIVE
        except Exception:
            return True                             # uncertain -> don't steal the slot
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _claim_instance_slot(preferred=None):
    """Claim a unique instance identity (an atomic lock file in ~/.geo_pipeline_instances) and return
    (tag, lock_path). With `preferred` (a user-chosen name) the tag is that sanitized name — or
    name-2 / name-3 / ... if a LIVE instance already holds it; with no name it's the lowest free
    sra1 / sra2 / sra3 ... among live instances. A dead instance's tag is reclaimed."""
    regdir = _instances_dir()
    base = _sanitize_tag(preferred) if preferred else ""
    try:
        os.makedirs(regdir, exist_ok=True)
    except Exception:
        return (base or "sra1"), None
    if base:
        candidates = (base if i == 1 else f"{base}-{i}" for i in range(1, 1000))
    else:
        candidates = (f"sra{n}" for n in range(1, 1000))
    for tag in candidates:
        path = os.path.join(regdir, f"{tag}.lock")
        if os.path.exists(path):
            alive = True
            try:
                alive = _pid_alive(json.load(open(path, encoding="utf-8")).get("pid"))
            except Exception:
                alive = False
            if alive:
                continue                 # tag held by a live instance
            try:
                os.remove(path)          # stale (instance died) -> reclaim it
            except Exception:
                continue
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # atomic claim (race-safe)
        except FileExistsError:
            continue                     # another instance grabbed it a moment ago
        except Exception:
            return tag, None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "tag": tag}, f)
        except Exception:
            pass
        return tag, path
    return (base or "sra1"), None


def _release_instance_slot():
    if _INSTANCE_LOCK_PATH:
        try:
            os.remove(_INSTANCE_LOCK_PATH)
        except Exception:
            pass


def _cluster_tag_busy(host, port, user, keyfile, base_root, tag):
    """Best-effort, key/agent SSH only: True if <base_root>/<tag> on the cluster has a PIPELINE_COMPLETE.txt
    (a finished prior run) OR live <tag>_* LSF jobs (a concurrent run); False if free; None if the SSH probe
    is unavailable/failed (so the caller treats 'unknown' as 'don't bump')."""
    root = base_root.rstrip("/") + "/" + tag
    probe = ("R=%s; T=%s; d=0; [ -f \"$R/PIPELINE_COMPLETE.txt\" ] && d=1; "
             "L=$(bjobs -noheader -o stat -J \"${T}_*\" 2>/dev/null | grep -E 'RUN|PEND' | wc -l); "
             "echo OCC $d ${L:-0}") % (cluster_deploy.shq(root), cluster_deploy.shq(tag))
    try:
        txt = cluster_deploy._ssh_capture_systemssh(host, port, user, keyfile, probe, timeout=25)
    except Exception:
        return None
    m = re.search(r"OCC\s+(\d+)\s+(\d+)", txt or "")
    if not m:
        return None
    return (int(m.group(1)) > 0) or (int(m.group(2)) > 0)


def _claim_cluster_aware(preferred):
    """Claim an instance tag that is free BOTH locally (no live PC instance, via _claim_instance_slot) AND on
    the cluster (no COMPLETED folder / live jobs for it) — so a fresh launch doesn't clobber a finished run or
    collide with a running one (the A549-onto-existing-A549 bug). BULLETPROOF: any failure (no saved creds,
    SSH down, unreachable cluster) falls back to the plain local claim so the server ALWAYS starts. Uses the
    SAVED cluster creds + PIPELINE_ROOT; only catches the common case where the instance name == the folder
    name (e.g. 'A549'), since the cell line that scopes other names isn't known until the run resolves it."""
    tag, lock = _claim_instance_slot(preferred)
    try:
        cl = (_load_settings().get("cluster") or {})
        host = (cl.get("ssh_host") or "").strip(); user = (cl.get("ssh_user") or "").strip()
        base = (cl.get("PIPELINE_ROOT") or "").strip()
        if not (host and user and base):
            return tag, lock                       # no saved cluster creds -> plain local claim
        port = str(cl.get("ssh_port") or "22").strip() or "22"; keyfile = (cl.get("ssh_key") or "").strip()
        if _cluster_tag_busy(host, port, user, keyfile, base, tag) is not True:
            return tag, lock                       # free, or SSH unavailable -> keep it
        base_name = tag
        for i in range(2, 40):
            t2, l2 = _claim_instance_slot(f"{base_name}-{i}")
            if _cluster_tag_busy(host, port, user, keyfile, base, t2) is not True:
                try:
                    if lock:
                        os.remove(lock)             # drop the original (occupied) claim
                except Exception:
                    pass
                print(f"   NOTE: cluster folder/jobs for \"{base_name}\" already exist -> this instance is "
                      f"\"{t2}\" so a fresh run won't clobber/collide (override with --instance).")
                return t2, l2
            try:
                if l2:
                    os.remove(l2)                   # candidate also busy -> release + try the next
            except Exception:
                pass
    except Exception as e:
        print(f"   (cluster-aware instance check skipped: {e!r})")
    return tag, lock


def _port_in_use(host, port):
    """True if something is already listening on host:port. Probe by CONNECT, not by bind —
    HTTPServer sets allow_reuse_address, and on Windows that lets two sockets share a port, so a
    failed bind can't detect an in-use port; a successful connect always can."""
    import socket
    h = "127.0.0.1" if host in ("0.0.0.0", "") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((h, port))
        return True            # someone is listening here
    except OSError:
        return False
    finally:
        s.close()


def _bind_server(host, start_port, tries=64):
    """Bind the first free port at/after start_port, so every launch gets its own instance."""
    last = None
    for port in range(start_port, start_port + tries):
        if _port_in_use(host, port):
            continue
        try:
            return ThreadingHTTPServer((host, port), Handler), port
        except OSError as e:
            last = e          # lost a race for this port -> try the next
    raise last or OSError("no free port found")


class _Tee(io.TextIOBase):
    """Write-through to the original stream AND to the reporter log (line-buffered)."""

    def __init__(self, orig, reporter):
        self.orig = orig
        self.reporter = reporter
        self._buf = ""

    def write(self, s):
        try:
            self.orig.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.reporter.log(line)
        return len(s)

    def flush(self):
        try:
            self.orig.flush()
        except Exception:
            pass


def _worker(cfg, P, reporter, secrets=None):
    """Run the pipeline, teeing all stdout/stderr into the live log."""
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = _Tee(old_out, reporter)
    sys.stderr = _Tee(old_err, reporter)
    try:
        pipeline.run_pipeline(cfg, P, reporter=reporter, secrets=secrets)
    except Exception as e:
        reporter.log("ERROR: " + repr(e))
        reporter.log(traceback.format_exc())
        reporter.fail(e)
    finally:
        sys.stdout, sys.stderr = old_out, old_err


# Reject values that could break out of remote-shell quoting before they reach the cluster. shq()
# already neutralizes these in command lines, but a boundary check gives the user a clear 400 instead
# of a mangled remote command, and defends the config.sh / generated-script paths too. Permits $USER,
# spaces and ordinary path chars; blocks quotes, backtick, ; | & < > \ newline and $( command-subst.
_SHELL_META_RE = re.compile(r"""[`;|&<>'"\\\n\r]|\$\(""")


def _reject_shell_meta(named):
    """named: iterable of (label, value). Returns (400, {...}) if any value carries shell
    metacharacters that could escape remote-command quoting; else None."""
    for label, val in named:
        s = str(val or "")
        if _SHELL_META_RE.search(s):
            return 400, {"error": "%s may not contain quotes, backtick, ; | & < > \\ newline or $( ) "
                                  "shell characters." % label}
    return None


def _start_run(body):
    """Validate the posted config, create a run dir, and launch the worker."""
    global _REPORTER, _WORKER, _RUN_DIR
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return 409, {"error": "A run is already in progress."}

        query = (body.get("query") or "").strip()
        if not query:
            return 400, {"error": "A search query is required."}

        scope = (body.get("scope") or "capped").strip()
        if scope == "all":
            cap = "unlimited"
        else:
            try:
                cap = int(body.get("cap", 25))
                if cap < 1:
                    raise ValueError
            except Exception:
                return 400, {"error": "Cap must be a positive whole number."}

        skip_ai = bool(body.get("skip_ai"))
        provider = llm_providers.normalize_provider(body.get("provider"))
        api_key = (body.get("api_key") or "").strip()
        ncbi_key = (body.get("ncbi_key") or "").strip() or None
        model = (body.get("model") or "").strip() or llm_providers.DEFAULT_MODEL[provider]
        base_url = (body.get("base_url") or "").strip()   # custom OpenAI-compatible endpoint (optional)
        disable_reasoning = bool(body.get("disable_reasoning"))   # turn off thinking for reasoning models (MiMo)
        deep_dive = bool(body.get("deep_dive", True))
        pick_mode = "manual" if (body.get("pick_mode") == "manual") else "auto"
        module = (body.get("module") or "bulk_rna_seq").strip() or "bulk_rna_seq"
        star_in = body.get("star") or {}
        _sk = ("GENOME_DIR", "SJDB_GTF", "STAR_INDEX_ROOT", "ORGANISM", "THREADS", "MEM_MB", "WALL", "DELETE_FASTQ_AFTER_BAM")
        star_cfg = {k: str(star_in.get(k)).strip() for k in _sk if str(star_in.get(k) or "").strip()} or None
        bed_in = body.get("bed") or {}
        _bk = ("ALTANALYZE_DIR", "SPECIES", "ORGANISM", "MEM_MB", "WALL", "enabled", "BED_MODE", "DELETE_BAM_AFTER_BED")
        bed_cfg = {k: str(bed_in.get(k)).strip() for k in _bk if str(bed_in.get(k) or "").strip()} or None
        psi_in = body.get("psi") or {}
        _pk = ("ALTANALYZE_HOME", "ALTANALYZE_DB", "ALTANALYZE_LOCAL", "SPECIES", "ORGANISM", "EXPNAME",
               "MEM_MB", "WALL", "RUN_GOELITE", "COMPRESS_WHEN_DONE", "enabled")
        psi_cfg = {k: str(psi_in.get(k)).strip() for k in _pk if str(psi_in.get(k) or "").strip()} or None
        concordance_in = body.get("concordance") or {}
        _ck = ("CANCER_ATLAS", "DPSI", "RAWP", "REMOVE_IR", "CONC_THRESHOLD", "MEM_MB", "WALL", "enabled")
        concordance_cfg = {k: str(concordance_in.get(k)).strip() for k in _ck if str(concordance_in.get(k) or "").strip()} or None
        # user-defined comparison groups (Phase B): [{name, control?, match:[...]}, ...] + compared pair
        group_in = body.get("groups") if isinstance(body.get("groups"), dict) else None
        group_cfg = group_in or None
        # phase range: validate start/end against the canonical stage order + check supplied inputs exist
        _order = [k for k, _ in progress.STAGES]
        start_stage = (body.get("start_stage") or "fetch").strip() or "fetch"
        end_stage = (body.get("end_stage") or "concordance_submit").strip() or "concordance_submit"
        if start_stage not in _order or end_stage not in _order:
            return 400, {"error": "unknown start/end stage"}
        if _order.index(start_stage) > _order.index(end_stage):
            return 400, {"error": "start stage is after end stage"}
        supplied_inputs = body.get("supplied_inputs") if isinstance(body.get("supplied_inputs"), dict) else {}
        _scp = next((c for c in progress.CHECKPOINTS if c["stage"] == start_stage), None)
        if _scp:
            for _spec in _scp.get("inputs", []):
                _p = str(supplied_inputs.get(_spec["field"]) or "").strip()
                if not _p:
                    if _spec.get("optional"):
                        continue
                    return 400, {"error": "start at '%s' needs %s" % (_scp["label"], _spec["label"])}
                if not os.path.exists(os.path.expanduser(_p)):
                    return 400, {"error": "supplied path not found: %s" % _p}
        try:
            concurrency = max(1, int(body.get("concurrency", 8)))   # no upper cap on concurrent AI batches
        except Exception:
            concurrency = 8
        # per-model max OUTPUT tokens for the AI cleaning calls (UI: model_max_tokens = {model: int}).
        # Blank/unset -> 60000 (the long-standing default). Raise it for verbose reasoning models so a
        # chain-of-thought doesn't truncate the tool call; lower it to cut cost / stay under a TPM limit.
        max_tokens = 60000
        try:
            _mt = (body.get("model_max_tokens") or {}).get(model) or body.get("max_tokens")
            if str(_mt or "").strip():
                max_tokens = max(256, int(_mt))
        except Exception:
            max_tokens = 60000

        if not skip_ai:
            if api_key:
                os.environ[llm_providers.KEY_ENV[provider]] = api_key
            elif not llm_providers.have_key(provider):
                return 400, {"error": f"An API key for {llm_providers.PROVIDER_LABEL[provider]} "
                                      "is required (or tick 'Skip AI cleaning')."}

        # ----- cluster handoff -----
        cluster_mode = (body.get("cluster_mode") or "off").strip()
        if not deep_dive:
            cluster_mode = "off"   # no per-study lists without the deep dive
        cluster_in = body.get("cluster") or {}
        cluster_cfg = None
        secrets = {}
        if cluster_mode != "off":
            if not (cluster_in.get("PIPELINE_ROOT") or "").strip():
                return 400, {"error": "Cluster PIPELINE_ROOT (the path on the cluster) is required."}
            if cluster_mode == "autonomous" and not (
                    (cluster_in.get("ssh_host") or "").strip()
                    and (cluster_in.get("ssh_user") or "").strip()):
                return 400, {"error": "Autonomous mode needs an SSH host and username."}
            keys = ["PIPELINE_ROOT", "SCRATCH_DIR", "LSF_QUEUE", "SRATOOLKIT_MODULE", "ASPERA_MODULE",
                    "THREADS", "MEM_MB", "WALL", "PREFETCH_MEM_MB", "WATCHDOG_INTERVAL_MIN", "JOB_TAG",
                    "ssh_host", "ssh_user", "ssh_port", "ssh_key"]
            cluster_cfg = {k: str(cluster_in.get(k)).strip()
                           for k in keys if str(cluster_in.get(k) or "").strip()}
            cluster_cfg.setdefault("JOB_TAG", _INSTANCE_TAG)   # auto per-instance tag if left blank
            # boundary check: these flow into remote ssh/bsub command lines + the generated config.sh
            _bad = _reject_shell_meta(
                [("Cluster PIPELINE_ROOT", cluster_cfg.get("PIPELINE_ROOT")),
                 ("Job tag", cluster_cfg.get("JOB_TAG")),
                 ("LSF queue", cluster_cfg.get("LSF_QUEUE")),
                 ("Scratch dir", cluster_cfg.get("SCRATCH_DIR")),
                 ("SRA toolkit module", cluster_cfg.get("SRATOOLKIT_MODULE")),
                 ("Aspera module", cluster_cfg.get("ASPERA_MODULE")),
                 ("SSH host", cluster_cfg.get("ssh_host")),
                 ("SSH user", cluster_cfg.get("ssh_user")),
                 ("SSH key path", cluster_cfg.get("ssh_key"))]
                + [("STAR " + k, (star_cfg or {}).get(k)) for k in ("GENOME_DIR", "STAR_INDEX_ROOT", "ORGANISM")]
                + [("BED " + k, (bed_cfg or {}).get(k)) for k in ("ALTANALYZE_DIR", "SPECIES", "ORGANISM")]
                + [("PSI " + k, (psi_cfg or {}).get(k)) for k in ("ALTANALYZE_HOME", "ALTANALYZE_DB", "SPECIES", "ORGANISM", "EXPNAME")]
                + [("Concordance " + k, (concordance_cfg or {}).get(k)) for k in ("CANCER_ATLAS",)])
            if _bad:
                return _bad
            pw = body.get("ssh_password") or ""      # secret -> memory for the run; saved only if remembered
            if pw:
                secrets["ssh_password"] = pw

        # The STAR/BED/PSI "also run" checkboxes are AUTHORITATIVE for the analysis chain: when the phase
        # range already reaches that chain (end >= star_submit), a TICKED trailing stage EXTENDS end_stage
        # to include it (never shrinks). Without this, a stale saved end_stage (e.g. "bed_submit" from
        # before the PSI stage existed) silently skips PSI even with its box ticked. A run that ends BEFORE
        # the chain (download-only / stop-at-STAR via the slider) is untouched; uncheck a box to stop earlier.
        if module == "bulk_rna_seq" and cluster_mode != "off" and end_stage in ("star_submit", "bed_submit", "psi_submit", "concordance_submit"):
            _bed_en = str((bed_cfg or {}).get("enabled", "1")).lower() not in ("0", "off", "false", "no")
            _psi_en = str((psi_cfg or {}).get("enabled", "1")).lower() not in ("0", "off", "false", "no")
            _conc_en = str((concordance_cfg or {}).get("enabled", "1")).lower() not in ("0", "off", "false", "no")
            _target = end_stage
            if _bed_en and _order.index("bed_submit") > _order.index(_target):
                _target = "bed_submit"
            if _psi_en and _order.index("psi_submit") > _order.index(_target):
                _target = "psi_submit"
            if _psi_en and _conc_en and _order.index("concordance_submit") > _order.index(_target):
                _target = "concordance_submit"
            if _order.index(_target) > _order.index(end_stage):
                print(f"  PHASE RANGE: end '{end_stage}' -> '{_target}' (enabled analysis toggles extend the range)")
                end_stage = _target

        _save_settings(body)   # remember these inputs locally for the next launch

        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40].strip("-") or "run"
        # tag the run dir with this instance so concurrent instances never share a run folder
        run_dir = os.path.join("runs", f"{slug}_{datetime.now():%Y%m%d-%H%M%S}_{_INSTANCE_TAG}")
        P = Paths(run_dir).ensure_dirs()
        cfg = pipeline.RunConfig(query=query, cap=cap, ncbi_key=ncbi_key, model=model,
                                 provider=provider, base_url=base_url, disable_reasoning=disable_reasoning,
                                 max_tokens=max_tokens, module=module,
                                 concurrency=concurrency, run_dir=P.run_dir, skip_ai=skip_ai,
                                 deep_dive=deep_dive, pick_mode=pick_mode,
                                 cluster_mode=cluster_mode, cluster_cfg=cluster_cfg, star_cfg=star_cfg,
                                 bed_cfg=bed_cfg, psi_cfg=psi_cfg, concordance_cfg=concordance_cfg, group_cfg=group_cfg,
                                 start_stage=start_stage, end_stage=end_stage, supplied_inputs=supplied_inputs)
        # config.json mirrors the CLI (ncbi_key + cluster_cfg stored; Anthropic key + SSH password NOT)
        with open(P.config, "w", encoding="utf-8") as f:
            json.dump(asdict(cfg), f, indent=2)

        reporter = RunReporter(run_dir=P.run_dir)
        _REPORTER = reporter
        _RUN_DIR = P.run_dir
        t = threading.Thread(target=_worker, args=(cfg, P, reporter, secrets), daemon=True)
        _WORKER = t
        t.start()
        return 200, {"ok": True, "run_dir": P.run_dir}


def _latest_run_dir():
    """The most recent runs/* dir for THIS instance (so a server RESTART resumes the right run). None if none."""
    try:
        cand = []
        for n in os.listdir("runs"):
            d = os.path.join("runs", n)
            if (os.path.isdir(d) and n.endswith("_" + _INSTANCE_TAG)
                    and os.path.isfile(os.path.join(d, "config.json"))):
                cand.append((os.path.getmtime(d), d))
        cand.sort()
        return cand[-1][1] if cand else None
    except Exception:
        return None


def _resume_run(body):
    """Re-attach + CONTINUE an existing run dir instead of minting a NEW one (the gap that made a server
    restart un-resumable: /api/start always created a fresh runs/<...> folder). Reconstructs RunConfig from
    the run's saved config.json; the pipeline's per-stage done-checks (begin()) skip completed stages, so it
    resumes from where it stopped. Run dir = body['run_dir'] if given, else _RUN_DIR, else this instance's
    most recent runs/* dir. Resume may also OVERRIDE the AI provider/model/base_url or turn AI off -- handy
    when the configured OpenAI-compatible proxy is down (switch to a local Ollama, or skip AI, and continue)."""
    global _REPORTER, _WORKER, _RUN_DIR
    with _LOCK:
        if _WORKER and _WORKER.is_alive():
            return 409, {"error": "A run is already in progress."}
        rd = (body.get("run_dir") or "").strip() or _RUN_DIR or _latest_run_dir()
        if not rd or not os.path.isdir(rd):
            return 404, {"error": "No existing run found to resume — start a run first."}
        cfgpath = os.path.join(rd, "config.json")
        if not os.path.isfile(cfgpath):
            return 400, {"error": f"No config.json in {rd} — cannot resume."}
        try:
            cfg = pipeline.RunConfig(**json.load(open(cfgpath, encoding="utf-8")))
        except Exception as e:
            return 400, {"error": f"Could not load the saved run config ({e})."}
        P = Paths(rd).ensure_dirs()
        # optional fresh overrides on resume (e.g. the AI proxy is down -> switch provider / skip AI)
        if (body.get("provider") or "").strip():
            cfg.provider = llm_providers.normalize_provider(body.get("provider"))
        if (body.get("model") or "").strip():
            cfg.model = body.get("model").strip()
        if "base_url" in body:
            cfg.base_url = (body.get("base_url") or "").strip()
        if body.get("disable_reasoning") is not None:
            cfg.disable_reasoning = bool(body.get("disable_reasoning"))
        if body.get("skip_ai"):
            cfg.skip_ai = True
        secrets = {}
        if body.get("api_key"):
            secrets["api_key"] = body.get("api_key")
        pw = (body.get("ssh_password") or _load_settings().get("ssh_password") or "")
        if pw:
            secrets["ssh_password"] = pw
        try:                                  # persist overrides so a further restart resumes with them
            json.dump(asdict(cfg), open(P.config, "w", encoding="utf-8"), indent=2)
        except Exception as e:
            print(f"  WARN: could not persist resumed config ({e})")
        reporter = RunReporter(run_dir=P.run_dir)
        _REPORTER = reporter
        _RUN_DIR = P.run_dir
        t = threading.Thread(target=_worker, args=(cfg, P, reporter, secrets), daemon=True)
        _WORKER = t
        t.start()
        print(f"Resuming run {P.run_dir} (provider={cfg.provider}, skip_ai={cfg.skip_ai})")
        return 200, {"ok": True, "run_dir": P.run_dir, "resumed": True}


def _status():
    with _LOCK:
        if _REPORTER is None:
            return {"state": "idle"}
        return _REPORTER.snapshot()


# ---- local settings: remember the user's inputs (incl. keys + cluster info) between launches ----
def _settings_path():
    return os.path.join(os.path.expanduser("~"), ".geo_pipeline_settings.json")


def _load_settings():
    try:
        return json.load(open(_settings_path(), encoding="utf-8"))
    except Exception:
        return {}


# The live cluster password is NEVER served over HTTP (it's not even persisted anymore).
_NEVER_SERVE_KEYS = ("ssh_password",)
# API/NCBI keys are withheld ONLY on a non-loopback (token) bind — don't push them across the network
# even to a token holder. On the default 127.0.0.1 bind they ARE served so the UI prefills: the keys
# already live in the local plaintext settings file, so loopback HTTP adds no exposure beyond that.
_NETWORK_SECRET_KEYS = ("api_keys", "api_key", "ncbi_key")


def _public_settings():
    """Saved settings for UI prefill. Always strips the live SSH password; on a non-loopback bind also
    strips the API/NCBI keys (the token gate is the protection there). On loopback the keys are served
    so the form prefills exactly as before."""
    s = dict(_load_settings())
    strip = list(_NEVER_SERVE_KEYS)
    if _AUTH_TOKEN:            # non-loopback bind -> also withhold API/NCBI keys from the network
        strip += list(_NETWORK_SECRET_KEYS)
    for k in strip:
        s.pop(k, None)
    if isinstance(s.get("cluster"), dict):
        s["cluster"] = {k: v for k, v in s["cluster"].items() if k != "ssh_password"}
    return s


def _save_settings(body):
    """Persist form inputs locally so they prefill next launch. The SSH password is NEVER written to
    disk (it lives only in the in-memory secrets dict for the active run); the file is written 0600 +
    atomically. API/NCBI keys are still persisted for prefill but are stripped from /api/settings."""
    # NOTE: start_stage/end_stage are deliberately NOT persisted -- the phase range always launches at its
    # largest (full pipeline) and is a per-run override, never a saved preference.
    keep = {k: body[k] for k in
            ("query", "scope", "cap", "skip_ai", "provider", "api_keys", "ncbi_key", "model",
             "model_history", "base_urls", "base_url", "disable_reasoning", "model_max_tokens",
             "module", "concurrency", "pick_mode", "cluster_mode", "alert_email",
             "diagnose_model_path", "diagnose_model_dir") if k in body}
    # Strip whitespace from secrets BEFORE persisting — a stray leading/trailing space or TAB from a paste
    # makes the gateway reject the key (e.g. LiteLLM master-key auth fails -> the confusing "No connected db").
    if isinstance(keep.get("api_keys"), dict):
        keep["api_keys"] = {p: (v.strip() if isinstance(v, str) else v) for p, v in keep["api_keys"].items()}
    for _kk in ("ncbi_key", "base_url"):
        if isinstance(keep.get(_kk), str):
            keep[_kk] = keep[_kk].strip()
    if isinstance(keep.get("base_urls"), dict):
        keep["base_urls"] = {p: (v.strip() if isinstance(v, str) else v) for p, v in keep["base_urls"].items()}
    if isinstance(body.get("cluster"), dict):
        keep["cluster"] = {k: v for k, v in body["cluster"].items() if k != "ssh_password"}
    if isinstance(body.get("star"), dict):
        keep["star"] = body["star"]
    if isinstance(body.get("bed"), dict):
        keep["bed"] = body["bed"]
    if isinstance(body.get("psi"), dict):
        keep["psi"] = body["psi"]
    if isinstance(body.get("concordance"), dict):
        keep["concordance"] = body["concordance"]
    if isinstance(body.get("groups"), dict):
        keep["groups"] = body["groups"]
    try:
        merged = dict(_load_settings())   # overlay onto existing so a partial save never drops other settings
        merged.update(keep)
        p = _settings_path()
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, p)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "GeoPipeline/1.0"

    def log_message(self, *args):
        pass  # quiet: 1s status polling would flood the console

    # -- helpers --
    def _send_json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, name):
        with _LOCK:
            run_dir = _RUN_DIR
        if not run_dir:
            return self._send_json(404, {"error": "no run yet"})
        base = os.path.basename(name)
        path = None
        run_real = os.path.realpath(run_dir)
        for sub in ("tables", "runtable"):       # search both output dirs by basename
            cand = os.path.realpath(os.path.join(run_dir, sub, base))
            # containment guard (defense in depth beyond basename): never serve outside the run dir
            if (base and os.path.isfile(cand)
                    and os.path.commonpath([run_real, cand]) == run_real):
                path = cand
                break
        if not path:
            return self._send_json(404, {"error": "file not found"})
        ctype = ("text/csv" if base.endswith(".csv")
                 else "text/markdown" if base.endswith(".md")
                 else "text/plain" if base.endswith(".txt")
                 else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                 if base.endswith(".xlsx")
                 else "application/zip" if base.endswith(".zip")
                 else "application/json" if base.endswith(".json")
                 else "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{base}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_static(self, relpath, ctype):
        """Serve a file from the project dir (vendored assets) — separate from run-dir downloads."""
        path = os.path.join(HERE, relpath)
        if not os.path.isfile(path):
            return self._send_json(404, {"error": "not found"})
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _send_readme(self):
        """Render README.md as a minimal dark page (preformatted; no markdown deps)."""
        try:
            md = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
        except Exception:
            md = "README.md not found."
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'><title>SpliceScout — User Guide</title>"
                "<style>body{background:#0f1420;color:#e7ecf5;font:14px/1.6 ui-monospace,Consolas,"
                "monospace;max-width:920px;margin:0 auto;padding:32px 22px}a{color:#5fb6c0}"
                "pre{white-space:pre-wrap;word-wrap:break-word}</style></head><body><pre>"
                + _html_attr(md) + "</pre></body></html>")
        self._send_html(html)

    # -- access control --
    def _token_ok(self):
        """True if no token is required (loopback bind) OR the request carries the right token."""
        if not _AUTH_TOKEN:
            return True
        tok = self.headers.get("X-Auth-Token") or ""
        if not tok:
            tok = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        return hmac.compare_digest(str(tok), _AUTH_TOKEN)

    def _csrf_ok(self):
        """Block browser CROSS-ORIGIN state-changing requests. A non-browser client (no Origin/Referer)
        is not a CSRF vector and is allowed; a browser request is allowed only if its Origin host is
        loopback or matches the Host we were reached on (same-origin)."""
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        if not origin:
            return True
        oh = _host_of(origin)
        if oh in _LOOPBACK_HOSTS:
            return True
        return bool(oh) and oh == _host_of("//" + (self.headers.get("Host") or ""))

    def _guard(self, csrf=False):
        """Enforce token (non-loopback bind) and, for state changes, the CSRF origin check.
        Returns True if the request may proceed; otherwise sends the error response and returns False."""
        if not self._token_ok():
            self._send_json(401, {"error": "missing or invalid auth token (see the SpliceScout console "
                                            "banner for the URL that includes ?token=...)"})
            return False
        if csrf and not self._csrf_ok():
            self._send_json(403, {"error": "cross-origin request blocked"})
            return False
        return True

    # -- routes --
    def do_GET(self):
        route = urlparse(self.path)
        if route.path in ("/", "/index.html"):
            if not self._token_ok():
                return self._send_json(401, {"error": "missing or invalid auth token — open the URL "
                                                      "printed in the SpliceScout console (it includes ?token=...)"})
            return self._send_html(_page())
        if route.path.startswith("/api/") and not self._token_ok():
            return self._send_json(401, {"error": "missing or invalid auth token"})
        if route.path == "/api/status":
            return self._send_json(200, _status())
        if route.path == "/api/settings":
            return self._send_json(200, _public_settings())
        if route.path == "/api/file":
            q = parse_qs(route.query)
            return self._send_file(q.get("name", [""])[0])
        if route.path == "/plotly.js":
            return self._send_static("vendor/plotly.min.js", "application/javascript; charset=utf-8")
        if route.path == "/readme":
            return self._send_readme()
        if route.path == "/api/plotdata":
            with _LOCK:
                run_dir = _RUN_DIR
            if not run_dir:
                return self._send_json(200, {"available": False, "samples": [], "studies": []})
            return self._send_json(200, plot_data.build_plot_data(Paths(run_dir)))
        if route.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self._send_json(404, {"error": "not found"})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_POST(self):
        route = urlparse(self.path)
        if not self._guard(csrf=True):     # token (non-loopback) + CSRF origin check on every state change
            return
        if route.path == "/api/shutdown":
            # Stop the WHOLE program. Respond FIRST, then terminate from a background thread. We must
            # os._exit() (a HARD exit): self.server.shutdown() alone only ends the HTTP accept loop, but
            # under the system-tray launcher (tray.py) the MAIN thread is the pystray icon loop and
            # serve_forever() runs in a daemon thread -- so stopping the server leaves the tray (and thus
            # the process) alive. os._exit kills the tray loop + serve_forever + any worker. Cluster runs
            # are self-driving and keep going on their own.
            self._send_json(200, {"ok": True, "message": "SpliceScout server shutting down"})
            try:
                self.wfile.flush()
            except Exception:
                pass

            def _stop():
                try:
                    self.server.shutdown()       # end the accept loop cleanly (also lets the 200 flush)
                except Exception:
                    pass
                try:
                    _release_instance_slot()     # free this instance's tag (os._exit skips atexit)
                except Exception:
                    pass
                os._exit(0)                      # HARD-exit the entire process (tray icon loop included)

            threading.Thread(target=_stop, daemon=True).start()
            return
        if route.path == "/api/settings":          # persist UI prefs (per-provider model history etc.) sans run
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            _save_settings(body)
            return self._send_json(200, {"ok": True})
        if route.path == "/api/chat":          # the Assistant — agentic chat over the pipeline (prepare-only)
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            messages = body.get("messages")
            if not isinstance(messages, list):
                return self._send_json(400, {"error": "messages must be a list of {role, content}"})
            # run_turn never raises (it self-diagnoses into {error,reply}); pass the save callback so the
            # assistant's update_settings writes to the SAME local store as the form.
            return self._send_json(200, chat_assist.run_turn(messages, _load_settings(),
                                                             _save_settings, _INSTANCE_TAG))
        if route.path == "/api/start":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            code, obj = _start_run(body)
            return self._send_json(code, obj)
        if route.path == "/api/resume":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            code, obj = _resume_run(body)
            return self._send_json(code, obj)
        if route.path == "/api/select":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            with _LOCK:
                rep = _REPORTER
            cell_line = (body.get("cell_line") or "").strip() or None   # None => auto-pick best
            ok = rep.provide_selection(cell_line) if rep else False
            return self._send_json(200 if ok else 409,
                                   {"ok": True, "cell_line": cell_line} if ok
                                   else {"error": "not awaiting a selection"})
        if route.path == "/api/cluster_retry":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            with _LOCK:
                rep = _REPORTER
            if not rep:
                return self._send_json(409, {"error": "no active run"})
            if (body.get("action") or "").strip() == "cancel":
                ok = rep.provide_cluster_fix({"action": "cancel"})
            else:
                cin = body.get("cluster") or {}
                keys = ["PIPELINE_ROOT", "ssh_host", "ssh_user", "ssh_port", "ssh_key"]
                cluster = {k: str(cin.get(k)).strip() for k in keys if str(cin.get(k) or "").strip()}
                fix = {"cluster": cluster}
                if "ssh_password" in body:           # present (even empty) => set or clear the password
                    fix["ssh_password"] = body.get("ssh_password") or ""
                ok = rep.provide_cluster_fix(fix)
            return self._send_json(200 if ok else 409,
                                   {"ok": True} if ok else {"error": "not awaiting a cluster fix"})
        if route.path == "/api/ai_retry":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            with _LOCK:
                rep = _REPORTER
            if not rep:
                return self._send_json(409, {"error": "no active run"})
            if (body.get("action") or "").strip() == "skip_ai":
                ok = rep.provide_ai_fix({"action": "skip_ai"})
            else:
                fix = {}
                for k in ("provider", "model"):
                    v = str(body.get(k) or "").strip()
                    if v:
                        fix[k] = v
                if "base_url" in body:        # OpenAI-compatible endpoint; present-but-blank clears it (-> provider default)
                    fix["base_url"] = str(body.get("base_url") or "").strip()
                if body.get("api_key"):       # secret -> transient only, never written to config.json
                    fix["api_key"] = body.get("api_key")
                ok = rep.provide_ai_fix(fix)
            return self._send_json(200 if ok else 409,
                                   {"ok": True} if ok else {"error": "not awaiting an AI fix"})
        if route.path == "/api/cluster_status":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            # Works even with NO active run (after a server restart): take SSH creds + JOB_TAG from the
            # active run's config.json if present, else the saved settings + this instance's tag. The
            # probe then DISCOVERS the cluster root from this instance's <JOB_TAG>_* jobs.
            with _LOCK:
                run_dir = _RUN_DIR
            cfg, fallback_root = {}, ""
            if run_dir:
                P = Paths(run_dir)
                try:
                    cfg = (json.load(open(P.config, encoding="utf-8")).get("cluster_cfg")) or {}
                except Exception:
                    cfg = {}
                fallback_root = cluster_deploy._read_config_root(P) or ""
            settings = _load_settings()
            sc = settings.get("cluster") or {}
            host = (cfg.get("ssh_host") or sc.get("ssh_host") or "").strip()
            user = (cfg.get("ssh_user") or sc.get("ssh_user") or "").strip()
            port = str(cfg.get("ssh_port") or sc.get("ssh_port") or "22").strip() or "22"
            keyfile = (cfg.get("ssh_key") or sc.get("ssh_key") or "").strip()
            password = (body.get("ssh_password") or settings.get("ssh_password") or "").strip()
            # use THIS instance's own tag — NOT the shared saved-settings JOB_TAG (one settings file
            # across instances), which made e.g. an sra3 instance read sra2's jobs.
            # Scope strictly to THIS instance's tag: the UI sends its INSTANCE_TAG, and we never fall
            # back to a bare "sra" (which would prefix-match other instances' jobs on the cluster).
            job_tag = (body.get("job_tag") or cfg.get("JOB_TAG") or _INSTANCE_TAG or "").strip()
            if not job_tag:
                return self._send_json(400, {"error": "instance job tag not set — cannot scope the status check"})
            # the deployed bundle scopes JOB_TAG by the cell line (project isolation) -> prefer the EFFECTIVE
            # tag baked into THIS run's cluster config.sh so the per-stage probes match the real job names.
            if run_dir:
                try:
                    _eff = cluster_deploy._read_config_jobtag(Paths(run_dir))
                    if _eff:
                        job_tag = _eff
                except Exception:
                    pass
            if not fallback_root:
                fallback_root = (sc.get("PIPELINE_ROOT") or "").strip()
            status = cluster_deploy.remote_status(host, user, port, keyfile, password, job_tag, fallback_root)
            # if this run is the Bulk RNA-seq (STAR) module, also report STAR alignment progress
            run_module = ""; bed_mode = "intron"
            if run_dir:
                try:
                    _rcfg = json.load(open(Paths(run_dir).config, encoding="utf-8"))
                    run_module = _rcfg.get("module") or ""
                    bed_mode = (_rcfg.get("bed_cfg") or {}).get("BED_MODE") or "intron"
                except Exception:
                    run_module = ""
            if run_module == "bulk_rna_seq" and status.get("ok"):
                # Self-discover BAM_OUT so the STAR/BED probes don't point at the BASE root (which omits
                # the per-instance subfolder) after a restart or a STAR-start run -> progress would read
                # 0/0. Order: the star bundle's own config.sh (most authoritative) > the root the download
                # probe DISCOVERED from live job CWDs > the saved base root.
                bam_out = ""
                if run_dir:
                    try:
                        _scfg = os.path.join(Paths(run_dir).star_dir, "config.sh")
                        _m = re.search(r'(?m)^BAM_OUT="?(.*?)"?\s*$', open(_scfg, encoding="utf-8").read())
                        if _m and _m.group(1).strip():
                            bam_out = _m.group(1).strip()
                    except Exception:
                        bam_out = ""
                if not bam_out:
                    _disc = (status.get("root") or "").strip()
                    _base = _disc or fallback_root
                    bam_out = (_base.rstrip("/") + "/STAR_bams") if _base else ""
                try:
                    star = cluster_deploy.remote_star_status(host, user, port, keyfile, password,
                                                             f"{job_tag}_star", bam_out)
                    if star and star.get("ok"):
                        status["star"] = star
                        # once STAR is done, also report the BAM->BED stage (avoids noise while STAR runs)
                        if star.get("complete") and bam_out:
                            bed = cluster_deploy.remote_bed_status(host, user, port, keyfile, password,
                                                                   f"{job_tag}_star_bed", bam_out, bed_mode)
                            if bed and bed.get("ok"):
                                status["bed"] = bed
                                # once BED is done, also report the AltAnalyze (PSI) stage
                                if bed.get("complete"):
                                    psi_root = bam_out.rstrip("/").rsplit("/", 1)[0] + "/psi"
                                    psi = cluster_deploy.remote_psi_status(host, user, port, keyfile, password,
                                                                           f"{job_tag}_psi", psi_root)
                                    if psi and psi.get("ok"):
                                        status["psi"] = psi
                                        # once PSI is done, also report the splicing-concordance stage
                                        if psi.get("complete"):
                                            concord_root = bam_out.rstrip("/").rsplit("/", 1)[0] + "/concordance"
                                            conc = cluster_deploy.remote_concordance_status(
                                                host, user, port, keyfile, password,
                                                f"{job_tag}_concordance", concord_root)
                                            if conc and conc.get("ok"):
                                                status["concordance"] = conc
                except Exception:
                    pass
            # Cross-stage STALL alert scan (UNCONDITIONAL — independent of the nested STAR/BED/PSI probes
            # above, which only run when the prior stage completed): scan THIS run's folder for ANY stage's
            # PIPELINE_STALLED/_ORPHANED/_LAUNCH_TIMEOUT marker so a silent downstream stall (the 5-day BED
            # death) is surfaced RED on every poll, not hidden.
            # SCOPED to this run's OWN effective root (the discovered job CWD, else the JOB_TAG-scoped root
            # baked into config.sh) — NEVER the bare shared PIPELINE_ROOT. Sibling runs (and abandoned old
            # test folders) live under that shared root, so scanning it would surface THEIR stalls as FALSE
            # alerts here (e.g. a run still DOWNLOADING would 'inherit' another project's BED stall). If all
            # we can resolve is the bare shared root, we refuse to scan: a stall we can't attribute to THIS
            # run is not this run's alert.
            try:
                _bare = (sc.get("PIPELINE_ROOT") or "").strip().rstrip("/")
                _scope = (status.get("root") or fallback_root or "").strip().rstrip("/")
                if _scope and _scope != _bare:
                    _al = cluster_deploy.remote_alerts(host, user, port, keyfile, password, _scope)
                    if _al.get("ok"):
                        status["alerts"] = _al.get("alerts") or []
            except Exception:
                pass
            return self._send_json(200, status)
        if route.path == "/api/alert_test":
            try:
                body = self._read_body()
            except Exception:
                return self._send_json(400, {"error": "invalid JSON"})
            settings = _load_settings(); sc = settings.get("cluster") or {}
            email = (body.get("alert_email") or settings.get("alert_email") or "").strip()
            host = (sc.get("ssh_host") or "").strip(); user = (sc.get("ssh_user") or "").strip()
            if not email:
                return self._send_json(400, {"error": "Enter an alert email first."})
            if not host or not user:
                return self._send_json(400, {"error": "Set the cluster SSH host + user first — alerts are "
                                             "sent via the cluster's own mail."})
            port = str(sc.get("ssh_port") or "22").strip() or "22"
            keyfile = (sc.get("ssh_key") or "").strip()
            password = (body.get("ssh_password") or settings.get("ssh_password") or "").strip()
            ok = cluster_deploy.send_alert_email(
                host, user, port, keyfile, password, email, "SpliceScout test alert",
                "This is a SpliceScout test alert. If you received it, cluster→email alerts work "
                "(check spam too).")
            return self._send_json(200, {"ok": ok, "email": email, "note": (
                "Queued via the cluster's mail — check your inbox AND spam (external delivery depends "
                "on the cluster's mail relay)." if ok else "The cluster wouldn't accept the message.")})
        self._send_json(404, {"error": "not found"})


def _page():
    llm_cfg = {
        "models": llm_providers.MODELS,
        "labels": llm_providers.PROVIDER_LABEL,
        "keyHint": {
            "anthropic": "Anthropic key (sk-ant-…) — console.anthropic.com",
            "openai": "OpenAI key (sk-…) — platform.openai.com/api-keys",
            "gemini": "Gemini key (AI…) — aistudio.google.com/apikey",
            "ollama": "No key needed — Ollama runs locally at localhost:11434 (set the OLLAMA_HOST env var before launching for a custom host/port).",
        },
    }
    return (PAGE.replace("__DEFAULT_QUERY__", _html_attr(pipeline.DEFAULT_QUERY))
                .replace("__LLM_CONFIG__", json.dumps(llm_cfg))
                .replace("__INSTANCE_TAG__", _html_attr(_INSTANCE_TAG))
                .replace("__CHECKPOINTS__", json.dumps(progress.CHECKPOINTS))
                .replace("__STAGE_DOCS__", json.dumps(stage_docs.STAGE_DOCS))
                .replace("__AUTH_TOKEN__", _html_attr(_AUTH_TOKEN)))


def _html_attr(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---- background STALL-alert poller: email the user when a cluster stage stalls (the proactive heads-up) ----
_ALERT_SEEN_PATH = os.path.join(os.path.expanduser("~"), ".geo_pipeline_alert_seen.json")
_ALERT_POLL_SECONDS = 1200   # 20 min (the cluster watchdogs poll on the same order; finer is just noise)


def _alert_seen_load():
    try:
        return set(json.load(open(_ALERT_SEEN_PATH, encoding="utf-8")))
    except Exception:
        return set()


def _alert_seen_save(seen):
    try:
        json.dump(sorted(seen), open(_ALERT_SEEN_PATH, "w", encoding="utf-8"))
    except Exception:
        pass


def _alert_poll_once():
    """One scan: if a cluster stage has newly STALLED/ORPHANED/timed-out, email the configured address via
    the cluster's own mail. Deduped by marker path+time (a persistent stall isn't re-emailed every cycle)."""
    s = _load_settings(); sc = s.get("cluster") or {}
    email = (s.get("alert_email") or "").strip()
    host = (sc.get("ssh_host") or "").strip(); user = (sc.get("ssh_user") or "").strip()
    root = (sc.get("PIPELINE_ROOT") or "").strip()
    if not (email and host and user and root):
        return
    port = str(sc.get("ssh_port") or "22").strip() or "22"
    keyfile = (sc.get("ssh_key") or "").strip(); password = (s.get("ssh_password") or "").strip()
    r = cluster_deploy.remote_alerts(host, user, port, keyfile, password, root)
    if not r.get("ok"):
        print("WARN: alert poll could not reach the cluster (%s) -- stalls won't be emailed until it recovers"
              % (r.get("error") or "ssh/remote error"))
        return
    seen = _alert_seen_load()
    new = [a for a in (r.get("alerts") or []) if (a["path"] + "|" + a["when"]) not in seen]
    if not new:
        return
    body = ("SpliceScout detected stalled/failed cluster stage(s):\n\n"
            + "\n".join("  [%s] %s  (%s)\n    %s" % (a["stage"], a["kind"], a["when"], a["path"]) for a in new)
            + "\n\nA stalled stage does NOT auto-advance to the next. Open SpliceScout (or ask the Assistant "
              "\"is anything stuck?\") and fix + re-arm that stage.\nRoot: " + (r.get("root") or ""))
    if cluster_deploy.send_alert_email(host, user, port, keyfile, password, email,
                                       "SpliceScout: %d cluster stage(s) stalled" % len(new), body):
        for a in new:
            seen.add(a["path"] + "|" + a["when"])
        _alert_seen_save(seen)
    else:
        print("WARN: detected %d new cluster stall(s) but the alert email could not be sent (cluster mail "
              "refused it) -- will retry next cycle" % len(new))


def _alert_poller():
    import time
    while True:
        try:
            _alert_poll_once()
        except Exception as e:
            # was silently swallowed -> SSH errors (host down, bad/missing key) made the whole alert poller a
            # no-op with NO trace, so the user thought alerts were on when they were dead. Log it.
            print("WARN: alert poller pass failed: %r" % (e,))
        time.sleep(_ALERT_POLL_SECONDS)


def main():
    global _INSTANCE_TAG, _INSTANCE_LOCK_PATH, _AUTH_TOKEN
    ap = argparse.ArgumentParser(description="Web front end for the GEO RNA-seq pipeline")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true", help="don't auto-open a browser")
    ap.add_argument("--instance", default="",
                    help="instance name -> cluster JOB_TAG (else $SPLICESCOUT_INSTANCE, else auto sraN)")
    a = ap.parse_args()

    # Non-loopback bind (e.g. --host 0.0.0.0 / a LAN IP) exposes a remote-control plane (/api/start
    # launches cluster runs). Require a random per-startup token on EVERY request so the LAN can't drive
    # it unauthenticated. Loopback bind keeps the no-token UX (only the CSRF origin check applies).
    if _host_of("//" + (a.host or "")) not in _LOOPBACK_HOSTS:
        _AUTH_TOKEN = pysecrets.token_urlsafe(24)

    # serve relative to this script so runs/ lands next to the pipeline code
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # the ~100 MB AltAnalyze exon ref ships gzip'd (GitHub's 100 MB file limit); recreate the plain-text
    # copy the BED step uploads. Idempotent -> only the first launch pays the (few-second) decompress.
    try:
        import ensure_refs
        ensure_refs.ensure_ref_files()
    except Exception as _e:
        print(f"   [refs] auto-decompress skipped: {_e}")

    # claim this instance's identity -> the name from the launcher ($SPLICESCOUT_INSTANCE / --instance),
    # else the lowest free sra1 / sra2 / sra3 ... (released on exit; a dead instance's tag is reclaimed)
    preferred = (a.instance or "").strip() or _resolve_instance_name()
    _INSTANCE_TAG, _INSTANCE_LOCK_PATH = _claim_cluster_aware(preferred)
    atexit.register(_release_instance_slot)

    # auto-pick a free port so EVERY launch starts its own instance (run concurrent projects)
    httpd, port = _bind_server(a.host, a.port)
    url = f"http://{a.host if a.host != '0.0.0.0' else '127.0.0.1'}:{port}/"
    if _AUTH_TOKEN:
        url = f"{url}?token={_AUTH_TOKEN}"
    print(f"GEO RNA-seq pipeline UI  (cluster JOB_TAG \"{_INSTANCE_TAG}\")  ->  {url}")
    if _AUTH_TOKEN:
        print("   *** NON-LOOPBACK BIND: this is an UNAUTHENTICATED cluster-control plane without the")
        print("   *** token above. Open EXACTLY the URL printed here (it carries ?token=...); anyone on")
        print("   *** the network who lacks the token is refused. Prefer the default 127.0.0.1 bind.")
    if port != a.port:
        print(f"   (port {a.port} was busy -> using {port})")
    print("   Launch this again any time to run a concurrent project — each instance gets its own")
    print("   port and its own cluster JOB_TAG (the name you choose, or an auto sra1/sra2/...).")
    print("Press Ctrl+C to stop.")
    if not a.no_open:
        try:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        except Exception:
            pass
    threading.Thread(target=_alert_poller, daemon=True).start()   # email on a new cluster STALL (if configured)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down.")
        httpd.shutdown()
    finally:
        _release_instance_slot()


# ----------------------------------------------------------------------------
# Single-page UI (no f-string: literal { } below belong to CSS/JS)
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SpliceSCOUT</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700;900&family=Exo+2:wght@300;400;500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#0a0a0b; --panel:#141417; --panel2:#1b1b1f; --line:#2b2b31;
    --txt:#ededee; --mut:#8b8b93; --accent:#5fb6c0; --accent2:#5fb6c0;
    --ok:#5cb890; --warn:#d6ab4f; --err:#d6606e; --chip:#1b1b1f;
    --glow:transparent; --glow2:transparent;
  }
  *{box-sizing:border-box}
  body{margin:0;color:var(--txt);background:#0a0a0b;
    font:clamp(15px,0.30vw+12.4px,19px)/1.55 'Exo 2',ui-sans-serif,system-ui,"Segoe UI",Roboto,Arial,sans-serif}
  /* full-viewport animated DNA background (mouse-reactive); sits BEHIND all content */
  #dnafx{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;display:block}
  /* fluid: scale with the monitor — use more of a wide/ultrawide screen, shrink on small.
     Content sits above the DNA canvas (z-index 1). */
  .wrap{width:min(1100px,92vw);max-width:none;margin:0 auto;
    padding:clamp(24px,2.2vw,44px) clamp(16px,1.8vw,30px) 90px;position:relative;z-index:1}
  header h1{margin:0 0 4px;font-size:24px;letter-spacing:.2px}
  .ibadge{display:inline-block;font-size:12px;font-weight:600;color:var(--accent);
    background:rgba(91,140,255,.12);border:1px solid var(--line);border-radius:20px;
    padding:3px 10px;margin-left:8px;vertical-align:middle;letter-spacing:0}
  header p{margin:0 0 24px;color:var(--mut)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    padding:22px 22px;margin-bottom:18px;box-shadow:0 8px 30px rgba(0,0,0,.25)}
  label.fld{display:block;margin:0 0 18px}
  .fld>span.lbl{display:block;font-weight:600;margin-bottom:6px}
  .hint{color:var(--mut);font-size:13px;margin-top:5px}
  input[type=text],input[type=number],input[type=password],textarea,select{
    width:100%;background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    border-radius:9px;padding:10px 12px;font:inherit;outline:none}
  input:focus,textarea:focus,select:focus{border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(91,140,255,.18)}
  textarea{resize:vertical;min-height:62px}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>*{flex:1;min-width:180px}
  .scope{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .scope label{display:flex;gap:7px;align-items:center;cursor:pointer;
    background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:9px 12px}
  .scope label.sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(91,140,255,.18)}
  .scope input[type=number]{width:110px;flex:none}
  .chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
  .chip{background:var(--chip);border:1px solid var(--line);border-radius:20px;
    padding:5px 11px;font-size:12.5px;color:var(--mut);cursor:pointer}
  .chip:hover{color:var(--txt);border-color:var(--accent)}
  .check{display:flex;gap:9px;align-items:flex-start;cursor:pointer;margin-top:2px}
  .check input{margin-top:3px}
  details.adv{margin-top:4px;border-top:1px dashed var(--line);padding-top:14px}
  details.adv summary{cursor:pointer;color:var(--mut);font-weight:600;margin-bottom:14px}
  button.primary{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#fff;
    border:none;border-radius:10px;padding:12px 22px;font:600 15px/1 inherit;cursor:pointer}
  button.primary:disabled{opacity:.55;cursor:not-allowed}
  button.ghost{background:var(--panel2);color:var(--txt);border:1px solid var(--line);
    border-radius:9px;padding:9px 16px;font:600 14px/1 inherit;cursor:pointer}
  .err{background:rgba(255,93,108,.12);border:1px solid var(--err);color:#ffd2d6;
    border-radius:9px;padding:11px 13px;margin-top:12px;font-size:14px}
  /* progress view */
  .meta{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);font-size:13px;margin-bottom:6px}
  .meta b{color:var(--txt);font-weight:600}
  .obar{height:12px;background:var(--panel2);border-radius:8px;overflow:hidden;border:1px solid var(--line)}
  .obar>span{display:block;height:100%;width:0;border-radius:8px;
    background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .5s ease}
  .ohead{display:flex;justify-content:space-between;align-items:baseline;margin:16px 0 7px}
  .ohead .pct{font-size:22px;font-weight:700}
  .ohead .eta{color:var(--mut);font-size:13px}
  .stages{margin-top:18px;display:flex;flex-direction:column;gap:2px}
  .stage{display:flex;gap:13px;padding:11px 8px;border-radius:9px}
  .stage.active{background:var(--panel2)}
  .sicon{width:24px;text-align:center;font-size:16px;flex:none;color:var(--mut);padding-top:1px}
  .stage.done .sicon{color:var(--ok)} .stage.active .sicon{color:var(--accent)}
  .stage.skipped .sicon{color:#5a6b8c} .stage.skipped{opacity:.5}
  .sbody{flex:1;min-width:0}
  .slabel{font-weight:600}
  .stage.pending .slabel,.stage.skipped .slabel{color:var(--mut);font-weight:500}
  .scount{font-size:12.5px;color:var(--mut);margin-top:5px}
  .sdetail{font-size:12.5px;color:var(--mut);margin-top:3px;white-space:nowrap;
    overflow:hidden;text-overflow:ellipsis}
  .sbar{height:7px;background:#0d1220;border-radius:5px;overflow:hidden;margin-top:7px;border:1px solid var(--line)}
  .sbar>span{display:block;height:100%;background:var(--accent);width:0;transition:width .5s ease}
  .sbar.indet>span{width:35%;background:linear-gradient(90deg,transparent,var(--accent),transparent);
    animation:slide 1.1s linear infinite}
  @keyframes slide{0%{margin-left:-40%}100%{margin-left:100%}}
  .spin{display:inline-block;animation:spin 1s linear infinite}
  @keyframes spin{100%{transform:rotate(360deg)}}
  .logwrap{margin-top:18px}
  .logwrap h3{margin:0 0 7px;font-size:13px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  .log{background:#0a0e17;border:1px solid var(--line);border-radius:9px;padding:11px 13px;
    height:230px;overflow:auto;font:12.5px/1.55 ui-monospace,"Cascadia Code",Consolas,monospace;
    color:#bcd0f0;white-space:pre-wrap;word-break:break-word}
  .log .t{color:#56627d}
  .banner{border-radius:10px;padding:14px 16px;margin-bottom:16px;font-weight:600}
  .banner.ok{background:rgba(55,201,139,.12);border:1px solid var(--ok);color:#bff3da}
  .banner.err{background:rgba(255,93,108,.12);border:1px solid var(--err);color:#ffd2d6}
  .banner.pick{background:rgba(91,140,255,.12);border:1px solid var(--accent);color:#cfe0ff}
  .stats{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
  .stat{background:var(--panel2);border:1px solid var(--line);border-radius:11px;padding:13px 16px;flex:1;min-width:130px}
  .stat .n{font-size:24px;font-weight:700}
  .stat .k{color:var(--mut);font-size:12.5px;margin-top:2px}
  .files{display:flex;flex-direction:column;gap:8px}
  .file{display:flex;justify-content:space-between;align-items:center;gap:12px;
    background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 13px}
  .file.head{border-color:var(--accent);box-shadow:0 0 0 2px rgba(91,140,255,.15)}
  .file .nm{font-family:ui-monospace,Consolas,monospace;font-size:13px}
  .file .sz{color:var(--mut);font-size:12px}
  a.dl{background:linear-gradient(90deg,var(--accent),var(--accent2));color:#fff;text-decoration:none;
    border-radius:8px;padding:7px 14px;font-size:13px;font-weight:600}
  .tag{font-size:11px;background:var(--accent);color:#fff;border-radius:5px;padding:2px 7px;margin-left:8px;vertical-align:middle}
  .mutfoot{color:var(--mut);font-size:12px;margin-top:18px;word-break:break-all}
  /* tabs */
  .tabs{display:flex;gap:6px;margin:6px 0 14px}
  .tabs button{background:var(--panel2);color:var(--mut);border:1px solid var(--line);
    border-radius:10px;padding:8px 18px;font:600 14px/1 inherit;cursor:pointer}
  .tabs button.on{color:#fff;border-color:transparent;background:linear-gradient(90deg,var(--accent),var(--accent2))}
  .tabs button[hidden]{display:none}
  /* step-doc modal */
  .modal{position:fixed;inset:0;background:rgba(6,9,16,.66);display:flex;align-items:center;
    justify-content:center;z-index:50;padding:20px}
  .modal[hidden]{display:none}
  .modal .box{background:var(--panel);border:1px solid var(--line);border-radius:14px;
    max-width:600px;width:100%;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
  .modal h2{margin:0 0 12px;font-size:18px}
  .modal #docBody p{margin:0 0 10px}
  .modal .docmeta{margin-top:10px;font-size:12.5px;color:var(--mut);font-family:ui-monospace,Consolas,monospace;
    word-break:break-word}
  .modal .x{float:right;cursor:pointer;color:var(--mut);font-size:22px;line-height:1;border:none;background:none}
  .slabel.doc{cursor:pointer} .slabel .qm{color:var(--accent);font-size:12px;opacity:.6}
  .slabel.doc:hover .qm{opacity:1}
  /* plots */
  .psec{margin:18px 0 7px;font-size:12.5px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.5px}
  .studylist{display:flex;flex-direction:column;gap:5px;max-height:280px;overflow:auto;margin-bottom:6px}
  .studyitem{display:flex;gap:10px;align-items:baseline;background:var(--panel2);border:1px solid var(--line);
    border-radius:8px;padding:8px 12px;cursor:pointer}
  .studyitem:hover{border-color:var(--accent)}
  .studyitem.sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(91,140,255,.18)}
  .studyitem .snm{font-family:ui-monospace,Consolas,monospace;font-size:13px;flex:none}
  .studyitem .sttl{flex:1;min-width:0;color:var(--mut);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .studyitem .sn{color:var(--mut);font-size:12px;flex:none}
  .chart{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:6px;margin-bottom:10px;min-height:120px}
  /* page footer */
  .pagefoot{color:var(--mut);font-size:12.5px;margin-top:28px;padding-top:14px;border-top:1px solid var(--line)}
  .pagefoot a{color:var(--accent);text-decoration:none} .pagefoot a:hover{text-decoration:underline}
  /* pipeline phase-range slider (left rail) */
  .setupgrid{display:grid;grid-template-columns:176px 1fr;gap:22px;align-items:start}
  @media(max-width:760px){.setupgrid{grid-template-columns:1fr}#phaserail{display:none}}
  #phaserail{position:sticky;top:18px;padding:12px 10px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)}
  .raillbl{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;text-align:center}
  .railwrap{display:flex;gap:10px;padding:9px 2px 9px 9px}
  .railtrack{position:relative;width:8px;border-radius:6px;background:var(--line);flex:none}
  .railfill{position:absolute;left:0;right:0;background:linear-gradient(180deg,var(--accent),var(--accent2));border-radius:6px}
  .railhandle{position:absolute;left:4px;width:18px;height:18px;margin-left:-9px;margin-top:-9px;border-radius:50%;background:var(--accent);border:2px solid #fff;cursor:grab;touch-action:none;box-shadow:0 1px 5px rgba(0,0,0,.55);z-index:2}
  .railhandle:active{cursor:grabbing}
  .raillabels{position:relative;flex:1}
  .railtick{position:absolute;left:0;font-size:11.5px;line-height:1.1;color:var(--mut);white-space:nowrap;cursor:pointer;transform:translateY(-50%)}
  .railtick.inrange{color:var(--txt)}
  .railtick.edge{color:var(--accent);font-weight:600}
  #startInputs .startinput{margin-top:8px}

  /* ============================ MINIMALIST FUTURISTIC (matte black) ============================ */
  /* Flat. No gradients, no glow, no neon. One restrained accent, hairline borders. */
  header{position:relative}
  .brand{font-family:'Orbitron',ui-sans-serif,sans-serif;font-weight:800;
    font-size:clamp(28px,4vw,44px);letter-spacing:4px;line-height:1;margin:0 0 10px;
    display:flex;align-items:center;gap:14px;flex-wrap:wrap;color:var(--txt)}
  .wordmark{color:var(--txt)}
  .wordmark b{font-weight:800;letter-spacing:5px;color:var(--accent)}
  .tagline{font-family:'Share Tech Mono',ui-monospace,monospace;color:var(--mut);font-size:12px;
    letter-spacing:2.5px;text-transform:uppercase;margin:0 0 26px}
  .tagline::before{content:"\2014  ";color:var(--accent)}
  h2,.modal h2{font-family:'Orbitron',ui-sans-serif,sans-serif;letter-spacing:.6px;font-weight:700}

  /* Flat matte panels — solid surface, hairline border, no shadow/blur/gradient */
  .card,.modal .box{background:var(--panel);border:1px solid var(--line);border-radius:13px;
    box-shadow:none;backdrop-filter:none;-webkit-backdrop-filter:none;position:relative;overflow:hidden}
  .card::before{content:"";position:absolute;top:0;left:0;width:36px;height:2px;background:var(--accent)}

  /* ibadge — flat outline */
  .ibadge{font-family:'Share Tech Mono',monospace;color:var(--accent);background:transparent;
    border:1px solid var(--line);letter-spacing:1px;box-shadow:none}

  /* inputs — flat, hairline border, accent border on focus (no glow) */
  input[type=text],input[type=number],input[type=password],textarea,select{
    background:#101013;border:1px solid var(--line);border-radius:8px;box-shadow:none;
    transition:border-color .14s}
  input:focus,textarea:focus,select:focus{border-color:var(--accent);box-shadow:none}

  /* buttons / download / active tab — solid accent fill, no gradient/glow */
  button.primary,a.dl,.tabs button.on{background:var(--accent);background-image:none;color:#08171a;
    box-shadow:none;letter-spacing:.4px;text-shadow:none;transition:filter .14s,background-color .14s}
  button.primary{font-family:'Orbitron',sans-serif;font-weight:700;font-size:13.5px;letter-spacing:1px}
  button.primary:hover,a.dl:hover{filter:brightness(1.1)}
  button.primary:disabled{filter:grayscale(.4) brightness(.8)}
  button.ghost,.tabs button{box-shadow:none;transition:border-color .14s,color .14s}
  button.ghost:hover,.tabs button:hover{border-color:var(--accent);color:var(--txt);box-shadow:none}

  /* progress + numerics — flat accent fill, solid mono text (no gradient/glow) */
  .obar,.sbar{border-color:var(--line);background:#101013}
  .obar>span,.sbar>span,.railfill{background:var(--accent);background-image:none;box-shadow:none}
  .ohead .pct,.stat .n{font-family:'Orbitron',ui-sans-serif,sans-serif;color:var(--txt);
    background:none;-webkit-text-fill-color:currentColor}
  .stage.active{background:var(--panel2);box-shadow:none}

  /* selected states — thin solid accent border, no glow */
  .scope label.sel,.studyitem.sel,.file.head{box-shadow:none;border-color:var(--accent)}
  .chip:hover{box-shadow:none;border-color:var(--accent)}
  .railhandle{background:var(--accent);background-image:none;border-color:var(--txt);box-shadow:none}

  /* flat surfaces (no blur), flat log */
  .banner{backdrop-filter:none}
  .stat,.scope label,.studyitem,.chart,#phaserail,.tabs button,.file{backdrop-filter:none}
  .log{background:#0c0c0e;backdrop-filter:none}
  ::selection{background:var(--accent);color:#08171a}
  /* minimal scrollbars */
  *{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
  *::-webkit-scrollbar{width:8px;height:8px}
  *::-webkit-scrollbar-thumb{background:var(--line);background-image:none;border-radius:5px}
  *::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>
<canvas id="dnafx" aria-hidden="true"></canvas>
<div class="wrap">
  <header>
    <button type="button" id="shutdownBtn" title="Stop the SpliceScout web server (cluster runs keep going on their own)" style="position:absolute;top:0;right:0;padding:6px 12px;font-size:13px;cursor:pointer;background:var(--panel2);color:var(--mut);border:1px solid var(--line);border-radius:9px">&#9211; Shut down</button>
    <h1 class="brand"><span class="wordmark">Splice<b>SCOUT</b></span> <span class="ibadge" title="This instance's cluster JOB_TAG — the instance name you chose at launch (or an auto sra1/sra2/... if you left it blank). It namespaces this project's LSF jobs so concurrent projects never collide.">JOB_TAG __INSTANCE_TAG__</span></h1>
    <p class="tagline">GEO&#8202;&rarr;&#8202;splicing-amenable, cell-line-grouped compound intelligence</p>
  </header>

  <div class="tabs" id="tabs">
    <button id="tabRun" class="on" type="button">Run</button>
    <button id="tabPlots" type="button" hidden>Plots</button>
    <button id="tabAssistant" type="button">Assistant</button>
  </div>

  <div id="runtab">
  <!-- SETUP -->
  <section id="setup" class="card">
    <div class="setupgrid">
      <aside id="phaserail">
        <div class="raillbl">Pipeline range</div>
        <div class="railwrap">
          <div id="railTrack" class="railtrack">
            <div id="railFill" class="railfill"></div>
            <div id="railStart" class="railhandle" data-h="start" title="START — first phase to run"></div>
            <div id="railEnd" class="railhandle" data-h="end" title="END — last phase to run"></div>
          </div>
          <div id="railLabels" class="raillabels"></div>
        </div>
        <div class="hint" style="margin-top:10px">Drag the two handles to run only part of the pipeline. Phases above START are skipped (supply their output at right); phases below END don't run.</div>
      </aside>
      <div class="setupmain">
    <div id="clusterCheck" hidden style="margin-bottom:16px;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--panel2)">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
        <span class="hint" style="margin:0">Cluster jobs from a previous launch of this instance still running? Check without starting a new run.</span>
        <button type="button" class="ghost" id="clCheckBtn">Check cluster status</button>
      </div>
      <div id="clusterstatus2" style="margin-top:10px"></div>
    </div>
    <form id="form">
      <div class="fld" id="startInputs" hidden></div>
      <label class="fld">
        <span class="lbl">NCBI GEO search query</span>
        <textarea id="query" spellcheck="false">__DEFAULT_QUERY__</textarea>
        <div class="hint">Entrez syntax, e.g. fields like <code>[Description]</code>, <code>[Organism]</code>. This is sent to GEO's <code>gds</code> database.</div>
        <div class="chips" id="examples">
          <span class="chip" data-q="rna-seq[Description] AND human[Organism] AND drug">human + drug</span>
          <span class="chip" data-q="rna-seq[Description] AND human[Organism] AND cancer AND treatment">cancer treatment</span>
          <span class="chip" data-q="rna-seq[Description] AND human[Organism] AND inhibitor">inhibitor</span>
        </div>
      </label>

      <label class="fld">
        <span class="lbl">How many studies?</span>
        <div class="scope" id="scope">
          <label id="lblCap" class="sel"><input type="radio" name="scope" value="capped" checked> Cap at
            <input type="number" id="cap" min="1" value="25"> studies</label>
          <label id="lblAll"><input type="radio" name="scope" value="all"> All matching studies</label>
        </div>
        <div class="hint">Extraction fetches SRA metadata per study (rate-limited), so more studies = longer runs and more API cost. Start small (25) to validate, then scale up.</div>
      </label>

      <label class="fld">
        <span class="lbl">AI provider &amp; key</span>
        <div class="row">
          <select id="provider" style="max-width:240px">
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="openai">OpenAI (ChatGPT)</option>
            <option value="gemini">Google Gemini</option>
            <option value="ollama">Ollama (local)</option>
          </select>
          <div id="modelcombo" style="position:relative;flex:1;min-width:200px">
            <input type="text" id="model" autocomplete="off" spellcheck="false"
                   placeholder="model name (editable — type any model)" style="width:100%;padding-right:30px">
            <button type="button" id="modeldrop" title="Models used with this provider"
                    style="position:absolute;right:1px;top:1px;bottom:1px;width:28px;border:none;border-radius:0 8px 8px 0;background:transparent;color:var(--mut);cursor:pointer;font-size:12px">&#9662;</button>
            <div id="modelmenu" style="display:none;position:absolute;z-index:30;left:0;right:0;top:calc(100% + 4px);background:var(--panel2);border:1px solid var(--line);border-radius:9px;box-shadow:0 8px 24px rgba(0,0,0,.45);max-height:240px;overflow:auto;padding:5px"></div>
          </div>
        </div>
        <div class="row" style="margin-top:10px;align-items:center;gap:8px">
          <span class="lbl" style="margin:0;white-space:nowrap;font-size:12px">Max output tokens</span>
          <input type="number" id="maxtokens" min="256" step="256" autocomplete="off"
                 placeholder="default 60000" style="max-width:150px">
          <span class="hint" style="margin:0">per model — caps each AI call&#39;s output. Raise for reasoning models (avoids truncation); lower to cut cost / stay under a rate limit.</span>
        </div>
        <input type="password" id="akey" autocomplete="off" placeholder="API key" style="margin-top:10px">
        <div class="hint" id="keyhint">Used for the AI cleaning passes (drug-name canonicalization + sample classification).</div>
        <div id="baseurlrow" style="margin-top:10px;display:none">
          <input type="text" id="baseurl" autocomplete="off" spellcheck="false" style="width:100%"
                 placeholder="Custom OpenAI-compatible base URL (optional)">
          <div class="hint">Point the OpenAI provider at a custom OpenAI-compatible endpoint — a MiMo/Qwen host, a local vLLM/LM-Studio server, or OpenRouter <code>https://openrouter.ai/api/v1</code>. Blank = <code>api.openai.com</code>. For <b>Ollama</b>, blank = <code>http://localhost:11434/v1</code> — set it only if Ollama runs on another host/port. The API key above (if any) is sent to this endpoint.</div>
          <label class="check" style="margin-top:8px"><input type="checkbox" id="noreason"> <span>Disable model reasoning (chain-of-thought). Needed for reasoning models like <b>MiMo</b> whose &ldquo;thinking&rdquo; can use up the token budget and truncate a batch (logged as &ldquo;no output&rdquo;). Leave off for non-reasoning models.</span></label>
        </div>
        <label class="check"><input type="checkbox" id="skip"> <span>Skip AI cleaning — run the deterministic stages only (no key needed). Tables will lack canonical drug names &amp; recovered cell lines.</span></label>
      </label>

      <label class="fld">
        <span class="lbl">Analysis module</span>
        <div class="hint" style="margin-bottom:9px">Sets the library-prep filter (which protocols pass the headline table) and the downstream analysis pipeline. More modules (single-cell, etc.) coming.</div>
        <div class="scope" id="modulesel">
          <label id="lblBulkRna" class="sel"><input type="radio" name="module" value="bulk_rna_seq" checked> Bulk RNA-seq (STAR)</label>
        </div>
        <div class="hint">Keeps full-length / splicing-amenable protocols (drops 10x, single-cell, 3'-tag) and, on the cluster, aligns the downloaded reads with STAR.</div>
        <div id="starfields">
          <input type="text" id="star_genome" autocomplete="off" spellcheck="false" style="width:100%;margin-top:8px"
                 placeholder="STAR genome index dir on the cluster (GENOME_DIR) — blank = resolve/build by organism">
          <div class="hint">If blank or not a real index, an index is resolved by organism: the registry (<code>star_index_registry.json</code>) &rarr; a previously built index &rarr; a one-time build job. Fill the registry with your GRCh38 index path to skip the build. Used only when the cluster is on.</div>
          <details class="adv" style="margin-top:8px"><summary>STAR options</summary>
            <div class="row" style="margin-top:8px">
              <input type="text" id="star_org" placeholder="Organism (blank = auto-detect; default Homo sapiens)" autocomplete="off">
              <input type="text" id="star_gtf" placeholder="GTF path (optional)" autocomplete="off">
            </div>
            <input type="text" id="star_indexroot" placeholder="STAR_INDEX_ROOT (where a build-once index is written)" autocomplete="off" style="width:100%;margin-top:8px">
            <div class="row" style="margin-top:8px">
              <input type="number" id="star_threads" placeholder="threads (5)" min="1" max="32">
              <input type="number" id="star_mem" placeholder="mem MB (64000)" min="1000">
              <input type="text" id="star_wall" placeholder="wall — blank = queue max">
            </div>
          </details>
          <label class="sel" style="display:block;margin-top:10px"><input type="checkbox" id="bed_enable" checked> Then convert BAMs &rarr; AltAnalyze junction/exon BEDs (the BAM&rarr;BED stage, after STAR)</label>
          <div class="hint">Runs automatically once STAR finishes. The AltAnalyze BAM&rarr;BED scripts + exon reference are shipped with the bundle, so the cluster needs no AltAnalyze install (just the stock python/2.7.5 + samtools modules).</div>
          <details class="adv" style="margin-top:8px"><summary>BAM&rarr;BED options</summary>
            <div class="row" style="margin-top:8px">
              <input type="text" id="bed_species" placeholder="species (blank = auto from organism: Hs/Mm/Rn/Dr/Ss/Ma)" autocomplete="off">
              <input type="number" id="bed_mem" placeholder="mem MB (32000)" min="1000">
              <input type="text" id="bed_wall" placeholder="wall — blank = queue max">
            </div>
            <div class="scope" id="bedmodesel" style="margin-top:8px">
              <label class="sel"><input type="radio" name="bedmode" value="intron" checked> Intron-retention (__intronJunction.bed)</label>
              <label><input type="radio" name="bedmode" value="exon"> Exon counts (__exon.bed)</label>
              <label><input type="radio" name="bedmode" value="both"> Both</label>
            </div>
            <div class="hint">__junction.bed is always produced; this picks the BAMtoExonBED pass. Default is intron-retention (AltAnalyze's own default).</div>
          </details>
          <label class="sel" style="display:block;margin-top:10px"><input type="checkbox" id="psi_enable" checked> Then run AltAnalyze splicing (PSI) on the BEDs (the analysis stage, after BAM&rarr;BED)</label>
          <div class="hint">Runs one AltAnalyze job over all the BEDs once BAM&rarr;BED finishes &mdash; a per-sample PSI table, plus a differential (dPSI) comparison when a 2-group split exists. AltAnalyze is found on the cluster, else uploaded under PIPELINE_ROOT/altanalyze_home only if it isn't there.</div>
          <details class="adv" style="margin-top:8px"><summary>AltAnalyze (PSI) options</summary>
            <div class="row" style="margin-top:8px">
              <input type="text" id="psi_home" placeholder="AltAnalyze home on cluster (blank = auto: found on cluster, else uploaded under PIPELINE_ROOT/altanalyze_home)" autocomplete="off">
              <input type="text" id="psi_db" placeholder="AltDatabase path (blank = inside AltAnalyze home)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:8px">
              <input type="text" id="psi_local" placeholder="local AltAnalyze dir to upload ONLY if not found on cluster (optional)" autocomplete="off">
              <input type="text" id="psi_species" placeholder="species (blank = auto: Hs/Mm/Rn/Dr/Ss/Ma)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:8px">
              <input type="text" id="psi_expname" placeholder="experiment name (blank = cell line)" autocomplete="off">
              <input type="number" id="psi_mem" placeholder="mem MB (128000)" min="1000">
              <input type="text" id="psi_wall" placeholder="wall — blank = queue max">
            </div>
            <label class="fld" style="margin-top:8px"><span class="lbl">When the AltAnalyze step finishes, compress remaining data</span>
              <select id="psi_compress">
                <option value="gzip">gzip &mdash; fast, widely readable (default)</option>
                <option value="xz">LZMA2 (xz) &mdash; smaller, slower</option>
                <option value="off">off &mdash; leave everything uncompressed</option>
              </select>
              <span class="hint">Compresses kept, still-uncompressed data (BEDs + AltAnalyze text outputs) to reclaim space once the run is done. Already-compressed files and BAMs are skipped.</span>
            </label>
            <label class="sel" style="display:block;margin-top:8px"><input type="checkbox" id="psi_goelite"> Also run GO-Elite enrichment (needs the GO-Elite DB + R; only when a comparison runs)</label>
            <div class="hint">AltAnalyze runs as Python 2.7 + samtools + R on the cluster. Comparison groups default to the run table's treated-vs-control split.</div>
          </details>
          <details class="adv" style="margin-top:8px"><summary>Comparison groups (optional &mdash; define your own)</summary>
            <div class="hint">Leave empty to auto-compare <b>drug-treated vs control</b> from the run-table metadata. To compare your OWN categories, add 2+ groups: a name, optional match keywords (comma-separated), and mark one as the control/baseline. Each sample is sorted by those keywords first, then the AI handles the rest from the full metadata row; samples that match no group are dropped from the comparison (the per-sample PSI table still covers everyone).</div>
            <div id="psigroups" style="margin-top:8px"></div>
            <button type="button" id="psi_addgroup" style="margin-top:6px;padding:4px 10px;cursor:pointer">+ Add group</button>
          </details>
          <label class="sel" style="display:block;margin-top:10px"><input type="checkbox" id="conc_enable" checked> Then score the drug PSI signatures against a cancer atlas (drug&ndash;cancer splicing concordance, after PSI)</label>
          <details class="adv" style="margin-top:8px"><summary>Drug concordance options</summary>
            <div class="hint">After AltAnalyze PSI, each drug's splicing signature is scored against a cancer-subtype atlas. Concordance <b>1</b> = the drug mimics the cancer subtype (bad), <b>0</b> = it reverses it (therapeutic); candidates fall below the threshold, ranked by overlapping splicing events and the subtype's patient count.</div>
            <label class="fld" style="margin-top:8px"><span class="lbl">Cancer atlas</span>
              <select id="conc_atlas">
                <option value="auto">auto &mdash; from the cell line (MDS-L &rarr; AML/MDS, A549 &rarr; lung)</option>
                <option value="aml_mds">AML/MDS (Leucegene OncoSplice subtypes)</option>
                <option value="lung">Lung (TCGA LUAD + LUSC OncoSplice subtypes)</option>
              </select>
              <span class="hint">Auto-selected from the cell line via cancer_atlas_registry.json; pick one to override.</span>
            </label>
            <div style="margin-top:8px">
              <input type="text" id="conc_dpsi" placeholder="dPSI cutoff (0.1)" autocomplete="off">
              <input type="text" id="conc_rawp" placeholder="rawp cutoff (0.05)" autocomplete="off">
              <input type="text" id="conc_thresh" placeholder="concordance threshold for candidates (0.3)" autocomplete="off">
            </div>
            <label class="sel" style="display:block;margin-top:8px"><input type="checkbox" id="conc_removeir"> Drop intron-retention events when scoring (&minus;&minus;removeIR)</label>
            <div class="hint">Reuses the AltAnalyze install the PSI stage resolved (only its export/UI/unique modules). Results land in &lt;run&gt;/concordance/results/&lt;atlas&gt;/ranked_concordance_summary.txt.</div>
          </details>
          <div style="margin-top:12px">
            <div class="lbl" style="margin-bottom:4px">Disk cleanup</div>
            <label class="sel" style="display:block"><input type="checkbox" id="del_fastq" checked> Delete FASTQs after they're aligned &mdash; frees disk (re-alignment would need a re-download)</label>
            <label class="sel" style="display:block;margin-top:6px"><input type="checkbox" id="del_bam"> Delete BAMs after they're converted to BEDs &mdash; frees more disk (re-making BEDs would need re-alignment)</label>
          </div>
        </div>
      </label>

      <label class="fld">
        <span class="lbl">Cell-line metadata deep-dive</span>
        <div class="hint" style="margin-bottom:9px">The best cell line is taken through the full SRA Run Selector metadata + a download-ready <code>SraAccList.txt</code> (plus a filtered per-run table &amp; Excel workbook).</div>
        <div class="scope" id="pickmode">
          <label id="lblAuto" class="sel"><input type="radio" name="pick" value="auto" checked> Auto-pick best cell line</label>
          <label id="lblManual"><input type="radio" name="pick" value="manual"> Let me choose after the scan</label>
        </div>
        <div class="hint">Auto ranks real cell lines by # unique compounds, then total reads. Manual pauses the run after the scan so you can pick from the ranked list.</div>
      </label>

      <label class="fld" id="clusterFld">
        <span class="lbl">Run on a cluster (download &amp; convert the reads)</span>
        <div class="scope" id="clmode">
          <label id="lblAuton" class="sel"><input type="radio" name="clmode" value="autonomous" checked> Autonomous (upload &amp; launch)</label>
          <label id="lblManualDl"><input type="radio" name="clmode" value="manual"> Download a bundle</label>
          <label id="lblClOff"><input type="radio" name="clmode" value="off"> Off</label>
        </div>
        <div class="hint">Hands the per-study accession lists to your LSF download pipeline (each study downloaded/converted separately). <b>Autonomous</b> uploads the bundle to your cluster and runs <code>./run_pipeline.sh</code>; <b>Download</b> gives you a ready-to-run zip. Each run lands in its own <b>per-instance subfolder</b> under PIPELINE_ROOT named by the instance tag (e.g. <code>…/Brazen</code>), so every stage + re-run of one instance shares a stable folder and runs never mix.</div>
        <div id="clfields">
          <input type="text" id="clroot" placeholder="PIPELINE_ROOT — absolute path on the cluster, e.g. /data/mylab/sra" style="margin-top:10px" autocomplete="off">
          <div id="sshwrap">
            <div class="row" style="margin-top:10px">
              <input type="text" id="sshhost" placeholder="SSH host (login.hpc.edu)" autocomplete="off">
              <input type="text" id="sshuser" placeholder="SSH username" autocomplete="off">
              <input type="number" id="sshport" value="22" title="SSH port — 22 is the standard SSH port" style="max-width:110px">
            </div>
            <div class="hint">The number on the right is the <b>SSH port</b> (22 is the standard default — only change it if your cluster uses a custom one).</div>
            <div class="row" style="margin-top:10px">
              <input type="text" id="sshkey" placeholder="private key file (optional — else SSH agent/default)" autocomplete="off">
              <input type="password" id="sshpass" placeholder="password (optional; key/agent preferred)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:10px;align-items:center;gap:8px">
              <input type="email" id="alertemail" placeholder="Alert email — get notified if a cluster stage STALLS (optional)" autocomplete="off" style="flex:1">
              <button type="button" class="ghost" id="alerttest" style="white-space:nowrap">Send test</button>
            </div>
            <div class="hint">If a download/STAR/BED/PSI stage stalls, SpliceScout emails you (it polls every ~20&nbsp;min while running). Sent via the cluster&#39;s own <code>mail</code> — check spam on the first one. Blank = off.</div>
          </div>
          <details class="adv" style="margin-top:12px">
            <summary>Advanced cluster settings (config.sh)</summary>
            <div class="row">
              <input type="text" id="clscratch" placeholder="SCRATCH_DIR (/scratch/$USER)" autocomplete="off">
              <input type="text" id="clqueue" placeholder="LSF_QUEUE (blank = default)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:10px">
              <input type="text" id="cltool" placeholder="sratoolkit module (sratoolkit/3.0.0)" autocomplete="off">
              <input type="text" id="claspera" placeholder="aspera module (aspera/3.9.1)" autocomplete="off">
            </div>
            <div class="row" style="margin-top:10px">
              <input type="number" id="clthreads" placeholder="THREADS (5)">
              <input type="number" id="clmem" placeholder="MEM_MB (32000)">
              <input type="text" id="clwall" placeholder="WALL — blank = queue max">
              <input type="number" id="clpfmem" placeholder="PREFETCH_MEM_MB (132000)">
              <input type="text" id="cljob" value="__INSTANCE_TAG__" placeholder="JOB_TAG (auto per instance)" title="LSF job-name prefix. Defaults to this instance's name (the one you chose at launch, or an auto sra1/sra2/... if you left it blank) so concurrent projects' cluster jobs never collide. Edit if you want a different tag." autocomplete="off">
            </div>
            <div class="row" style="margin-top:10px">
              <input type="text" id="clmodelpath" placeholder="Diagnose-AI model .gguf (optional cluster path)" title="Optional: full path on the cluster to a specific GGUF model for the CPU diagnostic AI. Leave blank to use the shared install's model (auto-cached into each pipeline dir on first need)." autocomplete="off" style="flex:2">
              <input type="text" id="clmodeldir" placeholder="model cache dir (optional)" title="Optional: a directory to cache the diagnose-AI model in so future runs reuse it. Blank = per-pipeline <PIPELINE_ROOT>/.splicescout_ai/models, auto-populated from the shared install." autocomplete="off" style="flex:1">
            </div>
            <div class="hint">Diagnose-AI model (optional). Blank uses the shared install's model and caches a copy into the pipeline dir so future runs reuse it. Set a path to point at a specific <code>.gguf</code>, or a cache dir to share one copy across stages.</div>
          </details>
        </div>
      </label>

      <details class="adv">
        <summary>Advanced options</summary>
        <label class="fld">
          <span class="lbl">AI concurrency</span>
          <input type="number" id="conc" min="1" max="99" value="8">
          <div class="hint">Parallel AI requests (1–99). Higher = faster; a rate-limited key may 429 (which now pauses cleanly via the AI-fix prompt).</div>
        </label>
        <label class="fld" style="margin-bottom:0">
          <span class="lbl">NCBI E-utilities API key (optional)</span>
          <input type="password" id="ncbi" autocomplete="off" placeholder="optional — raises rate limit 3→10 req/s">
          <div class="hint">Speeds up the fetch + extract stages. Leave blank to run keyless.</div>
        </label>
      </details>

      <div class="hint" style="margin-bottom:12px">Your entries (including API keys &amp; cluster info) are saved locally on this machine (<code>~/.geo_pipeline_settings.json</code>) so they auto-fill next time.</div>
      <button type="submit" class="primary" id="start">Start pipeline</button>
      <button type="button" class="ghost" id="resume" title="Continue this instance's most recent run from where it stopped (finished stages are skipped). Use after a server restart, or to retry a stalled stage. Tip: if the AI proxy is down, switch the provider above (e.g. to Ollama) or tick Skip AI first, then Resume.">Resume last run</button>
      <div class="err" id="formErr" hidden></div>
    </form>
      </div><!-- /setupmain -->
    </div><!-- /setupgrid -->
  </section>

  <!-- RUN -->
  <section id="run" class="card" hidden>
    <div id="banner"></div>
    <div class="meta" id="meta"></div>
    <div class="ohead">
      <span class="pct" id="opct">0%</span>
      <span class="eta" id="oeta"></span>
    </div>
    <div class="obar"><span id="obar"></span></div>
    <div class="stages" id="stages"></div>

    <div id="selectpanel" hidden></div>
    <div id="aifix" hidden></div>
    <div id="clusterfix" hidden></div>
    <div id="results" hidden></div>

    <div class="logwrap">
      <h3>Live log</h3>
      <div class="log" id="log"></div>
    </div>

    <div style="margin-top:18px;display:flex;gap:10px">
      <button class="ghost" id="newrun" hidden>Start another run</button>
    </div>
    <div class="mutfoot" id="rundir"></div>
  </section>
  </div><!-- /runtab -->

  <!-- PLOTS -->
  <section id="plots" class="card" hidden>
    <div id="plotsbody"><div class="hint">Loading…</div></div>
  </section>

  <!-- ASSISTANT (chat) -->
  <section id="chat" class="card" hidden>
    <div id="chatlog" style="min-height:300px;max-height:60vh;overflow:auto;padding:4px 2px"></div>
    <div id="chatwork" class="hint" hidden style="padding:4px 8px">the assistant is working…</div>
    <div style="display:flex;gap:8px;margin-top:8px;align-items:flex-end">
      <textarea id="chatbox" rows="2" placeholder="Ask me to set up a run, write a GEO query, chart your data, or check the cluster…" style="flex:1;resize:vertical;font-size:13.5px;padding:8px 10px;border-radius:10px;border:1px solid var(--line)"></textarea>
      <button id="chatsend" class="primary" type="button">Send</button>
    </div>
    <div class="hint" style="margin-top:6px">I can fill the form, generate &amp; test the GEO query, run SQL/charts, and read local + cluster logs — but I never launch anything; you review the form and press Start. (Local Ollama is slow here; a cloud provider is much snappier for the assistant.)</div>
  </section>

  <footer class="pagefoot">
    <a href="/readme" target="_blank">&#128214; User Guide</a> &nbsp;&middot;&nbsp; SpliceScout &nbsp;&middot;&nbsp; instance __INSTANCE_TAG__
  </footer>
</div>

<!-- step documentation modal -->
<div id="docmodal" class="modal" hidden>
  <div class="box">
    <button class="x" type="button" id="docClose">&times;</button>
    <h2 id="docTitle"></h2>
    <div id="docBody"></div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function fmtDur(s){
  if(s==null) return '—'; s=Math.round(s);
  if(s<60) return s+'s';
  const m=Math.floor(s/60), sec=s%60;
  if(m<60) return m+'m '+(sec<10?'0':'')+sec+'s';
  const h=Math.floor(m/60); return h+'h '+(m%60)+'m';
}
function fmtSize(b){ if(b==null) return ''; if(b<1024) return b+' B'; if(b<1048576) return (b/1024).toFixed(1)+' KB'; return (b/1048576).toFixed(1)+' MB'; }

// ---- setup interactions ----
const lblCap=$('#lblCap'), lblAll=$('#lblAll'), capEl=$('#cap');
function syncScope(){
  const all = $('input[name=scope][value=all]').checked;
  lblAll.classList.toggle('sel', all); lblCap.classList.toggle('sel', !all);
  capEl.disabled = all;
}
$('#scope').addEventListener('change', syncScope); syncScope();
document.querySelectorAll('#examples .chip').forEach(c =>
  c.addEventListener('click', ()=>{ $('#query').value = c.dataset.q; }));
const skipEl=$('#skip'), akeyEl=$('#akey');
skipEl.addEventListener('change', ()=>{ akeyEl.disabled = skipEl.checked; akeyEl.style.opacity = skipEl.checked?.5:1; });

// On a non-loopback bind the server requires a per-startup token on every request. It is embedded
// here (the page was only served because the request already proved it has the token) and attached to
// EVERY fetch via X-Auth-Token, so all the existing fetch() call sites keep working unchanged.
const AUTH_TOKEN = "__AUTH_TOKEN__";
if (AUTH_TOKEN) {
  const _origFetch = window.fetch.bind(window);
  window.fetch = (u, o) => {
    o = o || {};
    o.headers = Object.assign({}, o.headers || {}, {"X-Auth-Token": AUTH_TOKEN});
    return _origFetch(u, o);
  };
}

// AI provider + model + per-provider key memory
const LLM = __LLM_CONFIG__;
const STAGE_DOCS = __STAGE_DOCS__;
const INSTANCE_TAG = "__INSTANCE_TAG__";   // this server instance's cluster JOB_TAG (name chosen at launch, else sra1/sra2/...)
const providerEl=$('#provider'), modelEl=$('#model'), keyhintEl=$('#keyhint');
const baseurlEl=$('#baseurl'), modelMenuEl=$('#modelmenu'), modelDropEl=$('#modeldrop'), maxtokensEl=$('#maxtokens');
let savedKeys = {};
let modelHistory = {};   // {provider: [model ids, most-recent-first]} — editable, persisted, ✕-deletable
let baseUrls = {};       // {provider: custom OpenAI-compatible base URL} — persisted per provider
let modelTokens = {};    // {model id: max output tokens} — per-model AI output cap, persisted

function syncMaxTokens(){ if(maxtokensEl) maxtokensEl.value = modelTokens[(modelEl.value||'').trim()] || ''; }
function histFor(p){ if(!Array.isArray(modelHistory[p])) modelHistory[p]=[]; return modelHistory[p]; }
function defaultModelFor(p){ return histFor(p)[0] || (LLM.models[p]||[])[0] || ''; }
function addModelToHistory(p, m){
  m=(m||'').trim(); if(!m) return;
  const h=histFor(p); const i=h.indexOf(m); if(i>=0) h.splice(i,1);   // move-to-front (dedupe)
  h.unshift(m); if(h.length>25) h.length=25;                          // cap the remembered list
}
function deleteModelFromHistory(p, m){
  const h=histFor(p), i=h.indexOf(m);
  if(i>=0){ h.splice(i,1); renderModelMenu(); persistModelMemory(); }
}
function commitModel(){ addModelToHistory(providerEl.value, modelEl.value); persistModelMemory(); syncMaxTokens(); }
function _esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }
function renderModelMenu(){
  const p=providerEl.value, h=histFor(p), sug=(LLM.models[p]||[]).filter(m=>h.indexOf(m)<0);
  let html='';
  if(h.length){
    html += '<div style="font-size:11px;color:var(--mut);padding:3px 7px 4px">Used with '+_esc(LLM.labels[p]||p)+'</div>';
    h.forEach(m=>{ html += '<div class="mrow" style="display:flex;align-items:center;gap:6px;padding:6px 7px;border-radius:7px">'
      + '<span class="mpick" data-m="'+_esc(m)+'" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer">'+_esc(m)+'</span>'
      + '<span class="mdel" data-m="'+_esc(m)+'" title="Remove from history" style="color:var(--mut);cursor:pointer;padding:0 5px;border-radius:5px;font-weight:700">&#10005;</span></div>'; });
  }
  if(sug.length){
    html += '<div style="font-size:11px;color:var(--mut);padding:6px 7px 4px;'+(h.length?'border-top:1px solid var(--line);margin-top:4px':'')+'">Suggested</div>';
    sug.forEach(m=>{ html += '<div class="mpick mrow" data-m="'+_esc(m)+'" style="padding:6px 7px;border-radius:7px;cursor:pointer">'+_esc(m)+'</div>'; });
  }
  if(!html) html='<div style="font-size:12px;color:var(--mut);padding:7px">No models yet — type one above.</div>';
  modelMenuEl.innerHTML=html;
}
function openModelMenu(){ renderModelMenu(); modelMenuEl.style.display='block'; }
function closeModelMenu(){ modelMenuEl.style.display='none'; }
modelDropEl.addEventListener('click', e=>{ e.preventDefault(); e.stopPropagation();
  (modelMenuEl.style.display==='block') ? closeModelMenu() : openModelMenu(); });
modelMenuEl.addEventListener('click', e=>{
  const del=e.target.closest('.mdel');
  if(del){ e.stopPropagation(); deleteModelFromHistory(providerEl.value, del.getAttribute('data-m')); return; }
  const pick=e.target.closest('.mpick');
  if(pick){ modelEl.value=pick.getAttribute('data-m'); commitModel(); closeModelMenu(); modelEl.focus(); }
});
modelMenuEl.addEventListener('mouseover', e=>{ const r=e.target.closest('.mrow'); if(r) r.style.background='var(--panel)'; });
modelMenuEl.addEventListener('mouseout', e=>{ const r=e.target.closest('.mrow'); if(r) r.style.background=''; });
document.addEventListener('click', e=>{ if(!e.target.closest('#modelcombo')) closeModelMenu(); });
modelEl.addEventListener('change', commitModel);   // typed a model + Enter/blur -> remember it for this provider
function syncProvider(){
  const p = providerEl.value;
  modelEl.value = defaultModelFor(p);                 // restore the last model used with this provider
  syncMaxTokens();                                    // restore this model's saved max-tokens
  if(baseurlEl) baseurlEl.value = baseUrls[p] || '';   // restore this provider's saved base URL
  akeyEl.value = savedKeys[p] || '';
  akeyEl.placeholder = LLM.keyHint[p] || 'API key';
  keyhintEl.textContent = 'Used for the AI cleaning passes. ' + (LLM.keyHint[p]||'');
  const br=$('#baseurlrow'); if(br) br.style.display = (p==='openai') ? '' : 'none';  // base_url = OpenAI custom-endpoint only; Ollama is always local (localhost / OLLAMA_HOST)
  closeModelMenu();
}
providerEl.addEventListener('change', syncProvider);
akeyEl.addEventListener('input', ()=>{ savedKeys[providerEl.value] = akeyEl.value; });
if(baseurlEl) baseurlEl.addEventListener('input', ()=>{ baseUrls[providerEl.value] = baseurlEl.value; });
if(maxtokensEl) maxtokensEl.addEventListener('input', ()=>{
  const m=(modelEl.value||'').trim(), v=maxtokensEl.value.trim();
  if(!m) return;
  if(v) modelTokens[m]=v; else delete modelTokens[m];   // blank -> fall back to the default
});
{ // alert email: persist on change + a "Send test" button (stall alerts are sent via the cluster's mail)
  const ae=$('#alertemail');
  if(ae) ae.addEventListener('change', ()=>{ try{ fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({alert_email:ae.value})}); }catch(ex){} });
  const at=$('#alerttest');
  if(at) at.onclick=async ()=>{ at.disabled=true; const old=at.textContent; at.textContent='Sending…';
    try{ const r=await (await fetch('/api/alert_test',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({alert_email:(ae?ae.value:''), ssh_password:(($('#sshpass')||{}).value||'')})})).json();
      alert(r.ok ? ('Test alert queued to '+r.email+'.\n'+(r.note||'')) : ('Could not send: '+(r.error||r.note||'unknown'))); }
    catch(e){ alert('Send failed: '+e.message); }
    finally{ at.disabled=false; at.textContent=old; } };
}
function persistModelMemory(){
  try{ fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({provider:providerEl.value, model:modelEl.value, model_history:modelHistory,
      base_urls:baseUrls, base_url:(baseurlEl?baseurlEl.value:''), model_max_tokens:modelTokens})}); }catch(ex){}
}
syncProvider();

function syncPick(){
  const manual = document.querySelector('input[name=pick][value=manual]').checked;
  $('#lblManual').classList.toggle('sel', manual); $('#lblAuto').classList.toggle('sel', !manual);
}
$('#pickmode').addEventListener('change', syncPick); syncPick();
function syncModule(){
  const m = (document.querySelector('input[name=module]:checked')||{}).value || 'bulk_rna_seq';
  const b=$('#lblBulkRna'); if(b) b.classList.toggle('sel', m==='bulk_rna_seq');
}
$('#modulesel').addEventListener('change', syncModule); syncModule();

function syncBedMode(){
  const m=(document.querySelector('input[name=bedmode]:checked')||{}).value||'intron';
  document.querySelectorAll('#bedmodesel label').forEach(l=>{const i=l.querySelector('input'); if(i) l.classList.toggle('sel', i.value===m);});
}
if($('#bedmodesel')) $('#bedmodesel').addEventListener('change', syncBedMode);
syncBedMode();

// ----- AltAnalyze comparison-groups editor (Phase B) -----
function addGroupRow(name, keywords, control){
  const host=$('#psigroups'); if(!host) return;
  const d=document.createElement('div'); d.className='row psigrp'; d.style.marginTop='6px';
  d.innerHTML='<input type="text" class="pg_name" placeholder="group name (e.g. control)" autocomplete="off">'+
    '<input type="text" class="pg_kw" placeholder="match keywords, comma-separated (e.g. dmso, vehicle)" autocomplete="off">'+
    '<label class="sel" style="white-space:nowrap"><input type="radio" name="pg_control"> baseline</label>'+
    '<button type="button" class="pg_rm" style="padding:2px 8px;cursor:pointer">&times;</button>';
  host.appendChild(d);
  if(name!=null) d.querySelector('.pg_name').value=name;
  if(keywords!=null) d.querySelector('.pg_kw').value=keywords;
  if(control) d.querySelector('.pg_control').checked=true;
  d.querySelector('.pg_rm').addEventListener('click', ()=>d.remove());
}
function collectGroups(){
  const out=[];
  document.querySelectorAll('#psigroups .psigrp').forEach(d=>{
    const name=((d.querySelector('.pg_name')||{}).value||'').trim(); if(!name) return;
    const kw=(((d.querySelector('.pg_kw')||{}).value||'').split(',')).map(s=>s.trim()).filter(Boolean);
    const ctrl=!!(d.querySelector('.pg_control')&&d.querySelector('.pg_control').checked);
    out.push({name:name, control:ctrl, keywords:kw});
  });
  return out;
}
if($('#psi_addgroup')) $('#psi_addgroup').addEventListener('click', ()=>addGroupRow());

// ----- pipeline phase-range slider (vertical dual-handle) -----
const CHECKPOINTS = __CHECKPOINTS__;
let startIdx = 0, endIdx = Math.max(0, CHECKPOINTS.length - 1);
const RAIL_H = Math.max(150, CHECKPOINTS.length * 32);
function _railTop(i){ return Math.round((CHECKPOINTS.length < 2 ? 0 : i/(CHECKPOINTS.length-1)) * RAIL_H); }
function renderStartInputs(){
  const box = $('#startInputs'); if(!box) return;
  const cp = CHECKPOINTS[startIdx], ins = (cp && cp.inputs) || [];
  if(startIdx === 0 || !ins.length){ box.innerHTML=''; box.hidden=true; return; }
  box.hidden=false;
  let h = '<span class="lbl">Start at "'+cp.label+'" — supply what the skipped phases would have produced</span>';
  ins.forEach(sp=>{
    const ph = sp.label + (sp.optional?' (optional)':'') + ' — absolute path on this machine' + (sp.kind==='dir'?' (folder)':'');
    h += '<input type="text" class="startinput" data-field="'+sp.field+'" placeholder="'+ph+'" autocomplete="off" spellcheck="false">';
    h += '<div class="hint">'+sp.desc+'</div>';
  });
  box.innerHTML = h;
}
function collectStartInputs(){
  const o = {};
  document.querySelectorAll('#startInputs .startinput').forEach(el=>{ const v=(el.value||'').trim(); if(v) o[el.dataset.field]=v; });
  return o;
}
function renderPhaseRail(){
  const track = $('#railTrack'); if(!track) return;
  const labels = $('#railLabels');
  track.style.height = RAIL_H+'px'; labels.style.height = RAIL_H+'px';
  $('#railStart').style.top = _railTop(startIdx)+'px';
  $('#railEnd').style.top = _railTop(endIdx)+'px';
  const f = $('#railFill'); f.style.top = _railTop(startIdx)+'px'; f.style.height = (_railTop(endIdx)-_railTop(startIdx))+'px';
  labels.innerHTML = '';
  CHECKPOINTS.forEach((c,i)=>{
    const d = document.createElement('div');
    d.className = 'railtick' + ((i>=startIdx && i<=endIdx)?' inrange':'') + ((i===startIdx||i===endIdx)?' edge':'');
    d.style.top = _railTop(i)+'px'; d.textContent = c.label;
    d.onclick = ()=>{ if(Math.abs(i-startIdx) <= Math.abs(i-endIdx)) startIdx=Math.min(i,endIdx); else endIdx=Math.max(i,startIdx); renderPhaseRail(); };
    labels.appendChild(d);
  });
  renderStartInputs();
}
(function wireRail(){
  let dragH = null;
  function pick(clientY){ const r=$('#railTrack').getBoundingClientRect(); let fr=(clientY-r.top)/r.height; fr=Math.max(0,Math.min(1,fr)); return Math.round(fr*(CHECKPOINTS.length-1)); }
  function move(e){ if(!dragH) return; const y=(e.touches?e.touches[0].clientY:e.clientY); const i=pick(y); if(dragH==='start') startIdx=Math.min(i,endIdx); else endIdx=Math.max(i,startIdx); renderPhaseRail(); e.preventDefault(); }
  function up(){ dragH=null; document.removeEventListener('pointermove',move); document.removeEventListener('pointerup',up); }
  ['railStart','railEnd'].forEach(id=>{ const h=$('#'+id); if(!h) return; h.addEventListener('pointerdown',e=>{ dragH=h.dataset.h; document.addEventListener('pointermove',move); document.addEventListener('pointerup',up); e.preventDefault(); }); });
})();
renderPhaseRail();

// cluster section
function syncClusterMode(){
  const m = document.querySelector('input[name=clmode]:checked').value;
  $('#lblAuton').classList.toggle('sel', m==='autonomous');
  $('#lblManualDl').classList.toggle('sel', m==='manual');
  $('#lblClOff').classList.toggle('sel', m==='off');
  $('#clfields').style.display = (m==='off') ? 'none' : '';
  $('#sshwrap').style.display = (m==='autonomous') ? '' : 'none';
}
$('#clmode').addEventListener('change', syncClusterMode);
syncClusterMode();

// ---- start ----
$('#form').addEventListener('submit', async e=>{
  e.preventDefault();
  const btn=$('#start'), err=$('#formErr');
  btn.disabled=true; err.hidden=true;
  addModelToHistory(providerEl.value, modelEl.value);   // remember the model being launched for this provider
  const body={
    query: $('#query').value,
    scope: $('input[name=scope]:checked').value,
    cap: capEl.value,
    skip_ai: skipEl.checked,
    provider: providerEl.value,
    api_key: akeyEl.value,
    api_keys: savedKeys,
    ncbi_key: $('#ncbi').value,
    model: modelEl.value,
    model_history: modelHistory,                 // persist per-provider model history across launches
    base_urls: baseUrls,                         // persist per-provider base URLs across launches
    model_max_tokens: modelTokens,               // per-model max output tokens for the AI cleaning calls
    base_url: ((($('#provider')||{}).value==='openai') ? (($('#baseurl')||{}).value||'') : ''),  // base_url = the OpenAI custom-endpoint ONLY (Ollama is local; Gemini fixed)
    disable_reasoning: (($('#noreason')||{}).checked || false),
    concurrency: $('#conc').value,
    alert_email: (($('#alertemail')||{}).value || ''),   // email on a cluster STALL (via the cluster's mail)
    diagnose_model_path: (($('#clmodelpath')||{}).value || ''),  // optional .gguf path for the cluster CPU diagnostic AI
    diagnose_model_dir: (($('#clmodeldir')||{}).value || ''),    // optional shared model-cache dir for the diagnostic AI
    deep_dive: true,
    start_stage: ((CHECKPOINTS[startIdx]||{}).stage || 'fetch'),
    end_stage: ((CHECKPOINTS[endIdx]||{}).end_stage || 'psi_submit'),
    supplied_inputs: collectStartInputs(),
    pick_mode: document.querySelector('input[name=pick]:checked').value,
    module: (document.querySelector('input[name=module]:checked')||{}).value || 'bulk_rna_seq',
    star: { GENOME_DIR:(($('#star_genome')||{}).value||''), ORGANISM:(($('#star_org')||{}).value||''),
            SJDB_GTF:(($('#star_gtf')||{}).value||''), STAR_INDEX_ROOT:(($('#star_indexroot')||{}).value||''),
            THREADS:(($('#star_threads')||{}).value||''), MEM_MB:(($('#star_mem')||{}).value||''),
            WALL:(($('#star_wall')||{}).value||''),
            DELETE_FASTQ_AFTER_BAM:((($('#del_fastq')||{}).checked)?'1':'0') },
    bed: { SPECIES:(($('#bed_species')||{}).value||''), MEM_MB:(($('#bed_mem')||{}).value||''),
           WALL:(($('#bed_wall')||{}).value||''), enabled:((($('#bed_enable')||{}).checked)?'1':'0'),
           BED_MODE:((document.querySelector('input[name=bedmode]:checked')||{}).value||'intron'),
           DELETE_BAM_AFTER_BED:((($('#del_bam')||{}).checked)?'1':'0') },
    psi: { enabled:((($('#psi_enable')||{}).checked)?'1':'0'),
           ALTANALYZE_HOME:(($('#psi_home')||{}).value||''), ALTANALYZE_DB:(($('#psi_db')||{}).value||''),
           ALTANALYZE_LOCAL:(($('#psi_local')||{}).value||''), SPECIES:(($('#psi_species')||{}).value||''),
           EXPNAME:(($('#psi_expname')||{}).value||''), MEM_MB:(($('#psi_mem')||{}).value||''),
           WALL:(($('#psi_wall')||{}).value||''), RUN_GOELITE:((($('#psi_goelite')||{}).checked)?'1':'0'),
           COMPRESS_WHEN_DONE:(($('#psi_compress')||{}).value||'gzip') },
    concordance: { enabled:((($('#conc_enable')||{}).checked)?'1':'0'),
           CANCER_ATLAS:(($('#conc_atlas')||{}).value||'auto'), DPSI:(($('#conc_dpsi')||{}).value||''),
           RAWP:(($('#conc_rawp')||{}).value||''), CONC_THRESHOLD:(($('#conc_thresh')||{}).value||''),
           REMOVE_IR:((($('#conc_removeir')||{}).checked)?'1':'0') },
    groups: { groups: (typeof collectGroups==='function'?collectGroups():[]) },
    cluster_mode: document.querySelector('input[name=clmode]:checked').value,
    cluster: {
      PIPELINE_ROOT: $('#clroot').value, SCRATCH_DIR: $('#clscratch').value,
      LSF_QUEUE: $('#clqueue').value, SRATOOLKIT_MODULE: $('#cltool').value,
      ASPERA_MODULE: $('#claspera').value, THREADS: $('#clthreads').value,
      MEM_MB: $('#clmem').value, WALL: $('#clwall').value,
      PREFETCH_MEM_MB: $('#clpfmem').value, JOB_TAG: $('#cljob').value,
      ssh_host: $('#sshhost').value, ssh_user: $('#sshuser').value,
      ssh_port: $('#sshport').value, ssh_key: $('#sshkey').value,
    },
    ssh_password: $('#sshpass').value,
  };
  try{
    const r = await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j = await r.json();
    if(!r.ok) throw new Error(j.error||'Could not start the run.');
    $('#setup').hidden=true; $('#run').hidden=false;
    $('#results').hidden=true; $('#newrun').hidden=true; $('#banner').innerHTML='';
    startPolling();
  }catch(ex){ err.textContent=ex.message; err.hidden=false; btn.disabled=false; }
});

$('#resume').addEventListener('click', async ()=>{
  const rb=$('#resume'), err=$('#formErr');
  rb.disabled=true; err.hidden=true;
  // pass the current AI settings as OVERRIDES so a resume can switch provider (e.g. proxy down -> Ollama),
  // toggle Skip AI, or supply a fresh SSH password; everything else comes from the run's saved config.json.
  const body={
    provider: (($('#provider')||{}).value||''),
    model: (($('#model')||{}).value||''),
    base_url: (((($('#provider')||{}).value)==='openai') ? (($('#baseurl')||{}).value||'') : ''),
    disable_reasoning: (($('#noreason')||{}).checked||false),
    skip_ai: (($('#skip')||{}).checked||false),
    ssh_password: (($('#sshpass')||{}).value||''),
  };
  try{
    const r=await fetch('/api/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok) throw new Error(j.error||'Could not resume.');
    $('#setup').hidden=true; $('#run').hidden=false;
    $('#results').hidden=true; $('#newrun').hidden=true; $('#banner').innerHTML='';
    startPolling();
  }catch(ex){ err.textContent=ex.message; err.hidden=false; rb.disabled=false; }
});

$('#newrun').addEventListener('click', ()=>{
  $('#run').hidden=true; $('#setup').hidden=false; $('#start').disabled=false;
});

// ---- polling + render ----
let polling=false;
function startPolling(){ if(polling) return; polling=true; tick(); }
async function tick(){
  try{
    const r = await fetch('/api/status');
    const s = await r.json();
    render(s);
    if(s.state==='done' || s.state==='error'){ polling=false; return; }
  }catch(ex){ /* transient; keep polling */ }
  if(polling) setTimeout(tick, 1000);
}

function stageRow(st, i){
  const icon = st.status==='done' ? '✓'
    : st.status==='skipped' ? '⊘'
    : st.status==='active' ? '<span class="spin">◐</span>' : '○';
  let mid='';
  if(st.status==='active'){
    if(st.total){
      const pct=Math.min(100, st.done/st.total*100);
      mid = '<div class="sbar"><span style="width:'+pct.toFixed(1)+'%"></span></div>'
          + '<div class="scount">'+st.done+' / '+st.total
          + (st.eta!=null ? ' • ~'+fmtDur(st.eta)+' left' : '') + '</div>';
    } else {
      mid = '<div class="sbar indet"><span></span></div>';
    }
  } else if(st.status==='done' && st.elapsed!=null){
    mid = '<div class="scount">done in '+fmtDur(st.elapsed)+'</div>';
  }
  const detail = (st.detail && st.status==='active') ? '<div class="sdetail">'+esc(st.detail)+'</div>' : '';
  return '<div class="stage '+st.status+'"><div class="sicon">'+icon+'</div>'
       + '<div class="sbody"><div class="slabel doc" data-doc="'+esc(st.key)+'" title="What happens in this step?">'
       + (i+1)+'. '+esc(st.label)+(st.status==='skipped'?' — skipped':'')+' <span class="qm">&#9432;</span></div>'
       + mid + detail + '</div></div>';
}

let lastLogLen=0;
function render(s){
  const m=s.meta||{};
  $('#meta').innerHTML =
      'Query: <b>'+esc(m.query||'')+'</b>'
    + ' &nbsp;·&nbsp; Studies: <b>'+esc(m.cap==='unlimited'||m.cap==null?'all':m.cap)+'</b>'
    + ' &nbsp;·&nbsp; '+(m.skip_ai?'<b>AI cleaning skipped</b>':'AI: <b>'+esc((m.provider||'')+(m.model?' / '+m.model:''))+'</b>');

  const pct = s.state==='done' ? 100 : Math.round((s.overall_fraction||0)*100);
  $('#obar').style.width = pct+'%';
  $('#opct').textContent = pct+'%';
  let eta='';
  if(s.state==='running'){
    eta = 'Elapsed '+fmtDur(s.elapsed) + (s.overall_eta!=null ? '  ·  ~'+fmtDur(s.overall_eta)+' remaining' : '  ·  estimating…');
  } else if(s.state==='done'){ eta = 'Completed in '+fmtDur(s.elapsed); }
  else if(s.state==='error'){ eta = 'Stopped after '+fmtDur(s.elapsed); }
  $('#oeta').textContent = eta;

  $('#stages').innerHTML = (s.stages||[]).map(stageRow).join('');
  $('#stages').querySelectorAll('.slabel.doc').forEach(el=>el.onclick=()=>openDoc(el.dataset.doc));
  maybeRevealPlots(s);
  renderSelect(s);
  renderAiFix(s);
  renderClusterFix(s);

  // log (append-aware autoscroll)
  const logEl=$('#log');
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  if((s.log||[]).length !== lastLogLen){
    logEl.innerHTML = (s.log||[]).map(l => '<span class="t">'+fmtDur(l.t).padStart(6,' ')+'</span>  '+esc(l.text)).join('\n');
    lastLogLen = (s.log||[]).length;
    if(atBottom) logEl.scrollTop = logEl.scrollHeight;
  }

  $('#rundir').textContent = m.run_dir ? ('run dir: '+m.run_dir) : '';

  // banners + results
  const banner=$('#banner');
  if(s.state==='done'){
    banner.className='banner ok'; banner.textContent='✓ Pipeline complete — tables ready below.';
    renderResults(s); $('#newrun').hidden=false; $('#start').disabled=false;
  } else if(s.state==='error'){
    banner.className='banner err'; banner.textContent='✗ Pipeline stopped: '+esc(s.error||'unknown error')+' — see the log.';
    $('#newrun').hidden=false; $('#start').disabled=false;
  } else { banner.className=''; banner.textContent=''; }
}

function renderResults(s){
  const box=$('#results'); const r=s.result||{}; const sp=r.splicing||{}, all=r.all||{}; const dd=r.deep_dive||null;
  let stats='<div class="stats">'
    + stat(sp.samples, 'splicing-amenable samples')
    + stat(sp.cell_lines, 'cell lines / buckets')
    + stat(sp.compounds, 'unique compounds')
    + (dd ? stat(dd.n_accessions, 'SRA runs · '+esc(dd.canonical||'top line')) : stat(all.samples, 'samples (all protocols)'))
    + '</div>';
  let ddline='';
  if(dd){
    ddline = '<div class="banner pick" style="margin-bottom:14px">Deep-dived <b>'+esc(dd.canonical||'')+'</b>'
      + (dd.n_studies!=null ? ' · '+dd.n_studies+' studies' : '')
      + (dd.n_accessions!=null ? ' · '+dd.n_accessions+' run accessions' : '')
      + (dd.matched_values&&dd.matched_values.length ? ' · matched: '+dd.matched_values.map(esc).join(', ') : '')
      + (dd.match_mode ? ' ('+esc(dd.match_mode)+')' : '') + '</div>';
  }
  const cl=r.cluster||null;
  let clline='';
  if(cl){
    const uploaded = (cl.mode==='autonomous' && cl.submitted);
    let tail;
    if(uploaded){
      tail = ' — uploaded &amp; launched on the cluster'
           + (cl.host ? ' (<code>'+esc(cl.host)+'</code>)' : '')
           + '; watch progress in the log (or <code>status.sh</code> on the cluster).';
    } else if(cl.mode==='autonomous'){
      tail = ' — autonomous upload did <b>not</b> complete; grab <b>cluster_bundle.zip</b> below and '
           + 'run it manually (see the log / RUN_ON_CLUSTER.txt).';
    } else {
      tail = ' — grab <b>cluster_bundle.zip</b> below to run it on your cluster '
           + '(see RUN_ON_CLUSTER.txt inside).';
    }
    clline = '<div class="banner '+(uploaded?'ok':'pick')+'" style="margin-bottom:14px">Cluster bundle ready'
      + (cl.n_studies!=null ? ' · '+cl.n_studies+' studies (each run separately)' : '')
      + (cl.pipeline_root ? ' · PIPELINE_ROOT <code>'+esc(cl.pipeline_root)+'</code>' : '')
      + tail + '</div>';
  }
  // featured (SraAccList, cluster zip) first, then headline, then the rest
  const files=(s.files||[]).slice().sort((a,b)=> (b.featured?2:b.headline?1:0)-(a.featured?2:a.headline?1:0));
  const filesHtml=files.map(f =>
    '<div class="file'+(f.headline||f.featured?' head':'')+'">'
    + '<span><span class="nm">'+esc(f.name)+'</span>'
    + (f.featured?'<span class="tag">MAIN OUTPUT</span>':f.headline?'<span class="tag">HEADLINE</span>':'')
    + ' <span class="sz">'+fmtSize(f.size)+'</span></span>'
    + '<a class="dl" href="/api/file?name='+encodeURIComponent(f.name)+'">Download</a></div>').join('');
  box.innerHTML = ddline + clline + stats + '<div class="files">'+filesHtml+'</div>';
  if(cl && cl.mode==='autonomous' && cl.submitted){
    box.innerHTML += '<div style="margin-top:14px"><button class="ghost" id="clstatBtn">Check cluster status</button>'
      + ' <span class="hint">poll the cluster for download/convert progress + ETA</span></div>'
      + '<div id="clusterstatus" style="margin-top:12px"></div>';
    const b=$('#clstatBtn'); if(b) b.onclick=()=>fetchClusterStatus('clusterstatus');
  }
  box.hidden=false;
}
function stat(n, k){ return '<div class="stat"><div class="n">'+(n==null?'—':Number(n).toLocaleString())+'</div><div class="k">'+esc(k)+'</div></div>'; }

// manual cell-line pick: render the ranked candidates when the run pauses for a choice
function renderSelect(s){
  const box=$('#selectpanel');
  if(!s.awaiting_selection){ box.hidden=true; box.innerHTML=''; return; }
  const cands=s.candidates||[];
  box.innerHTML =
    '<div class="banner pick">Pick the cell line to deep-dive — ranked by # unique compounds, then total reads:</div>'
    + '<div class="files">'
    + cands.slice(0,20).map(c =>
        '<div class="file"><span><span class="nm">'+esc(c.canonical)+'</span> <span class="sz">'
        + c.n_compounds+' compounds · '+Number(c.total_spots||0).toLocaleString()+' reads · '
        + c.n_studies+' studies</span></span>'
        + '<button class="dl pk" data-cl="'+encodeURIComponent(c.canonical)+'">Deep-dive</button></div>').join('')
    + '</div>'
    + '<button class="ghost" id="autopick" style="margin-top:10px">Auto-pick the best (top of list)</button>';
  box.hidden=false;
  box.querySelectorAll('button.pk').forEach(b => b.onclick = () => postSelect(decodeURIComponent(b.dataset.cl)));
  const ap=$('#autopick'); if(ap) ap.onclick = () => postSelect(null);
}
async function postSelect(cell_line){
  $('#selectpanel').innerHTML = '<div class="banner pick">Starting deep-dive'
    + (cell_line ? ' for '+esc(cell_line) : '') + '…</div>';
  try{ await fetch('/api/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cell_line})}); }catch(e){}
}

// cluster upload failed -> show the diagnosis + a prefilled form to fix and retry just the upload
function renderClusterFix(s){
  const box=$('#clusterfix');
  if(!s.awaiting_cluster_fix || !s.cluster_prompt){ box.hidden=true; box.dataset.built=''; box.innerHTML=''; return; }
  if(box.dataset.built==='1') return;          // don't clobber what the user is typing each poll
  const d=s.cluster_prompt.diagnosis||{}, c=s.cluster_prompt.current||{};
  const sus=new Set(d.suspect_fields||[]);
  const fld=(id,key,label,type)=>
    '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">'+esc(label)
    +(sus.has(key)?' <span class="tag" style="background:var(--warn);color:#1b1300">check this</span>':'')
    +'</span><input id="'+id+'" type="'+(type||'text')+'" value="'+esc(c[key]||'')+'" autocomplete="off"></label>';
  box.innerHTML =
      '<div class="banner err">Cluster upload failed — '+esc(d.title||'unknown')+'</div>'
    + '<div class="hint" style="margin:-6px 0 14px">'+esc(d.detail||'')
    + ' The rest of the pipeline is done — fix the details below and retry just the upload.</div>'
    + fld('cf_host','ssh_host','SSH host')
    + '<div class="row">'+fld('cf_user','ssh_user','SSH username')+fld('cf_port','ssh_port','SSH port')+'</div>'
    + fld('cf_key','ssh_key','Private key file (optional)')
    + fld('cf_pass','ssh_password','SSH password (optional; blank = key/agent)','password')
    + fld('cf_root','PIPELINE_ROOT','PIPELINE_ROOT (cluster path)')
    + '<div style="display:flex;gap:10px;margin-top:4px">'
    + '<button class="primary" id="cfRetry">Retry upload</button>'
    + '<button class="ghost" id="cfSkip">Skip — I\'ll run it manually</button></div>';
  box.hidden=false; box.dataset.built='1';
  $('#cfRetry').onclick=()=>postClusterRetry(false);
  $('#cfSkip').onclick=()=>postClusterRetry(true);
}
async function postClusterRetry(cancel){
  const box=$('#clusterfix');
  let body;
  if(cancel){ body={action:'cancel'}; }
  else{
    body={cluster:{
      ssh_host:$('#cf_host').value, ssh_user:$('#cf_user').value, ssh_port:$('#cf_port').value,
      ssh_key:$('#cf_key').value, PIPELINE_ROOT:$('#cf_root').value },
      ssh_password:$('#cf_pass').value};
  }
  box.innerHTML='<div class="banner pick">'+(cancel?'Skipping cluster upload…':'Retrying cluster upload…')+'</div>';
  box.dataset.built='1';
  try{ await fetch('/api/cluster_retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }catch(e){}
}

// AI preflight failed (bad provider / model / key) -> fix the info or turn AI off, then continue
function renderAiFix(s){
  const box=$('#aifix');
  if(!s.awaiting_ai_fix || !s.ai_prompt){ box.hidden=true; box.dataset.built=''; box.innerHTML=''; return; }
  if(box.dataset.built==='1') return;          // don't clobber what the user is typing each poll
  const d=s.ai_prompt.diagnosis||{}, c=s.ai_prompt.current||{};
  box.innerHTML =
      '<div class="banner err">AI cleaning paused — '+esc(d.title||'unknown')+'</div>'
    + '<div class="hint" style="margin:-6px 0 14px">'+esc(d.detail||'')
    + ' Fix the details below and retry (a mid-run outage resumes from where it stopped — only the '
    + 'unanswered batches re-run), or turn AI off to finish deterministically.</div>'
    + '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">Provider</span>'
    + '<select id="af_prov"><option value="anthropic">Anthropic (Claude)</option>'
    + '<option value="openai">OpenAI (ChatGPT)</option><option value="gemini">Google Gemini</option><option value="ollama">Ollama (local)</option></select></label>'
    + '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">Model</span>'
    + '<input id="af_model" type="text" value="'+esc(c.model||'')+'" autocomplete="off"></label>'
    + '<label class="fld" id="af_baseurlrow" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">Base URL (OpenAI-compatible endpoint — blank = api.openai.com)</span>'
    + '<input id="af_baseurl" type="text" value="'+esc(c.base_url||'')+'" placeholder="https://openrouter.ai/api/v1" autocomplete="off"></label>'
    + '<label class="fld" style="margin-bottom:10px"><span class="lbl" style="font-size:13px">API key (blank = keep current)</span>'
    + '<input id="af_key" type="password" value="" autocomplete="off"></label>'
    + '<div style="display:flex;gap:10px;margin-top:4px">'
    + '<button class="primary" id="afRetry">Retry AI</button>'
    + '<button class="ghost" id="afSkip">Turn off AI — run without it</button></div>';
  box.hidden=false; box.dataset.built='1';
  const sel=$('#af_prov'); if(sel && c.provider) sel.value=c.provider;
  syncAfProvider(true);                       // toggle the base-URL row + prefill it from per-provider memory
  if(sel) sel.addEventListener('change', ()=>syncAfProvider(false));
  $('#afRetry').onclick=()=>postAiRetry(false);
  $('#afSkip').onclick=()=>postAiRetry(true);
}
// Base URL is the OpenAI custom-endpoint (honored for OpenAI; ignored by local Ollama, which uses OLLAMA_HOST) -> show it for OpenAI only.
function syncAfProvider(initial){
  const p=$('#af_prov').value, row=$('#af_baseurlrow'); if(!row) return;
  row.style.display=(p==='openai')?'':'none';
  if(!initial){                               // user switched provider -> load that provider's remembered model + base URL
    $('#af_model').value   = defaultModelFor(p);
    $('#af_baseurl').value = baseUrls[p] || '';
  }else if(p==='openai' && !$('#af_baseurl').value && baseUrls[p]){
    $('#af_baseurl').value = baseUrls[p];      // first render with no run base_url -> show a remembered endpoint
  }
}
async function postAiRetry(skip){
  const box=$('#aifix');
  let body;
  if(skip){
    body={action:'skip_ai'};
  }else{
    const prov=$('#af_prov').value, model=$('#af_model').value.trim(),
          key=$('#af_key').value, bu=($('#af_baseurl')?$('#af_baseurl').value.trim():'');
    body={provider:prov, model:model, api_key:key, base_url:bu};
    // remember what was entered in the SAME per-provider store the launch form uses, then persist it
    if(model) addModelToHistory(prov, model);
    baseUrls[prov]=bu;
    if(key) savedKeys[prov]=key;                          // blank key = keep the current one (don't overwrite)
    providerEl.value=prov; modelEl.value=model||defaultModelFor(prov);   // reflect the fix on the launch form too
    if(baseurlEl) baseurlEl.value=bu;
    const br=$('#baseurlrow'); if(br) br.style.display=(prov==='openai')?'':'none';
    const persist={provider:prov, model:model||defaultModelFor(prov),
      model_history:modelHistory, base_urls:baseUrls, base_url:bu, model_max_tokens:modelTokens};
    if(key) persist.api_keys=savedKeys;                   // only persist keys when one was entered (avoid wiping on a key-stripped bind)
    try{ fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(persist)}); }catch(ex){}
  }
  box.innerHTML='<div class="banner pick">'+(skip?'Turning off AI…':'Retrying AI…')+'</div>';
  box.dataset.built='1';
  try{ await fetch('/api/ai_retry',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); }catch(e){}
}

// ---- step-doc modal (click a pipeline step to read what it does) ----
function openDoc(key){
  const d=STAGE_DOCS[key]; if(!d) return;
  $('#docTitle').textContent=d.title||key;
  $('#docBody').innerHTML = '<p>'+esc(d.what||'')+'</p>'
    + (d.inputs?'<div class="docmeta"><b>Inputs:</b> '+esc(d.inputs)+'</div>':'')
    + (d.outputs?'<div class="docmeta"><b>Outputs:</b> '+esc(d.outputs)+'</div>':'');
  $('#docmodal').hidden=false;
}
function closeDoc(){ $('#docmodal').hidden=true; }

// ---- tabs (Run | Plots) ----
function showTab(t){
  $('#runtab').hidden = (t!=='run');
  $('#plots').hidden  = (t!=='plots');
  $('#chat').hidden   = (t!=='chat');
  $('#tabRun').classList.toggle('on', t==='run');
  $('#tabPlots').classList.toggle('on', t==='plots');
  $('#tabAssistant').classList.toggle('on', t==='chat');
  if(t==='plots') openPlots();
}
function maybeRevealPlots(s){
  const ready = s && (s.state==='done'
    || (s.stages||[]).some(st=>(st.key==='cellline_match'||st.key==='runtable_annotate') && st.status==='done'));
  if(ready) $('#tabPlots').hidden=false;
}

// ============================ Assistant (chat) ============================
let CHAT=[]; let CHAT_BUSY=false;
try{ CHAT = JSON.parse(sessionStorage.getItem('ss_chat')||'[]')||[]; }catch(e){ CHAT=[]; }
function chatSave(){ try{ sessionStorage.setItem('ss_chat', JSON.stringify(CHAT)); }catch(e){} }
function fmtSQL(s){
  return (s||'').replace(/\b(select|from|where|group by|order by|having|inner join|left join|right join|join|on|and|or|as|limit|offset|count|sum|avg|min|max|distinct|with|case|when|then|else|end|desc|asc|like|in|not|null|is|union|all|between)\b/gi, m=>m.toUpperCase());
}
function codeBlock(lang, code){
  let body=code;
  if(lang==='json'){ try{ body=JSON.stringify(JSON.parse(code), null, 2); }catch(e){} }
  else if(lang==='sql'){ body=fmtSQL(code.trim()); }
  const id='cb'+Math.random().toString(36).slice(2);
  return '<div style="position:relative;margin:8px 0">'
    +'<div style="font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:2px">'+esc(lang||'code')+'</div>'
    +'<button type="button" class="copybtn ghost" data-cb="'+id+'" style="position:absolute;top:16px;right:6px;font-size:10px;padding:1px 7px">copy</button>'
    +'<pre style="margin:0;background:var(--panel,#f6f6f4);border:1px solid var(--line);border-radius:8px;padding:10px 12px;overflow:auto;font-size:12.5px"><code id="'+id+'">'+esc(body)+'</code></pre></div>';
}
function mdInline(s){
  return esc(s)
    .replace(/`([^`]+)`/g,'<code style="background:var(--line);padding:1px 4px;border-radius:4px;font-size:12.5px">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g,'<a href="$2" target="_blank">$1</a>');
}
function mdToHtml(text){
  const parts=(text||'').replace(/\r/g,'').split('```'); let html='';
  for(let i=0;i<parts.length;i++){
    if(i%2===1){
      const seg=parts[i], nl=seg.indexOf('\n');
      const lang=(nl>=0?seg.slice(0,nl):'').trim().toLowerCase();
      html+=codeBlock(lang, (nl>=0?seg.slice(nl+1):seg).replace(/\n$/,''));
    } else {
      const lines=parts[i].split('\n'); let inList=false, buf='';
      for(const ln of lines){
        if(/^\s*[-*]\s+/.test(ln)){ if(!inList){buf+='<ul style="margin:4px 0 4px 18px;padding:0">';inList=true;} buf+='<li style="margin:2px 0">'+mdInline(ln.replace(/^\s*[-*]\s+/,''))+'</li>'; }
        else { if(inList){buf+='</ul>';inList=false;} if(ln.trim()) buf+='<div style="margin:3px 0">'+mdInline(ln)+'</div>'; }
      }
      if(inList) buf+='</ul>';
      html+=buf;
    }
  }
  return html;
}
function chatBubble(role, inner){
  const me=role==='user';
  return '<div style="display:flex;justify-content:'+(me?'flex-end':'flex-start')+';margin:8px 0">'
    +'<div style="max-width:88%;padding:9px 12px;border-radius:12px;font-size:13.5px;line-height:1.5;'
    +(me?'background:var(--accent,#534ab7);color:#fff':'background:var(--panel,#f6f6f4);border:1px solid var(--line)')+'">'
    +inner+'</div></div>';
}
function renderChat(){
  const log=$('#chatlog'); if(!log) return;
  if(!CHAT.length){ log.innerHTML='<div class="hint" style="text-align:center;padding:24px 10px">Hi! Tell me what you want to study and I\'ll set up the run, write &amp; test the GEO query, answer questions, chart your data, or read the cluster logs.<br><br><i>e.g. "Find human RNA-seq drug-treatment studies and set it up."</i></div>'; return; }
  let html='';
  CHAT.forEach((m,i)=>{
    if(m.role==='user'){ html+=chatBubble('user', esc(m.content)); return; }
    let inner=mdToHtml(m.content);
    if(m._figs&&m._figs.length) inner+='<div id="cc'+i+'"></div>';
    if(m._tools&&m._tools.length) inner+='<div style="margin-top:6px;font-size:11px;color:var(--mut)">used: '+m._tools.map(esc).join(', ')+'</div>';
    html+=chatBubble('assistant', inner);
  });
  log.innerHTML=html;
  CHAT.forEach((m,i)=>{ if(m._figs&&m._figs.length){ ensurePlotly().then(()=>{
    const host=document.getElementById('cc'+i); if(!host) return; host.innerHTML='';
    m._figs.forEach(fig=>{ const d=document.createElement('div'); d.style.margin='8px 0'; host.appendChild(d);
      try{ Plotly.newPlot(d, fig.data||[], Object.assign({autosize:true,margin:{t:36,r:12,b:48,l:52}}, fig.layout||{}), {displayModeBar:false,responsive:true}); }catch(e){} });
  }).catch(()=>{}); } });
  log.scrollTop=log.scrollHeight;
}
async function sendChat(){
  const box=$('#chatbox'); if(!box) return; const text=box.value.trim();
  if(!text||CHAT_BUSY) return;
  CHAT.push({role:'user',content:text}); box.value=''; chatSave(); renderChat();
  CHAT_BUSY=true; $('#chatwork').hidden=false; $('#chatsend').disabled=true;
  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({messages:CHAT.map(m=>({role:m.role,content:m.content}))})});
    const d=await r.json();
    const msg={role:'assistant',content:d.reply||d.error||'(no reply)'};
    if(d.charts&&d.charts.length) msg._figs=d.charts;
    if(d.trace&&d.trace.length) msg._tools=d.trace.map(t=>t.tool);
    CHAT.push(msg); chatSave(); renderChat();
    if(d.settings_changed&&Object.keys(d.settings_changed).length){
      try{ applySettings(await (await fetch('/api/settings')).json()); }catch(e){}
      const log=$('#chatlog');
      log.insertAdjacentHTML('beforeend','<div style="text-align:center;margin:6px 0"><span style="border:1px solid var(--line);color:var(--mut);font-size:11px;padding:3px 10px;border-radius:10px">&#10003; updated the form: '+esc(Object.keys(d.settings_changed).join(', '))+'</span></div>');
      log.scrollTop=log.scrollHeight;
    }
  }catch(ex){ CHAT.push({role:'assistant',content:'Sorry — the request failed: '+(ex&&ex.message||String(ex))}); chatSave(); renderChat(); }
  finally{ CHAT_BUSY=false; $('#chatwork').hidden=true; $('#chatsend').disabled=false; }
}
document.addEventListener('click', e=>{
  const b = e.target.closest && e.target.closest('.copybtn'); if(!b) return;
  const el=document.getElementById(b.getAttribute('data-cb'));
  if(el&&navigator.clipboard){ navigator.clipboard.writeText(el.textContent); b.textContent='copied'; setTimeout(()=>{b.textContent='copy';},1200); }
});

// ---- Plots tab (Plotly, vendored at /plotly.js; loaded lazily on first open) ----
let PLOTDATA=null, _plotlyStarted=false;
function ensurePlotly(){
  return new Promise((resolve,reject)=>{
    if(window.Plotly) return resolve();
    if(_plotlyStarted){ const i=setInterval(()=>{ if(window.Plotly){clearInterval(i);resolve();} },80); return; }
    _plotlyStarted=true;
    const sc=document.createElement('script'); sc.src='/plotly.js';
    sc.onload=()=>resolve(); sc.onerror=()=>reject(new Error('could not load the plotting library'));
    document.head.appendChild(sc);
  });
}
async function loadPlotData(force){
  if(PLOTDATA && !force) return PLOTDATA;
  const d = await (await fetch('/api/plotdata')).json();
  PLOTDATA = (d && d.available) ? d : null;
  return PLOTDATA;
}
async function openPlots(){
  const host=$('#plotsbody');
  try{
    const d = await loadPlotData(false);
    if(!d){ host.innerHTML='<div class="hint">No plot data yet — plots use the deep-dived cell line\'s runs, available after the <b>match cell-line names</b> step. Reopen this tab then.</div>'; return; }
    await ensurePlotly();
    buildPlotsUI(d);
  }catch(ex){ host.innerHTML='<div class="err">Plots unavailable: '+esc(ex.message)+'</div>'; }
}
const LABELS={spots:'read depth (spots)',avg_spot_len:'avg spot length (bp)',bases:'bases',
  study:'study',drug:'drug',drug_treated:'drug treated',is_control:'control',dose:'dose',
  instrument:'instrument',library_selection:'library selection',platform:'platform',assay:'assay',
  layout:'library layout',source_name:'source name',treatment:'treatment'};
const lab=v=>LABELS[v]||v;
const PCONF={displayModeBar:false,responsive:true};
function buildPlotsUI(d){
  const studies=d.studies||[];
  $('#plotsbody').innerHTML =
      '<div style="display:flex;justify-content:space-between;align-items:baseline">'
    + '<h3 class="psec" style="margin-top:0">'+esc(d.cell_line||'cell line')+' &mdash; studies ('+studies.length+' · '+d.samples.length+' runs)</h3>'
    + '<button class="ghost" id="plReload" style="padding:6px 12px">&#8635; Reload</button></div>'
    + '<div class="hint" style="margin-bottom:8px">Only the picked cell line\'s runs are shown. Click a study to chart read depth and spot length per run.</div>'
    + '<div class="studylist" id="studylist">'
    + studies.map(st=>'<div class="studyitem" data-gse="'+esc(st.gse)+'">'
        + '<span class="snm">'+esc(st.gse)+'</span>'
        + '<span class="sttl">'+esc(st.title||'')+'</span>'
        + '<span class="sn">'+st.n_samples+' runs</span></div>').join('')
    + '</div><div id="studycharts"></div>' + customPlotterHTML(d);
  $('#studylist').querySelectorAll('.studyitem').forEach(el=>el.onclick=()=>{
    $('#studylist').querySelectorAll('.studyitem').forEach(x=>x.classList.remove('sel'));
    el.classList.add('sel'); renderStudyCharts(el.dataset.gse);
  });
  const rl=$('#plReload'); if(rl) rl.onclick=async()=>{ await loadPlotData(true); buildPlotsUI(PLOTDATA); };
  bindCustomPlotter(d);
  if(!window._plResize){ window._plResize=true; window.addEventListener('resize', ()=>{
    document.querySelectorAll('.chart').forEach(c=>{ if(c.data && window.Plotly) Plotly.Plots.resize(c); }); }); }
}
function plotLayout(title, extra){
  return Object.assign({ title:{text:title,font:{color:'#e7ecf5',size:14}},
    paper_bgcolor:'rgba(0,0,0,0)', plot_bgcolor:'rgba(0,0,0,0)', font:{color:'#9aa6c0'},
    autosize:true, height:360, margin:{l:90,r:20,t:42,b:46},
    legend:{font:{color:'#9aa6c0'},orientation:'h',y:-0.18},
    xaxis:{gridcolor:'#2b2b31',zerolinecolor:'#2b2b31',automargin:true},
    yaxis:{gridcolor:'#2b2b31',zerolinecolor:'#2b2b31',automargin:true} }, extra||{});
}
function iqrBox(id, vals, labels, title){
  if(!vals.filter(v=>v!=null).length){ $('#'+id).innerHTML='<div class="hint">No data.</div>'; return; }
  Plotly.newPlot(id, [{ x:vals, type:'box', boxpoints:'all', jitter:0.5, pointpos:0, orientation:'h',
    marker:{color:'#5fb6c0',size:5,opacity:.6}, line:{color:'#5fb6c0'}, fillcolor:'rgba(95,182,192,.12)',
    text:labels, hovertemplate:'%{text}<br>%{x}<extra></extra>', name:'' }],
    plotLayout(title,{height:210,yaxis:{showticklabels:false,gridcolor:'#2b2b31',zerolinecolor:'#2b2b31'}}), PCONF);
}
function renderStudyCharts(gse){
  const rows=(PLOTDATA.samples||[]).filter(s=>s.study===gse);
  $('#studycharts').innerHTML =
      '<h3 class="psec">'+esc(gse)+' &mdash; read depth per run ('+rows.length+')</h3><div id="chartDepth" class="chart"></div>'
    + '<h3 class="psec">'+esc(gse)+' &mdash; spot length (avg read length) per run</h3><div id="chartLen" class="chart"></div>';
  iqrBox('chartDepth', rows.map(r=>r.spots), rows.map(r=>r.run), 'Read depth (spots)');
  const len=rows.filter(r=>r.avg_spot_len!=null);
  if(len.length) iqrBox('chartLen', len.map(r=>r.avg_spot_len), len.map(r=>r.run), 'Avg spot length (bp)');
  else $('#chartLen').innerHTML='<div class="hint">No spot-length data for this study.</div>';
}
function customPlotterHTML(d){
  const allvars=d.categorical.concat(d.numeric);
  const opt=(arr,sel)=>arr.map(v=>'<option value="'+v+'"'+(v===sel?' selected':'')+'>'+esc(lab(v))+'</option>').join('');
  const defX=d.categorical.includes('drug_treated')?'drug_treated':(d.categorical[0]||'study');
  const defY=d.numeric.includes('spots')?'spots':(d.numeric[0]||'spots');
  return '<h3 class="psec">Custom plot</h3>'
   +'<div class="hint" style="margin-bottom:8px">'+d.samples.length+' runs of '+esc(d.cell_line||'the cell line')+'. Pick variables and a chart type.</div>'
   +'<div class="row">'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">Chart type</span><select id="cpType">'
     +'<option value="box">Box (IQR + dots)</option><option value="violin">Violin</option>'
     +'<option value="bar">Bar (count)</option><option value="scatter">Scatter</option>'
     +'<option value="histogram">Histogram</option><option value="heatmap">Heatmap (counts)</option>'
     +'<option value="density">2D density</option></select></label>'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">X / group</span><select id="cpX">'+opt(allvars,defX)+'</select></label>'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">Y (numeric)</span><select id="cpY">'+opt(d.numeric,defY)+'</select></label>'
   +'<label class="fld" style="margin:0"><span class="lbl" style="font-size:12px">Color / 2nd</span><select id="cpColor"><option value="">none</option>'+opt(d.categorical,'')+'</select></label>'
   +'</div><div class="hint" id="cpHint" style="margin-bottom:8px"></div>'
   +'<button class="ghost" id="cpPlot" style="margin-bottom:10px">Plot</button><div id="customchart" class="chart"></div>';
}
function bindCustomPlotter(d){
  const b=$('#cpPlot'); if(!b) return;
  const upd=()=>{ const t=$('#cpType').value;
    const need={box:'Y by X (split by Color)',violin:'Y by X (split by Color)',bar:'run count of X (stacked by Color)',
      scatter:'X vs Y (colored by Color)',histogram:'distribution of Y (split by Color)',
      heatmap:'run counts of X × Color',density:'2D density of X (numeric) vs Y'};
    $('#cpHint').textContent=need[t]||''; };
  $('#cpType').onchange=upd; upd();
  b.onclick=()=>renderCustom(d); renderCustom(d);
}
function renderCustom(d){
  const type=$('#cpType').value, x=$('#cpX').value, y=$('#cpY').value, color=$('#cpColor').value;
  const S=d.samples;
  const groups = color ? [...new Set(S.map(r=>r[color]))].filter(g=>g!=='').sort() : [null];
  const pick=g=> color ? S.filter(r=>r[color]===g) : S;
  const H=h=>({height:h});
  let traces=[];
  if(type==='heatmap'){
    const c2 = color || (d.categorical.find(v=>v!==x) || x);
    const xs=[...new Set(S.map(r=>String(r[x])))].sort(), ys=[...new Set(S.map(r=>String(r[c2])))].sort();
    const z=ys.map(yy=>xs.map(xx=>S.filter(r=>String(r[x])===xx && String(r[c2])===yy).length));
    Plotly.newPlot('customchart',[{type:'heatmap',x:xs,y:ys,z:z,colorscale:'Blues',
      hovertemplate:lab(x)+'=%{x}<br>'+lab(c2)+'=%{y}<br>%{z} runs<extra></extra>'}],
      plotLayout('run counts: '+lab(x)+' × '+lab(c2), H(Math.max(280, 70+30*ys.length))), PCONF); return;
  }
  if(type==='density'){
    Plotly.newPlot('customchart',[{type:'histogram2d',x:S.map(r=>r[x]),y:S.map(r=>r[y]),colorscale:'Blues'}],
      plotLayout(lab(y)+' vs '+lab(x)+' (density)', H(420)), PCONF); return;
  }
  if(type==='bar'){
    const cats=[...new Set(S.map(r=>String(r[x])))].sort();
    groups.forEach(g=>{ const rows=pick(g);
      traces.push({type:'bar', x:cats, y:cats.map(c=>rows.filter(r=>String(r[x])===c).length), name:g==null?'runs':String(g)}); });
    Plotly.newPlot('customchart',traces,plotLayout('run count by '+lab(x),Object.assign({barmode:'stack'},H(380))),PCONF); return;
  }
  if(type==='histogram'){
    groups.forEach(g=>traces.push({type:'histogram', x:pick(g).map(r=>r[y]).filter(v=>v!=null), name:g==null?lab(y):String(g), opacity:.7}));
    Plotly.newPlot('customchart',traces,plotLayout(lab(y)+' distribution',Object.assign({barmode:'overlay'},H(380))),PCONF); return;
  }
  if(type==='scatter'){
    groups.forEach(g=>{ const rows=pick(g);
      traces.push({type:'scatter',mode:'markers', x:rows.map(r=>r[x]), y:rows.map(r=>r[y]), text:rows.map(r=>r.run),
        name:g==null?'':String(g), marker:{size:6,opacity:.6}}); });
    Plotly.newPlot('customchart',traces,plotLayout(lab(y)+' vs '+lab(x),H(420)),PCONF); return;
  }
  // box / violin — HORIZONTAL: Y(numeric) distribution by X(category), split by Color
  const xs=[...new Set(S.map(r=>String(r[x])))];
  const h=Math.max(300, 50+28*Math.max(xs.length, xs.length*groups.length));
  groups.forEach(g=>{ const rows=pick(g);
    traces.push(Object.assign({type:type, x:rows.map(r=>r[y]), y:rows.map(r=>String(r[x])), orientation:'h',
      text:rows.map(r=>r.run), name:g==null?'':String(g)},
      type==='box'?{boxpoints:'all',jitter:.4,pointpos:0,marker:{size:4,opacity:.5}}:{points:'all'})); });
  Plotly.newPlot('customchart',traces,plotLayout(lab(y)+' by '+lab(x),Object.assign({boxmode:'group',violinmode:'group'},H(h))),PCONF);
}

// ---- on-demand cluster status (checked only when you click the button) ----

// least-squares slope (runs/sec) over [{t,c}] points; null if <2 points
function clstatSlope(pts){
  const n=pts.length; if(n<2) return null;
  let st=0,sc=0,stt=0,stc=0;
  for(const p of pts){ st+=p.t; sc+=p.c; stt+=p.t*p.t; stc+=p.t*p.c; }
  const den=n*stt-st*st; if(den===0) return null;
  return (n*stc-st*sc)/den;
}
// record this check's converted count (persisted, keyed by job+root) so the ETA refines with every
// check. Resets if a new run reuses the same tag (total changes, or converted regresses).
function clstatRecord(d){
  const ov=d.overall||{}; if(ov.exp==null||ov.converted==null) return null;
  const key='clstat_hist_'+(d.job_tag||'def')+'_'+(d.root||'');
  let hist=[]; try{ hist=JSON.parse(localStorage.getItem(key)||'[]'); }catch(e){ hist=[]; }
  const last=hist.length?hist[hist.length-1]:null;
  if(last && (ov.exp!==last.e || ov.converted<last.c)) hist=[];     // new run on same tag -> reset
  const now=Math.floor(Date.now()/1000);
  if(!hist.length || now-hist[hist.length-1].t>=5) hist.push({t:now,c:ov.converted,e:ov.exp});
  if(hist.length>300) hist=hist.slice(-300);
  try{ localStorage.setItem(key, JSON.stringify(hist)); }catch(e){}
  return hist;
}
// ETA seconds to finish converting. Primary = rate observed across YOUR checks (refines each check);
// first-check fallback = the cluster watchdog's own logged rate.
function clstatEta(d, hist){
  const ov=d.overall||{};
  if(hist && hist.length>=2 && ov.exp!=null){
    const t0=hist[0].t, r=clstatSlope(hist.map(p=>({t:p.t-t0,c:p.c})));
    if(r && r>0){ const rem=ov.exp-ov.converted;
      return {sec: rem<=0?0:Math.round(rem/r), src:'your '+hist.length+' checks'}; }
  }
  if(d.eta_seconds!=null) return {sec:d.eta_seconds, src:'cluster log'};
  return {sec:null, src:null};
}
async function fetchClusterStatus(panelId){
  panelId = panelId || 'clusterstatus';
  const host=$('#'+panelId);
  if(!host) return;
  host.innerHTML='<div class="hint">Checking the cluster…</div>';
  try{
    const d = await (await fetch('/api/cluster_status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_tag:INSTANCE_TAG})})).json();
    if(!d.ok){
      host.innerHTML='<div class="err">'+esc((d.diagnosis&&d.diagnosis.title)||d.error||'Could not reach the cluster')
          +(d.diagnosis&&d.diagnosis.detail?' — '+esc(d.diagnosis.detail):'')+'</div>';
      return;
    }
    renderClusterStatus(d, panelId);
  }catch(ex){
    host.innerHTML='<div class="err">'+esc(ex.message)+'</div>';
  }
}
function renderClusterStatus(d, panelId){
  panelId = panelId || 'clusterstatus';
  const host=$('#'+panelId), ov=d.overall||{};
  const hist=clstatRecord(d), eta=clstatEta(d, hist);   // refine the ETA from every check
  const tag = d.job_tag ? ' ('+esc(d.job_tag)+')' : '';
  let h='';
  if(d.alerts && d.alerts.length){   // cross-stage STALL alert (surfaces a silent downstream stall RED)
    h += '<div class="err" style="font-weight:600;padding:8px 10px;border-radius:8px;margin-bottom:8px">⚠ '
       + d.alerts.length+' stage'+(d.alerts.length>1?'s':'')+' STALLED / failed — needs attention:'
       + d.alerts.map(a=>'<div style="font-weight:400;font-size:12.5px;margin-top:3px">• <b>'+esc(a.stage)+'</b> '+esc(a.kind)+' <span style="color:var(--mut)">('+esc(a.when)+')</span></div>').join('')
       + '<div style="font-weight:400;font-size:11.5px;margin-top:5px;color:var(--mut)">A stalled stage does NOT auto-advance — fix + re-arm it (or ask the Assistant: &ldquo;is anything stuck?&rdquo;).</div></div>';
  }
  h+='<div class="banner '+(d.complete?'ok':(d.stalled?'err':'pick'))+'">'
    + (d.complete?'✓ Cluster pipeline complete':(d.stalled?'⚠ Watchdog stopped (stalled)':'Cluster progress'))+tag
    + (ov.exp!=null?' &mdash; '+ov.converted+' converted &middot; '+ov.downloaded+' downloaded / '+ov.exp+' runs ('+ov.pct+'%)':'')
    + (d.live_jobs!=null?' &middot; '+d.live_jobs+' active jobs':'')
    + (eta.sec!=null&&!d.complete?' &middot; ~'+fmtDur(eta.sec)+' left to convert':'') + '</div>';
  if(d.star){
    const s=d.star, so=s.overall||{};
    const phase = s.complete ? '✓ STAR alignment complete'
      : s.stalled ? '⚠ STAR stalled'
      : s.launch_pending ? 'STAR queued — waiting for the download to finish'
      : s.building ? 'STAR — building genome index…'
      : 'STAR aligning';
    h += '<div class="banner '+(s.complete?'ok':(s.stalled?'err':'pick'))+'" style="margin-top:6px">'
       + phase + (so.exp?' &mdash; '+so.done+' / '+so.exp+' BAMs ('+so.pct+'%)':'')
       + (s.live_jobs!=null?' &middot; '+s.live_jobs+' jobs':'')
       + (s.eta_seconds!=null&&!s.complete?' &middot; ~'+fmtDur(s.eta_seconds)+' left':'') + '</div>';
  }
  if(d.bed){
    const b=d.bed, bo=b.overall||{};
    const bphase = b.complete ? '✓ BAM&rarr;BED complete'
      : b.stalled ? '⚠ BAM&rarr;BED stalled'
      : b.launch_pending ? 'BAM&rarr;BED queued — waiting for STAR'
      : 'BAM&rarr;BED converting';
    h += '<div class="banner '+(b.complete?'ok':(b.stalled?'err':'pick'))+'" style="margin-top:6px">'
       + bphase + (bo.exp?' &mdash; '+bo.done+' / '+bo.exp+' BED pairs ('+bo.pct+'%)':'')
       + (b.live_jobs!=null?' &middot; '+b.live_jobs+' jobs':'')
       + (b.eta_seconds!=null&&!b.complete?' &middot; ~'+fmtDur(b.eta_seconds)+' left':'') + '</div>';
  }
  if(d.psi){
    const p=d.psi;
    const pphase = p.complete ? '✓ AltAnalyze PSI complete'
      : p.stalled ? '⚠ AltAnalyze PSI stalled'
      : p.launch_pending ? 'AltAnalyze PSI queued — waiting for BAM&rarr;BED'
      : p.job_running ? 'AltAnalyze PSI running' : 'AltAnalyze PSI';
    h += '<div class="banner '+(p.complete?'ok':(p.stalled?'err':'pick'))+'" style="margin-top:6px">'
       + pphase + (p.live_jobs!=null?' &middot; '+p.live_jobs+' jobs':'') + '</div>';
  }
  if(d.concordance){
    const c=d.concordance;
    const cphase = c.complete ? '✓ Drug concordance complete'
      : c.stalled ? '⚠ Drug concordance stalled'
      : c.launch_pending ? 'Drug concordance queued — waiting for PSI'
      : c.job_running ? 'Drug concordance scoring' : 'Drug concordance';
    h += '<div class="banner '+(c.complete?'ok':(c.stalled?'err':'pick'))+'" style="margin-top:6px">'
       + cphase + (c.n_atlases!=null&&c.n_atlases>0?' &mdash; '+c.n_atlases+' atlas result(s)':'')
       + (c.live_jobs!=null?' &middot; '+c.live_jobs+' jobs':'') + '</div>';
  }
  if(ov.exp){ h+='<div class="obar" title="solid = converted, faint = downloaded" style="margin-bottom:4px;position:relative">'
    + '<span style="width:'+(ov.dl_pct||0)+'%;opacity:.30"></span>'
    + '<span style="width:'+(ov.pct||0)+'%;position:absolute;left:1px;top:0"></span></div>'
    + '<div class="hint" style="margin-bottom:12px;font-size:11.5px">solid = converted, faint = downloaded</div>'; }
  else if(d.live_jobs){ h+='<div class="hint" style="margin-bottom:8px">'+d.live_jobs+' active job(s) found'+tag
    +', but no per-study counts'+(d.root?' under <code>'+esc(d.root)+'</code>':'')+' — see the probe output below.</div>'; }
  else { h+='<div class="hint" style="margin-bottom:8px">No active jobs found for this instance'+tag+'. '
    + (d.root?'Checked <code>'+esc(d.root)+'</code>.':'Start a cluster run first.')+'</div>'; }
  const ps=(d.per_study||[]).slice().sort((a,b)=>(a.converted/Math.max(1,a.exp))-(b.converted/Math.max(1,b.exp)));
  if(ps.length){ h+='<div class="hint" style="margin-bottom:6px">Per study (downloaded · converted / total):</div>'
    + '<div class="files" style="max-height:240px;overflow:auto">'
    + ps.map(s=>'<div class="file"><span class="nm">'+esc(s.gse)+'</span><span class="sz">'
        + s.downloaded+' dl &middot; '+s.converted+' conv / '+s.exp+'</span></div>').join('')+'</div>'; }
  if(d.root){ h+='<div class="mutfoot" style="margin-top:8px">root: '+esc(d.root)+'</div>'; }
  if(d.raw){ h+='<details style="margin-top:8px"><summary class="hint" style="cursor:pointer">probe output</summary>'
    + '<pre style="white-space:pre-wrap;font-size:11px;color:#9aa6c0;max-height:220px;overflow:auto;background:#0a0e17;border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:6px">'+esc(d.raw)+'</pre></details>'; }
  h+='<div class="hint" style="margin-top:10px;font-size:11.5px">'
    + (d.complete?'Pipeline complete.'
       :(d.stalled?'Watchdog stalled.'
         :'Click "Refresh now" to update.'+(eta.sec==null?' ETA appears once conversions progress.':'')))
    + ' Last checked '+esc(new Date().toLocaleTimeString())
    + (eta.src&&eta.sec!=null&&!d.complete?' &middot; ETA from '+esc(eta.src):'') + '</div>';
  h+='<button class="ghost" id="clstatRefresh_'+panelId+'" style="margin-top:8px">Refresh now</button>';
  host.innerHTML=h; const b=$('#clstatRefresh_'+panelId); if(b) b.onclick=()=>fetchClusterStatus(panelId);
}

// prefill the form from locally-saved settings (keys + cluster info remembered between launches)
function setVal(id, v){ const el=$('#'+id); if(el && v!=null && v!=='') el.value=v; }
function applySettings(s){
  if(!s || !Object.keys(s).length) return;
  setVal('query', s.query); setVal('cap', s.cap);
  if(s.scope){ const r=document.querySelector('input[name=scope][value="'+s.scope+'"]'); if(r) r.checked=true; }
  if(s.skip_ai!=null){ skipEl.checked=!!s.skip_ai; akeyEl.disabled=skipEl.checked; akeyEl.style.opacity=skipEl.checked?.5:1; }
  if(s.provider){ providerEl.value = s.provider; }
  savedKeys = Object.assign({}, s.api_keys || {});
  modelHistory = Object.assign({}, s.model_history || {});
  baseUrls = Object.assign({}, s.base_urls || {});
  modelTokens = Object.assign({}, s.model_max_tokens || {});
  if(s.provider && s.model){ const h=histFor(s.provider); if(h.indexOf(s.model)<0) h.unshift(s.model); }  // back-compat: seed from the legacy single model
  if(s.provider && s.base_url && baseUrls[s.provider]==null) baseUrls[s.provider]=s.base_url;             // and the legacy single base_url
  syncProvider();   // restores model + base_url + key for the chosen provider from the per-provider memory
  if(s.disable_reasoning!=null && $('#noreason')) $('#noreason').checked = !!s.disable_reasoning;
  setVal('ncbi', s.ncbi_key); setVal('conc', s.concurrency); setVal('alertemail', s.alert_email);
  setVal('clmodelpath', s.diagnose_model_path); setVal('clmodeldir', s.diagnose_model_dir);
  if(s.pick_mode){ const r=document.querySelector('input[name=pick][value="'+s.pick_mode+'"]'); if(r) r.checked=true; }
  if(s.module){ const r=document.querySelector('input[name=module][value="'+s.module+'"]'); if(r) r.checked=true; }
  if(s.cluster_mode){ const r=document.querySelector('input[name=clmode][value="'+s.cluster_mode+'"]'); if(r) r.checked=true; }
  const c=s.cluster||{};
  setVal('clroot', c.PIPELINE_ROOT); setVal('clscratch', c.SCRATCH_DIR); setVal('clqueue', c.LSF_QUEUE);
  setVal('cltool', c.SRATOOLKIT_MODULE); setVal('claspera', c.ASPERA_MODULE);
  setVal('clthreads', c.THREADS); setVal('clmem', c.MEM_MB); setVal('clwall', c.WALL);
  setVal('clpfmem', c.PREFETCH_MEM_MB);   // JOB_TAG is auto per-instance (sra1, sra2, ...) — keep the instance default, don't restore a saved one
  setVal('sshhost', c.ssh_host); setVal('sshuser', c.ssh_user); setVal('sshport', c.ssh_port); setVal('sshkey', c.ssh_key);
  setVal('sshpass', s.ssh_password);
  const st=s.star||{};
  setVal('star_genome', st.GENOME_DIR); setVal('star_org', st.ORGANISM); setVal('star_gtf', st.SJDB_GTF);
  setVal('star_indexroot', st.STAR_INDEX_ROOT); setVal('star_threads', st.THREADS);
  setVal('star_mem', st.MEM_MB); setVal('star_wall', st.WALL);
  if($('#del_fastq') && st.DELETE_FASTQ_AFTER_BAM!=null) $('#del_fastq').checked = (String(st.DELETE_FASTQ_AFTER_BAM)!=='0');
  const bd=s.bed||{};
  setVal('bed_species', bd.SPECIES); setVal('bed_mem', bd.MEM_MB); setVal('bed_wall', bd.WALL);
  if($('#bed_enable') && bd.enabled!=null) $('#bed_enable').checked = (String(bd.enabled)!=='0');
  if(bd.BED_MODE){ const r=document.querySelector('input[name=bedmode][value="'+bd.BED_MODE+'"]'); if(r) r.checked=true; }
  if($('#del_bam') && bd.DELETE_BAM_AFTER_BED!=null) $('#del_bam').checked = (String(bd.DELETE_BAM_AFTER_BED)==='1');
  if(typeof syncBedMode==='function') syncBedMode();
  const ps=s.psi||{};
  setVal('psi_home', ps.ALTANALYZE_HOME); setVal('psi_db', ps.ALTANALYZE_DB); setVal('psi_local', ps.ALTANALYZE_LOCAL);
  setVal('psi_species', ps.SPECIES); setVal('psi_expname', ps.EXPNAME); setVal('psi_mem', ps.MEM_MB); setVal('psi_wall', ps.WALL);
  if($('#psi_enable') && ps.enabled!=null) $('#psi_enable').checked = (String(ps.enabled)!=='0');
  if($('#psi_goelite') && ps.RUN_GOELITE!=null) $('#psi_goelite').checked = (String(ps.RUN_GOELITE)==='1');
  if($('#psi_compress') && ps.COMPRESS_WHEN_DONE) $('#psi_compress').value = ps.COMPRESS_WHEN_DONE;
  const cc=s.concordance||{};
  setVal('conc_dpsi', cc.DPSI); setVal('conc_rawp', cc.RAWP); setVal('conc_thresh', cc.CONC_THRESHOLD);
  if($('#conc_atlas') && cc.CANCER_ATLAS) $('#conc_atlas').value = cc.CANCER_ATLAS;
  if($('#conc_enable') && cc.enabled!=null) $('#conc_enable').checked = (String(cc.enabled)!=='0');
  if($('#conc_removeir') && cc.REMOVE_IR!=null) $('#conc_removeir').checked = (String(cc.REMOVE_IR)==='1');
  const pg=(s.groups&&s.groups.groups)||[];
  if(pg.length && $('#psigroups') && typeof addGroupRow==='function'){ $('#psigroups').innerHTML='';
    pg.forEach(g=>addGroupRow(g.name||'', (g.keywords||[]).join(', '), g.control)); }
  // Pipeline range ALWAYS launches at its LARGEST (full fetch -> psi_submit). The slider is a per-run
  // override, NOT a persisted preference, so a saved narrow end (e.g. bed_submit) can never silently cap a
  // launch. Drag the rail per run if you want a partial range.
  if(typeof CHECKPOINTS!=='undefined'){ startIdx = 0; endIdx = Math.max(0, CHECKPOINTS.length - 1); }
  if(typeof renderPhaseRail==='function') renderPhaseRail();
  if(s.cluster && (s.cluster.ssh_host||'').trim()) $('#clusterCheck').hidden=false;  // enable after-restart status check
  syncScope(); syncPick(); syncModule(); syncClusterMode();
}

// resume an in-flight (or finished) run if the page is reloaded
function shutdownServer(){
  if(!confirm('Shut down the SpliceScout server? This closes the local web UI. Any cluster runs you already launched keep going on their own (they are self-driving).')) return;
  var b=document.getElementById('shutdownBtn'); if(b){ b.disabled=true; b.textContent='Shutting down...'; }
  fetch('/api/shutdown',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(function(){ setTimeout(_stopped,400); }).catch(function(){ setTimeout(_stopped,400); });
  function _stopped(){
    document.body.innerHTML='<div style="max-width:680px;margin:18vh auto;text-align:center;font:16px/1.6 system-ui,Segoe UI,Arial,sans-serif;color:#e7ecf5"><h1 style="font-size:22px">SpliceScout server stopped</h1><p style="color:#9aa6c0">You can close this tab. Cluster runs you launched keep going on their own. Relaunch SpliceScout to use the UI again.</p></div>';
  }
}
(function(){var b=document.getElementById('shutdownBtn'); if(b) b.addEventListener('click', shutdownServer);})();

(async function init(){
  $('#tabRun').onclick=()=>showTab('run'); $('#tabPlots').onclick=()=>showTab('plots');
  $('#tabAssistant').onclick=()=>showTab('chat');
  $('#chatsend').onclick=sendChat;
  $('#chatbox').addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); sendChat(); } });
  renderChat();
  $('#clCheckBtn').onclick=()=>fetchClusterStatus('clusterstatus2');
  $('#docClose').onclick=closeDoc;
  $('#docmodal').onclick=(e)=>{ if(e.target.id==='docmodal') closeDoc(); };
  document.addEventListener('keydown',e=>{ if(e.key==='Escape') closeDoc(); });
  try{ applySettings(await (await fetch('/api/settings')).json()); }catch(ex){}
  try{
    const s = await (await fetch('/api/status')).json();
    if(s.state && s.state!=='idle'){
      $('#setup').hidden=true; $('#run').hidden=false;
      render(s);
      if(s.state==='running') startPolling();
    }
  }catch(ex){}
})();
</script>
<script>
/* DNA background: floating helix glyphs that drift + spin and react to the mouse (repel, brighten, spin). */
(function(){
  var cv=document.getElementById('dnafx'); if(!cv||!cv.getContext) return;
  var ctx=cv.getContext('2d'), W=0,H=0, DPR=Math.min(window.devicePixelRatio||1,2);
  var DNA=String.fromCodePoint(0x1F9EC);  /* the dna emoji U+1F9EC, built from its code point (pure ASCII) */
  var parts=[], mouse={x:-1e4,y:-1e4}, raf=0;
  function rnd(a,b){return a+Math.random()*(b-a);}
  function mk(){return {x:rnd(0,Math.max(W,1)),y:rnd(0,Math.max(H,1)),
    vx:rnd(-0.25,0.25),vy:rnd(-0.25,0.25),sz:rnd(16,40),
    rot:rnd(0,6.283),vr:rnd(-0.01,0.01),op:rnd(0.04,0.12)};}
  function resize(){
    W=window.innerWidth; H=window.innerHeight;
    cv.width=Math.floor(W*DPR); cv.height=Math.floor(H*DPR);
    cv.style.width=W+'px'; cv.style.height=H+'px';
    ctx.setTransform(DPR,0,0,DPR,0,0);
    var n=Math.max(14,Math.min(64,Math.round(W*H/24000)));  /* density scales with screen area */
    while(parts.length<n) parts.push(mk());
    if(parts.length>n) parts.length=n;
  }
  function frame(){
    ctx.clearRect(0,0,W,H);
    var R=180, R2=R*R, mx=1.9;
    for(var i=0;i<parts.length;i++){
      var p=parts[i], dx=p.x-mouse.x, dy=p.y-mouse.y, d2=dx*dx+dy*dy, near=d2<R2;
      if(near){ var d=Math.sqrt(d2)||1, f=(1-d/R)*0.9;
        p.vx+=(dx/d)*f; p.vy+=(dy/d)*f; p.vr+=(dx>0?1:-1)*0.004*f; }    /* repel + extra spin near cursor */
      p.vx+=rnd(-0.012,0.012); p.vy+=rnd(-0.012,0.012);                 /* ambient drift */
      p.vx*=0.95; p.vy*=0.95;                                           /* friction -> settle back to a float */
      p.vx=Math.max(-mx,Math.min(mx,p.vx)); p.vy=Math.max(-mx,Math.min(mx,p.vy));
      p.x+=p.vx; p.y+=p.vy; p.rot+=p.vr; p.vr*=0.99;
      var m=p.sz;
      if(p.x<-m)p.x=W+m; else if(p.x>W+m)p.x=-m;
      if(p.y<-m)p.y=H+m; else if(p.y>H+m)p.y=-m;
      var op=p.op; if(near) op=Math.min(0.30,p.op+(1-Math.sqrt(d2)/R)*0.26);  /* brighten slightly near cursor */
      ctx.save(); ctx.translate(p.x,p.y); ctx.rotate(p.rot); ctx.globalAlpha=op;
      ctx.filter='grayscale(1) brightness(1.7)';   /* matte: desaturate the emoji to a faint monochrome texture */
      ctx.font=p.sz+'px "Segoe UI Emoji","Apple Color Emoji","Noto Color Emoji",system-ui,sans-serif';
      ctx.textAlign='center'; ctx.textBaseline='middle';
      ctx.fillText(DNA,0,0); ctx.restore();
    }
    ctx.globalAlpha=1; raf=requestAnimationFrame(frame);
  }
  window.addEventListener('resize',resize,{passive:true});
  window.addEventListener('mousemove',function(e){mouse.x=e.clientX;mouse.y=e.clientY;},{passive:true});
  window.addEventListener('mouseout',function(){mouse.x=-1e4;mouse.y=-1e4;},{passive:true});
  document.addEventListener('visibilitychange',function(){           /* don't burn CPU on a hidden tab */
    if(document.hidden){ if(raf) cancelAnimationFrame(raf); raf=0; }
    else if(!raf){ raf=requestAnimationFrame(frame); }
  });
  resize(); frame();
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
