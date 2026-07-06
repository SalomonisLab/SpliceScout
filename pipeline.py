"""
NCBI GEO RNA-seq pipeline — submit a query, get cleaned splicing-amenable tables.

End to end: fetch GEO -> extract structured SRA metadata + library protocol -> prep AI batches
-> AI clean (compounds + samples via the Anthropic API) -> merge -> build all tables.

Interactive:   python pipeline.py
Unattended:    python pipeline.py --query "..." --cap 25 --yes
Resume a run:  python pipeline.py --run-dir runs/<existing> --resume
Skip AI (test):python pipeline.py --cap 25 --skip-ai --yes
"""
import argparse
import getpass
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime

from pipeline_paths import Paths
from progress import NULL
import progress
import llm_providers
import fetch_5000_ncbi
import structured_extract
import prep_ai
import merge_ai
import build_final

DEFAULT_QUERY = "rna-seq[Description] AND human[Organism] AND drug"
STAGES = ["fetch", "extract", "prep", "ai_compounds", "ai_samples", "merge", "build"]
_AI_RATE_RETRY_SECS = 30   # on an AI rate-limit (429), wait this long and re-submit, instead of pausing
_AI_PREFLIGHT_RATE_MAX = 10  # ...but cap preflight rate-retries so a permanently-throttled key can't hang forever

# Canonical 18-stage order (the phase-range slider works over progress.STAGES, not the 7-stage list above).
_STAGE_ORDER = [k for k, _ in progress.STAGES]


def _idx(stage):
    try:
        return _STAGE_ORDER.index(stage)
    except ValueError:
        return -1


def _resolve_inject_dest(dest, P):
    """Map a progress.CHECKPOINTS input `dest` to an on-disk path inside the run dir."""
    if dest == "runtable_filtered_csv":          # needs the cell-line slug from cellline_selection.json
        import cluster_deploy
        try:
            sel = json.load(open(P.cellline_selection, encoding="utf-8"))
        except Exception:
            sel = {}
        name = sel.get("canonical") or sel.get("cell_line") or sel.get("name") or "line"
        return P.runtable_filtered_csv(cluster_deploy._slug(name))
    return getattr(P, dest)


def _inject_start_artifacts(cfg, P, state, start_i):
    """Mid-pipeline START: copy each user-supplied artifact into the run dir at its Paths location,
    then pre-mark every stage BEFORE start_stage 'done' so begin() skips it. Raises on a missing
    REQUIRED input. Idempotent -> safe on resume. cellline_selection is listed first in CHECKPOINTS
    so dests that need its slug (runtable_filtered_csv) resolve after it is staged."""
    import shutil
    start = getattr(cfg, "start_stage", "fetch") or "fetch"
    supplied = getattr(cfg, "supplied_inputs", None) or {}
    cp = next((c for c in progress.CHECKPOINTS if c["stage"] == start), None)
    if cp:
        for spec in cp.get("inputs", []):
            dest = _resolve_inject_dest(spec["dest"], P)
            src = str(supplied.get(spec["field"]) or "").strip()
            if not src:
                # nothing supplied -> fine if already in the run dir (resume) or the input is optional
                if os.path.exists(dest) or spec.get("optional"):
                    continue
                raise RuntimeError("start stage %r needs input %r (%s) but none was supplied"
                                   % (start, spec["field"], spec.get("label", spec["field"])))
            src = os.path.abspath(os.path.expanduser(src))
            if not os.path.exists(src):
                raise RuntimeError("supplied input %r not found: %s" % (spec["field"], src))
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if spec.get("kind") == "dir":
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copyfile(src, dest)
            print(f"[start] supplied {spec['field']} -> {dest}")
    for k in _STAGE_ORDER[:start_i]:
        state[k] = "done"
    _save_state(P, state)


@dataclass
class RunConfig:
    query: str
    cap: object               # int or "unlimited"
    ncbi_key: object          # str or None
    model: str
    concurrency: int
    run_dir: str
    skip_ai: bool = False
    provider: str = "anthropic"   # AI provider for cleaning: anthropic | openai | gemini
    base_url: str = ""         # custom OpenAI-compatible endpoint (MiMo/Qwen/local/OpenRouter); blank = default
    disable_reasoning: bool = False  # OpenAI-compatible "thinking off" body — for reasoning models (e.g. MiMo)
    max_tokens: int = 60000    # max OUTPUT tokens per AI cleaning call (per-model, set in the UI); 60000 = default
    module: str = "bulk_rna_seq"  # analysis module: drives the library-prep filter + the analysis pipeline (STAR)
    deep_dive: bool = True     # after build, deep-dive the best cell line into a Run Selector table
    pick_mode: str = "auto"    # "auto" (top real line) or "manual" (UI/CLI picks from the ranked list)
    cluster_mode: str = "off"  # "off" | "manual" (build bundle) | "autonomous" (build + upload/launch)
    cluster_cfg: dict = None   # config.sh values + ssh host/port/user/key (NO password — that's a secret)
    star_cfg: dict = None      # STAR config.sh values (genome index, organism, build resources) for bulk_rna_seq
    bed_cfg: dict = None       # BAM->BED config.sh values (AltAnalyze species/resources; 'enabled' toggle) for bulk_rna_seq
    psi_cfg: dict = None       # AltAnalyze (PSI) config.sh values + 'enabled' toggle + ALTANALYZE_LOCAL for bulk_rna_seq
    concordance_cfg: dict = None  # splicing-concordance config.sh values + 'enabled' toggle + cancer_atlas for bulk_rna_seq
    group_cfg: dict = None     # user-defined comparison groups (Phase B); None => default treated-vs-control
    start_stage: str = "fetch"        # phase range: first stage to run (a progress.STAGES key)
    end_stage: str = "concordance_submit"  # phase range: last stage to run; stages outside [start,end] are skipped
    supplied_inputs: dict = None      # {input-field: local path} to stage in for a mid-pipeline START (progress.CHECKPOINTS)


def _ask(prompt, default=None, secret=False):
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def _load_state(P):
    if os.path.exists(P.state):
        try:
            return json.load(open(P.state, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(P, state):
    # atomic: a kill mid-write must not truncate pipeline_state.json (that would lose all resume progress)
    tmp = P.state + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, P.state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query")
    ap.add_argument("--cap", help="integer or 'unlimited'")
    ap.add_argument("--ncbi-key", default=None)
    ap.add_argument("--provider", choices=list(llm_providers.PROVIDERS), default="anthropic",
                    help="AI provider for cleaning: anthropic | openai | gemini")
    ap.add_argument("--anthropic-key", default=None)
    ap.add_argument("--openai-key", default=None)
    ap.add_argument("--gemini-key", default=None)
    ap.add_argument("--model", default=None, help="provider-specific model (else the provider default)")
    ap.add_argument("--openai-base-url", default=None,
                    help="custom OpenAI-compatible endpoint (a MiMo/Qwen host, local vLLM/LM-Studio, or "
                         "OpenRouter https://openrouter.ai/api/v1); used with --provider openai")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--disable-reasoning", action="store_true",
                    help="turn OFF chain-of-thought for OpenAI-compatible reasoning models (e.g. MiMo) "
                         "whose thinking otherwise exhausts the token budget and truncates batches")
    ap.add_argument("--run-dir", default=None)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--skip-ai", action="store_true", help="run deterministic stages only")
    ap.add_argument("--no-deep-dive", action="store_true", help="skip the cell-line metadata deep dive")
    ap.add_argument("--start-stage", default="fetch",
                    help="phase range: first stage to run (a progress.STAGES key); earlier stages' outputs must already exist in the run dir")
    ap.add_argument("--end-stage", default="bed_submit",
                    help="phase range: last stage to run; later stages are skipped")
    ap.add_argument("--module", default=None, help="analysis module (e.g. bulk_rna_seq) — drives filter + alignment")
    ap.add_argument("--star-genome-dir", default=None, help="prebuilt STAR genome index dir (bulk_rna_seq module)")
    ap.add_argument("--star-gtf", default=None, help="GTF for STAR splice junctions (optional)")
    ap.add_argument("--star-index-root", default=None, help="where a build-once STAR index is written")
    ap.add_argument("--star-organism", default=None, help="organism override (default: auto-detect from the runs)")
    ap.add_argument("--pick", choices=["auto", "manual"], default="auto",
                    help="deep-dive cell-line pick: auto (top real line) or manual (prompt)")
    ap.add_argument("--cell-line", default=None, help="deep-dive a specific cell line by name")
    ap.add_argument("--validate-runtable", action="store_true",
                    help="prove the Run Selector reconstruction matches the official export, then exit")
    ap.add_argument("--cluster-mode", choices=["off", "manual", "autonomous"], default="off",
                    help="cluster handoff: off | manual (build bundle) | autonomous (build + upload/launch)")
    ap.add_argument("--cluster-root", default=None, help="PIPELINE_ROOT path on the cluster")
    ap.add_argument("--ssh-host", default=None)
    ap.add_argument("--ssh-user", default=None)
    ap.add_argument("--ssh-port", default="22")
    ap.add_argument("--ssh-key", default=None, help="private key file (else SSH agent / default key)")
    ap.add_argument("--cluster-retry", action="store_true",
                    help="re-run ONLY the cluster upload for an existing --run-dir (asks for fixed info)")
    ap.add_argument("--yes", action="store_true", help="don't prompt for confirmations")
    a = ap.parse_args()

    if a.validate_runtable:
        import runtable_build
        runtable_build.validate()
        return

    if a.cluster_retry:
        _cluster_retry_only(a)
        return

    # ----- resume an existing run -----
    if a.run_dir and a.resume:
        P = Paths(a.run_dir).ensure_dirs()
        cfg = RunConfig(**json.load(open(P.config, encoding="utf-8")))
        if a.skip_ai:
            cfg.skip_ai = True
        if a.provider != "anthropic":
            cfg.provider = llm_providers.normalize_provider(a.provider)
        if a.model:
            cfg.model = a.model
        if a.openai_base_url is not None:
            cfg.base_url = a.openai_base_url
        if a.disable_reasoning:
            cfg.disable_reasoning = True
        if a.module:
            cfg.module = a.module
        sc = _star_cfg_from_args(a)
        if sc:
            cfg.star_cfg = {**(cfg.star_cfg or {}), **sc}
        if a.no_deep_dive:
            cfg.deep_dive = False
        if a.pick != "auto" or a.cell_line:
            cfg.pick_mode = "manual"
        if a.cluster_mode != "off":
            cfg.cluster_mode = a.cluster_mode
        cc = _cluster_cfg_from_args(a)
        if cc:
            cfg.cluster_cfg = {**(cfg.cluster_cfg or {}), **cc}
        print(f"Resuming run {P.run_dir}")
    else:
        # ----- gather config (args, then interactive prompts) -----
        query = a.query or _ask("Search query (NCBI GEO)", DEFAULT_QUERY)
        cap_raw = a.cap or _ask("Study cap (number or 'unlimited')", "unlimited")
        cap = "unlimited" if str(cap_raw).lower().startswith("u") else int(cap_raw)
        ncbi_key = a.ncbi_key or _ask("NCBI E-utilities API key (blank = keyless)", "") or None
        provider = llm_providers.normalize_provider(a.provider)
        model = a.model or _ask(f"{provider} model", llm_providers.DEFAULT_MODEL[provider])
        base_url = a.openai_base_url
        if base_url is None and provider in ("openai", "gemini"):
            base_url = _ask("Custom OpenAI-compatible base URL (blank = provider default)", "") or None
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40].strip("-") or "run"
        run_dir = a.run_dir or os.path.join("runs", f"{slug}_{datetime.now():%Y%m%d-%H%M%S}")
        P = Paths(run_dir).ensure_dirs()
        pick_mode = "manual" if (a.pick == "manual" or a.cell_line) else "auto"
        cfg = RunConfig(query=query, cap=cap, ncbi_key=ncbi_key, model=model, provider=provider,
                        base_url=base_url or "", disable_reasoning=a.disable_reasoning,
                        module=(a.module or "bulk_rna_seq"),
                        concurrency=a.concurrency, run_dir=P.run_dir,
                        skip_ai=a.skip_ai, deep_dive=not a.no_deep_dive, pick_mode=pick_mode,
                        cluster_mode=a.cluster_mode, cluster_cfg=_cluster_cfg_from_args(a),
                        star_cfg=_star_cfg_from_args(a),
                        start_stage=(a.start_stage or "fetch"), end_stage=(a.end_stage or "bed_submit"))
        json.dump(asdict(cfg), open(P.config, "w", encoding="utf-8"), indent=2)

    # provider API key into env (CLI: prompt if missing)
    for flag, env in (("anthropic_key", "ANTHROPIC_API_KEY"), ("openai_key", "OPENAI_API_KEY"),
                      ("gemini_key", "GEMINI_API_KEY")):
        if getattr(a, flag):
            os.environ[env] = getattr(a, flag)
    if not cfg.skip_ai and not llm_providers.have_key(cfg.provider):
        envname = llm_providers.KEY_ENV[cfg.provider]
        k = _ask(f"{envname} (required for AI cleaning with {cfg.provider}; blank to skip AI)", "")
        if k:
            os.environ[envname] = k
        else:
            cfg.skip_ai = True
            print(f"** No {cfg.provider} key -> running deterministic stages only (--skip-ai). **")

    select_fn = None
    if cfg.deep_dive and cfg.pick_mode == "manual":
        if a.cell_line:
            select_fn = lambda ranked, _n=a.cell_line: _n
        else:
            select_fn = _console_pick

    secrets = {}
    if os.environ.get("CLUSTER_SSH_PASSWORD"):
        secrets["ssh_password"] = os.environ["CLUSTER_SSH_PASSWORD"]

    run_pipeline(cfg, P, select_fn=select_fn, secrets=secrets,
                 cluster_fix_fn=_console_cluster_fix, ai_fix_fn=_console_ai_fix)


def _cluster_cfg_from_args(a):
    """Collect cluster/ssh settings from CLI args into a cluster_cfg dict (or None)."""
    c = {}
    if a.cluster_root:
        c["PIPELINE_ROOT"] = a.cluster_root
    if a.ssh_host:
        c["ssh_host"] = a.ssh_host
    if a.ssh_user:
        c["ssh_user"] = a.ssh_user
    if a.ssh_port and str(a.ssh_port) != "22":
        c["ssh_port"] = a.ssh_port
    if a.ssh_key:
        c["ssh_key"] = a.ssh_key
    return c or None


def _star_cfg_from_args(a):
    """Collect STAR settings from CLI args into a star_cfg dict (or None)."""
    c = {}
    if getattr(a, "star_genome_dir", None):
        c["GENOME_DIR"] = a.star_genome_dir
    if getattr(a, "star_gtf", None):
        c["SJDB_GTF"] = a.star_gtf
    if getattr(a, "star_index_root", None):
        c["STAR_INDEX_ROOT"] = a.star_index_root
    if getattr(a, "star_organism", None):
        c["ORGANISM"] = a.star_organism
    return c or None


def _console_pick(ranked):
    """CLI manual pick: print the ranked real cell lines and read a choice (TTY only)."""
    if not sys.stdin.isatty() or not ranked:
        return None
    print("\nReal cell lines (ranked by # unique compounds, then total reads):")
    for i, r in enumerate(ranked[:20], 1):
        print(f"  {i:>2}. {r['canonical']:<18} {r['n_compounds']} compounds | "
              f"{r['total_spots']:,} reads | {r['n_studies']} studies")
    try:
        ans = input("Pick a number or name (Enter = #1): ").strip()
    except EOFError:
        return None
    if not ans:
        return ranked[0]["canonical"]
    if ans.isdigit() and 1 <= int(ans) <= len(ranked):
        return ranked[int(ans) - 1]["canonical"]
    for r in ranked:
        if r["canonical"].lower() == ans.lower():
            return r["canonical"]
    return ranked[0]["canonical"]


def _public_cluster_cfg(cluster_cfg):
    """Non-secret cluster settings to echo back when asking for corrections."""
    c = dict(cluster_cfg or {})
    c.pop("ssh_password", None)   # never stored here, but be safe
    return c


def _console_cluster_fix(diagnosis, current):
    """CLI: explain the upload failure and read corrected cluster/SSH values (TTY only)."""
    if not sys.stdin.isatty():
        return None
    diagnosis = diagnosis or {}
    print("\n  -- Cluster upload failed --")
    print(f"  {diagnosis.get('title','')}: {diagnosis.get('detail','')}")
    if diagnosis.get("suspect_fields"):
        print("  Likely wrong: " + ", ".join(diagnosis["suspect_fields"]))
    print("  Enter corrected values (blank = keep current).")
    fields = [("ssh_host", "SSH host"), ("ssh_user", "SSH username"), ("ssh_port", "SSH port"),
              ("ssh_key", "private key file"), ("PIPELINE_ROOT", "PIPELINE_ROOT (cluster path)")]
    out = {}
    for key, label in fields:
        cur = current.get(key, "")
        try:
            ans = input(f"    {label} [{cur}]: ").strip()
        except EOFError:
            return None
        if ans:
            out[key] = ans
    fix = {"cluster": out}
    try:
        pw = getpass.getpass("    SSH password (blank = use key/agent): ")
    except Exception:
        pw = ""
    fix["ssh_password"] = pw   # explicit: empty clears any prior password (use key/agent)
    try:
        go = input("    Retry upload now? [Y/n]: ").strip().lower()
    except EOFError:
        return None
    if go in ("n", "no"):
        return {"action": "cancel"}
    return fix


def _console_ai_fix(diagnosis, current):
    """CLI: explain why AI cleaning can't start and read a corrected provider/model/key (or turn AI
    off). Returns {provider?, model?, api_key?} | {"action": "skip_ai"} | None (TTY only)."""
    if not sys.stdin.isatty():
        return None   # non-interactive -> caller turns AI off
    diagnosis = diagnosis or {}
    current = current or {}
    print("\n  -- AI cleaning can't start --")
    print(f"  {diagnosis.get('title','')}: {diagnosis.get('detail','')}")
    print(f"  Current: provider={current.get('provider','')} model={current.get('model','')}")
    print("  Enter corrections (blank = keep current), or type 'off' to run without AI.")
    try:
        prov = input("    provider (anthropic|openai|gemini, or 'off'): ").strip()
    except EOFError:
        return None
    if prov.lower() in ("off", "skip", "none", "no"):
        return {"action": "skip_ai"}
    out = {}
    if prov:
        out["provider"] = prov
    try:
        model = input(f"    model [{current.get('model','')}]: ").strip()
        if model:
            out["model"] = model
        key = getpass.getpass("    API key (blank = keep current): ")
        if key:
            out["api_key"] = key
    except Exception:
        pass
    return out


def _submit_with_retry(P, cfg, deep, secrets, reporter, cluster_fix_fn=None, max_attempts=6):
    """Autonomous submit with a fix-and-retry loop: on failure, understand the log, ask for
    corrected SSH/cluster info, rebuild the (small) bundle so config.sh matches, and re-submit —
    WITHOUT re-running the rest of the pipeline. Always non-fatal (bundle stays downloadable)."""
    import cluster_deploy
    res = cluster_deploy.submit_over_ssh(P, cfg.cluster_cfg, secrets, reporter=reporter)
    attempts = 1
    while not res.get("submitted"):
        diag = res.get("diagnosis") or cluster_deploy.diagnose_failure(res.get("reason", ""))
        if attempts >= max_attempts:
            print(f"  CLUSTER SUBMIT: gave up after {attempts} attempts; bundle remains downloadable.")
            break
        asker = cluster_fix_fn or reporter.await_cluster_fix
        reporter.set_detail("waiting for corrected SSH/cluster info…")
        fix = asker(diag, _public_cluster_cfg(cfg.cluster_cfg))
        if not fix or fix.get("action") == "cancel":
            print("  CLUSTER SUBMIT: skipped; the bundle is still downloadable "
                  "(follow RUN_ON_CLUSTER.txt to run it manually).")
            break
        updated = {k: v for k, v in (fix.get("cluster") or {}).items() if str(v).strip() != ""}
        cfg.cluster_cfg = {**(cfg.cluster_cfg or {}), **updated}
        if "ssh_password" in fix:                 # allow set OR explicit clear (switch to key/agent)
            if fix["ssh_password"]:
                secrets["ssh_password"] = fix["ssh_password"]
            else:
                secrets.pop("ssh_password", None)
        try:                                      # persist corrected (non-secret) cfg for any --resume
            json.dump(asdict(cfg), open(P.config, "w", encoding="utf-8"), indent=2)
        except Exception:
            pass
        print(f"  CLUSTER SUBMIT: retrying with corrected settings (attempt {attempts + 1})…")
        cluster_deploy.build_bundle(P, deep, cfg.cluster_cfg, reporter=reporter)  # config.sh = new root
        res = cluster_deploy.submit_over_ssh(P, cfg.cluster_cfg, secrets, reporter=reporter)
        attempts += 1
    return res


def _cluster_retry_only(a):
    """Standalone: rebuild the bundle for an existing run and (re)submit it, asking for corrected
    cluster/SSH info on failure — so a bad upload never means re-running the whole pipeline."""
    if not a.run_dir:
        print("--cluster-retry needs --run-dir <existing run>")
        return
    P = Paths(a.run_dir).ensure_dirs()
    if not os.path.exists(P.config):
        print(f"no config.json in {a.run_dir}")
        return
    cfg = RunConfig(**json.load(open(P.config, encoding="utf-8")))
    cc = _cluster_cfg_from_args(a)
    if cc:
        cfg.cluster_cfg = {**(cfg.cluster_cfg or {}), **cc}
    if cfg.cluster_mode == "off":
        cfg.cluster_mode = "autonomous"
    if not os.path.exists(P.cellline_selection):
        print("no deep-dive selection (cellline_selection.json) in this run — nothing to upload.")
        return
    deep = json.load(open(P.cellline_selection, encoding="utf-8"))
    secrets = {}
    if os.environ.get("CLUSTER_SSH_PASSWORD"):
        secrets["ssh_password"] = os.environ["CLUSTER_SSH_PASSWORD"]
    import cluster_deploy
    print(f"=== CLUSTER RETRY for {P.run_dir} ===")
    cluster_deploy.build_bundle(P, deep, cfg.cluster_cfg)
    res = _submit_with_retry(P, cfg, deep, secrets, NULL, _console_cluster_fix)
    print("  submitted." if res.get("submitted") else "  not submitted (bundle is downloadable).")


def _list_outputs(P):
    """Enumerate the tables/ + runtable/ outputs for the UI (name, dir, size, flags)."""
    out = []

    def add(dirpath, dirkey, headline_name=None, featured_name=None):
        if os.path.isdir(dirpath):
            for name in sorted(os.listdir(dirpath)):
                fp = os.path.join(dirpath, name)
                if os.path.isfile(fp):
                    out.append({"name": name, "size": os.path.getsize(fp), "dir": dirkey,
                                "headline": name == headline_name,
                                "featured": name == featured_name})
    add(P.tables_dir, "tables", headline_name="ncbi_final_splicing.csv")
    add(P.runtable_dir, "runtable", featured_name="SraAccList.txt")
    for o in out:                       # the cluster bundle is also a featured deliverable
        if o["name"] == "cluster_bundle.zip":
            o["featured"] = True
    return out


def _deep_summary(P):
    """Compact deep-dive summary for the UI (robust to resume — read from files)."""
    d = {}
    if os.path.exists(P.cellline_selection):
        try:
            sel = json.load(open(P.cellline_selection, encoding="utf-8"))
            d.update({"canonical": sel.get("canonical"), "n_studies": len(sel.get("studies", [])),
                      "n_compounds": len(sel.get("compounds", [])), "total_spots": sel.get("total_spots", 0)})
        except Exception:
            pass
    if os.path.exists(P.cellline_match):
        try:
            m = json.load(open(P.cellline_match, encoding="utf-8"))
            d["match_mode"] = m.get("mode")
            d["matched_values"] = sorted(v for v, i in m.get("matches", {}).items() if i.get("matches"))
        except Exception:
            pass
    if os.path.exists(P.sra_acc_list):
        try:
            d["n_accessions"] = sum(1 for line in open(P.sra_acc_list, encoding="utf-8") if line.strip())
        except Exception:
            pass
    return d or None


def _save_cluster_status(P, mode, submit_res):
    """Record what the cluster step ACTUALLY did, so the UI/CLI report the real outcome (not both
    possibilities) and it survives resume / page reload. `submit_res` is None when no upload was
    attempted this run (manual mode, or a resume that didn't re-run the submit)."""
    # resume that didn't re-attempt: keep any prior (e.g. successful) status untouched
    if submit_res is None and mode == "autonomous" and os.path.exists(P.cluster_status):
        return
    status = {"mode": mode, "attempted": mode == "autonomous" and submit_res is not None,
              "submitted": bool(submit_res and submit_res.get("submitted"))}
    if submit_res:
        if submit_res.get("host"):
            status["host"] = submit_res.get("host")
        if not submit_res.get("submitted") and submit_res.get("reason"):
            status["reason"] = submit_res.get("reason")
    try:
        json.dump(status, open(P.cluster_status, "w", encoding="utf-8"))
    except Exception:
        pass


def _cluster_summary(P):
    """Compact cluster-handoff summary for the UI (read from files; robust to resume)."""
    if not os.path.exists(P.cluster_bundle_zip):
        return None
    out = {"bundle": True, "zip": os.path.basename(P.cluster_bundle_zip)}
    cfgsh = os.path.join(P.cluster_dir, "config.sh")
    if os.path.exists(cfgsh):
        m = re.search(r'(?m)^PIPELINE_ROOT="?(.*?)"?\s*$', open(cfgsh, encoding="utf-8").read())
        if m:
            out["pipeline_root"] = m.group(1)
    bydir = os.path.join(P.cluster_dir, "by_study")
    if os.path.isdir(bydir):
        out["n_studies"] = sum(1 for g in os.listdir(bydir) if os.path.isdir(os.path.join(bydir, g)))
    if os.path.exists(P.cluster_status):                  # what the upload actually did
        try:
            st = json.load(open(P.cluster_status, encoding="utf-8"))
            out["mode"] = st.get("mode")
            out["submitted"] = bool(st.get("submitted"))
            out["attempted"] = bool(st.get("attempted"))
            if st.get("host"):
                out["host"] = st.get("host")
        except Exception:
            pass
    return out


def _save_star_status(P, cfg, built, submit_res):
    """Record what the STAR stage actually did (survives resume / page reload)."""
    if built is None and submit_res is None and os.path.exists(P.star_status):
        return  # resume that didn't re-run STAR -> keep prior status
    status = {"mode": cfg.cluster_mode, "built": bool(built),
              "attempted": cfg.cluster_mode == "autonomous" and submit_res is not None,
              "submitted": bool(submit_res and submit_res.get("submitted"))}
    if built:
        status.update({k: built.get(k) for k in ("organism", "bam_out", "job_tag") if built.get(k)})
    if submit_res and not submit_res.get("submitted") and submit_res.get("reason"):
        status["reason"] = submit_res.get("reason")
    try:
        json.dump(status, open(P.star_status, "w", encoding="utf-8"))
    except Exception:
        pass


def _star_summary(P):
    """Compact STAR-handoff summary for the UI (read from files; robust to resume)."""
    if not os.path.exists(P.star_bundle_zip) and not os.path.exists(P.star_status):
        return None
    out = {}
    if os.path.exists(P.star_bundle_zip):
        out["bundle"] = True
        out["zip"] = os.path.basename(P.star_bundle_zip)
    if os.path.exists(P.star_status):
        try:
            st = json.load(open(P.star_status, encoding="utf-8"))
            for k in ("mode", "built", "attempted", "submitted", "organism", "bam_out", "job_tag", "reason"):
                if k in st:
                    out[k] = st.get(k)
        except Exception:
            pass
    return out or None


def _save_bed_status(P, cfg, built, submit_res):
    """Record what the BAM->BED stage actually did (survives resume / page reload)."""
    if built is None and submit_res is None and os.path.exists(P.bed_status):
        return  # resume that didn't re-run BED -> keep prior status
    status = {"mode": cfg.cluster_mode, "built": bool(built),
              "attempted": cfg.cluster_mode == "autonomous" and submit_res is not None,
              "submitted": bool(submit_res and submit_res.get("submitted"))}
    if built:
        status.update({k: built.get(k) for k in ("species", "organism", "bam_input", "job_tag", "bed_mode") if built.get(k)})
    if submit_res and not submit_res.get("submitted") and submit_res.get("reason"):
        status["reason"] = submit_res.get("reason")
    try:
        json.dump(status, open(P.bed_status, "w", encoding="utf-8"))
    except Exception:
        pass


def _bed_summary(P):
    """Compact BED-handoff summary for the UI (read from files; robust to resume)."""
    if not os.path.exists(P.bed_bundle_zip) and not os.path.exists(P.bed_status):
        return None
    out = {}
    if os.path.exists(P.bed_bundle_zip):
        out["bundle"] = True
        out["zip"] = os.path.basename(P.bed_bundle_zip)
    if os.path.exists(P.bed_status):
        try:
            st = json.load(open(P.bed_status, encoding="utf-8"))
            for k in ("mode", "built", "attempted", "submitted", "species", "organism", "bam_input", "job_tag", "bed_mode", "reason"):
                if k in st:
                    out[k] = st.get(k)
        except Exception:
            pass
    return out or None


def _save_psi_status(P, cfg, built, submit_res):
    """Record what the AltAnalyze (PSI) stage actually did (survives resume / page reload)."""
    if built is None and submit_res is None and os.path.exists(P.psi_status):
        return  # resume that didn't re-run PSI -> keep prior status
    status = {"mode": cfg.cluster_mode, "built": bool(built),
              "attempted": cfg.cluster_mode == "autonomous" and submit_res is not None,
              "submitted": bool(submit_res and submit_res.get("submitted"))}
    if built:
        status.update({k: built.get(k) for k in
                       ("species", "organism", "bed_input", "job_tag", "psi_root", "grouped", "altanalyze_home")
                       if built.get(k) is not None})
    if submit_res:
        for k in ("altanalyze_home", "altanalyze_found"):
            if submit_res.get(k) is not None:
                status[k] = submit_res.get(k)
        if not submit_res.get("submitted") and submit_res.get("reason"):
            status["reason"] = submit_res.get("reason")
    try:
        json.dump(status, open(P.psi_status, "w", encoding="utf-8"))
    except Exception:
        pass


def _psi_summary(P):
    """Compact PSI-handoff summary for the UI (read from files; robust to resume)."""
    if not os.path.exists(P.psi_bundle_zip) and not os.path.exists(P.psi_status):
        return None
    out = {}
    if os.path.exists(P.psi_bundle_zip):
        out["bundle"] = True
        out["zip"] = os.path.basename(P.psi_bundle_zip)
    if os.path.exists(P.psi_status):
        try:
            st = json.load(open(P.psi_status, encoding="utf-8"))
            for k in ("mode", "built", "attempted", "submitted", "species", "organism", "bed_input",
                      "job_tag", "psi_root", "grouped", "altanalyze_home", "altanalyze_found", "reason"):
                if k in st:
                    out[k] = st.get(k)
        except Exception:
            pass
    return out or None


def _save_concordance_status(P, cfg, built, submit_res):
    """Record what the splicing-concordance stage actually did (survives resume / page reload)."""
    if built is None and submit_res is None and os.path.exists(P.concordance_status):
        return  # resume that didn't re-run concordance -> keep prior status
    status = {"mode": cfg.cluster_mode, "built": bool(built),
              "attempted": cfg.cluster_mode == "autonomous" and submit_res is not None,
              "submitted": bool(submit_res and submit_res.get("submitted"))}
    if built:
        status.update({k: built.get(k) for k in
                       ("species", "organism", "job_tag", "psi_root", "concord_root", "atlas", "n_queries")
                       if built.get(k) is not None})
    if submit_res:
        if submit_res.get("altanalyze_home") is not None:
            status["altanalyze_home"] = submit_res.get("altanalyze_home")
        if not submit_res.get("submitted") and submit_res.get("reason"):
            status["reason"] = submit_res.get("reason")
    try:
        json.dump(status, open(P.concordance_status, "w", encoding="utf-8"))
    except Exception:
        pass


def _concordance_summary(P):
    """Compact concordance-handoff summary for the UI (read from files; robust to resume)."""
    if not os.path.exists(P.concordance_bundle_zip) and not os.path.exists(P.concordance_status):
        return None
    out = {}
    if os.path.exists(P.concordance_bundle_zip):
        out["bundle"] = True
        out["zip"] = os.path.basename(P.concordance_bundle_zip)
    if os.path.exists(P.concordance_status):
        try:
            st = json.load(open(P.concordance_status, encoding="utf-8"))
            for k in ("mode", "built", "attempted", "submitted", "species", "organism",
                      "job_tag", "psi_root", "concord_root", "atlas", "n_queries", "altanalyze_home", "reason"):
                if k in st:
                    out[k] = st.get(k)
        except Exception:
            pass
    return out or None


def _psi_group_inputs(P, cfg, sel, reporter=NULL):
    """Phase B hook: if the user defined comparison groups, classify samples (deterministic code + AI) into
    a `group` column and return (group_col, labelmap) for psi_deploy; else (None, None) -> psi_deploy's
    default treated-vs-control split. Degrades to (None, None) on any failure."""
    gc = getattr(cfg, "group_cfg", None)
    if not gc:
        return None, None
    try:
        import group_assign
    except Exception as e:
        print(f"  PSI: group_assign module unavailable ({e}) -> default treated-vs-control split")
        return None, None
    try:
        return group_assign.assign(P, cfg, sel, reporter=reporter)
    except Exception as e:
        print(f"  PSI: group assignment failed ({e}) -> default treated-vs-control")
        return None, None


def _persist_cfg(P, cfg):
    """Save the (non-secret) RunConfig to config.json so a --resume uses any corrected values."""
    try:
        json.dump(asdict(cfg), open(P.config, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        print(f"  WARN: could not persist run config to {P.config} ({e}) -- a --resume may use stale values")


def _ai_cfg_from(cfg):
    """Build the ai_clean cfg dict from the (possibly mid-run-edited) run config. Re-read each time a pass
    runs so a provider/model/base_url switch made via the AI-fix form is actually picked up on retry."""
    return {"provider": cfg.provider, "model": cfg.model, "concurrency": cfg.concurrency,
            "base_url": getattr(cfg, "base_url", "") or "", "max_retries": 8,
            "max_tokens": int(getattr(cfg, "max_tokens", 60000) or 60000),
            "disable_reasoning": getattr(cfg, "disable_reasoning", False)}


def _apply_ai_fix(cfg, fix, P):
    """Apply an AI-fix payload (from the inline fix form / CLI prompt) to cfg IN PLACE. Returns True if the
    user chose to turn AI OFF (sets cfg.skip_ai), else False after switching provider/model/key/base_url.
    Shared by the preflight loop and the mid-run resilient-pass loop so both behave identically."""
    if not fix or fix.get("action") in ("skip_ai", "cancel"):
        cfg.skip_ai = True
        _persist_cfg(P, cfg)
        return True
    if fix.get("provider"):
        newp = llm_providers.normalize_provider(fix["provider"])
        if newp != cfg.provider and not fix.get("model"):
            cfg.model = llm_providers.DEFAULT_MODEL[newp]   # default model for the new provider
        cfg.provider = newp
    if fix.get("model"):
        cfg.model = str(fix["model"]).strip()
    if "base_url" in fix:                              # set OR clear (blank -> provider default); honored for openai/gemini/anthropic, ignored by ollama
        cfg.base_url = str(fix.get("base_url") or "").strip()
    if fix.get("api_key"):
        os.environ[llm_providers.KEY_ENV[cfg.provider]] = str(fix["api_key"]).strip()
    _persist_cfg(P, cfg)
    return False


def _ensure_ai_works(cfg, P, reporter, ai_fix_fn):
    """Preflight the AI provider/model/key. On failure (bad key/model/etc.) PAUSE: the UI shows a fix
    form (provider/model/key) + 'Turn off AI', the CLI prompts. Loop until AI works or the user turns
    it off (sets cfg.skip_ai). The server passes reporter.await_ai_fix; the CLI passes _console_ai_fix.
    """
    import ai_clean
    rate_tries = 0
    while not cfg.skip_ai:
        print(f"[AI] validating provider={cfg.provider} model={cfg.model} …")
        reporter.set_detail(f"validating {cfg.provider} / {cfg.model}…")
        err = ai_clean.preflight({"provider": cfg.provider, "model": cfg.model,
                                  "base_url": getattr(cfg, "base_url", "") or "",
                                  "disable_reasoning": getattr(cfg, "disable_reasoning", False)})
        if err is None:
            print("[AI] provider/model/key OK")
            return
        diag = llm_providers.classify_ai_error(err)
        if diag.get("category") == "rate" and rate_tries < _AI_PREFLIGHT_RATE_MAX:
            rate_tries += 1                          # transient throttle -> wait & re-submit (capped)
            print(f"[AI] rate limited ({diag['title']}); re-submitting in {_AI_RATE_RETRY_SECS}s "
                  f"(try {rate_tries}/{_AI_PREFLIGHT_RATE_MAX}, not pausing for a fix)…")
            reporter.set_detail(f"rate limited — re-submitting in {_AI_RATE_RETRY_SECS}s…")
            time.sleep(_AI_RATE_RETRY_SECS)
            continue
        print(f"[AI] preflight failed: {diag['title']} — {diag['detail']}")
        asker = ai_fix_fn or reporter.await_ai_fix
        fix = asker(diag, {"provider": cfg.provider, "model": cfg.model,
                           "base_url": getattr(cfg, "base_url", "") or ""})
        if _apply_ai_fix(cfg, fix, P):
            reporter.skip_stage("ai_compounds")
            reporter.skip_stage("ai_samples")
            print("** AI cleaning turned OFF -> running the deterministic stages only. **")
            return
        print(f"[AI] retrying with provider={cfg.provider} model={cfg.model} …")


def _run_ai_pass_resilient(pass_name, P, cfg, reporter, ai_fix_fn):
    """Run one AI cleaning pass, surviving a MID-RUN provider outage. ai_clean.run_pass RAISES only when so
    many batches go unanswered that the provider looks DOWN (not just flaky) — historically that aborted the
    whole run into an 'error' state with no inline retry, stranding hours of completed batches. Instead, PAUSE
    here with the SAME inline fix form as preflight (switch provider/model/key — e.g. proxy down -> Ollama —
    or turn AI off) and retry: ai_clean.run_pass resumes per-batch, so only the still-missing batches re-run.
    Turning AI off drop-fills the unanswered batches as Unknown so the pass COMPLETES with every answered
    batch kept. Returns when the pass is complete (or AI was turned off)."""
    import ai_clean
    while True:
        try:
            return ai_clean.run_pass(pass_name, P, _ai_cfg_from(cfg), reporter=reporter)
        except Exception as e:   # run_pass RAISES only for a mid-pass provider outage (too many to safely drop)
            d = llm_providers.classify_ai_error(e)
            diag = {"title": "AI provider went down mid-run",
                    "detail": (str(e) + "  " + (d.get("detail") or "")).strip()[:600],
                    "category": d.get("category") or "network"}
            print(f"[AI:{pass_name}] PAUSED mid-pass — {diag['title']}: {diag['detail']}")
            reporter.set_detail("AI provider down mid-run — switch provider or turn AI off to continue…")
            asker = ai_fix_fn or reporter.await_ai_fix
            fix = asker(diag, {"provider": cfg.provider, "model": cfg.model,
                               "base_url": getattr(cfg, "base_url", "") or ""})
            if _apply_ai_fix(cfg, fix, P):
                # turn AI off mid-pass: complete THIS pass deterministically (keep every answered batch,
                # Unknown-fill only the unanswered ones) rather than discard the work already done. cfg.skip_ai
                # is now set, so the caller skips any later AI pass (compounds runs before samples).
                ai_clean.drop_fill_missing(pass_name, P)
                print(f"** AI turned OFF mid-{pass_name}: answered batches kept, rest Unknown; "
                      f"remaining AI stages skipped. **")
                return {"skipped_remaining": True}
            print(f"[AI:{pass_name}] retrying with provider={cfg.provider} model={cfg.model} …")


def _ensure_provider_key_in_env(provider):
    """Idempotently load the AI provider's API key from the persisted settings into os.environ. The server
    pushes the key to the env only when a run STARTS (server.py), and the CLI only via its flags; a RESUME
    replays NEITHER, so without this a resumed run reaches the first AI step (cellline_match, then
    runtable_annotate) with no OPENAI_API_KEY -> 'Missing credentials' -> silent deterministic fallback.
    No-op if the key is already set, the provider is keyless (ollama), or the settings file is absent."""
    if not provider or provider == "ollama" or llm_providers.have_key(provider):
        return
    try:
        import json as _json
        sp = os.path.join(os.path.expanduser("~"), ".geo_pipeline_settings.json")
        with open(sp, encoding="utf-8") as f:
            settings = _json.load(f)
        keys = settings.get("api_keys") or {}
        key = (keys.get(provider) or settings.get("api_key") or "").strip()
        if key:
            os.environ[llm_providers.KEY_ENV[provider]] = key
            print(f"  [key] loaded {provider} API key from ~/.geo_pipeline_settings.json (resume-safe)")
    except Exception:
        pass


def run_pipeline(cfg, P, reporter=NULL, select_fn=None, secrets=None, cluster_fix_fn=None,
                 ai_fix_fn=None):
    """Execute the full pipeline (7 base + 5 deep-dive + 2 cluster stages) for a run dir.

    Single source of truth shared by the CLI (`main`) and the web server (`server.py`).
    Assumes the Anthropic key is already in the environment (or cfg.skip_ai is set).
    `reporter` is a progress.RunReporter (or the NULL no-op) driving the front-end
    stepper / ETA / live log. `select_fn(ranked)->canonical|None` lets a caller supply the
    manual cell-line pick (CLI prompt); the server uses reporter.await_selection instead.
    `secrets` carries transient, never-persisted values (e.g. {"ssh_password": ...}).
    `cluster_fix_fn(diagnosis, current)->fix|None` lets a CLI supply corrected cluster info when an
    autonomous upload fails (the server uses reporter.await_cluster_fix instead) so a bad upload is
    retried in place without re-running the pipeline.
    Resumable via pipeline_state.json exactly as before.
    """
    secrets = secrets or {}
    if not getattr(cfg, "skip_ai", False):       # resume-safe: a resumed run never replayed the start-form's key
        _ensure_provider_key_in_env(cfg.provider)
    state = _load_state(P)
    cap_int = None if cfg.cap == "unlimited" else int(cfg.cap)
    fetch_max = 100000 if cfg.cap == "unlimited" else int(cfg.cap)

    reporter.set_meta(query=cfg.query, cap=cfg.cap, model=cfg.model, provider=cfg.provider,
                      skip_ai=cfg.skip_ai, run_dir=P.run_dir)
    if cfg.skip_ai:
        reporter.skip_stage("ai_compounds")
        reporter.skip_stage("ai_samples")

    # Phase range: run only stages in [start_stage, end_stage]; everything outside is skipped.
    start_i = _idx(getattr(cfg, "start_stage", "fetch") or "fetch")
    end_i = _idx(getattr(cfg, "end_stage", "bed_submit") or "bed_submit")
    if start_i < 0:
        start_i = 0
    if end_i < 0:
        end_i = len(_STAGE_ORDER) - 1
    if start_i >= _idx("select"):
        cfg.deep_dive = True   # starting at/after 'select' needs the deep-dive branch to load `deep`

    def in_range(key):
        i = _idx(key)
        return i >= 0 and start_i <= i <= end_i

    def done(stage):
        return state.get(stage) == "done"

    def mark(stage):
        state[stage] = "done"
        _save_state(P, state)

    def begin(key):
        """Begin a stage; return True if it still needs to run (in range AND not already done)."""
        if not in_range(key):
            reporter.skip_stage(key)
            return False
        reporter.begin_stage(key)
        return not done(key)

    # Mid-pipeline START: stage in the artifacts the skipped earlier stages would have produced,
    # and mark those stages done so begin() skips them (must run BEFORE the AI preflight below).
    _inject_start_artifacts(cfg, P, state, start_i)

    print(f"\n=== PIPELINE run_dir={P.run_dir} ===")
    print(f"query={cfg.query!r} cap={cfg.cap} model={cfg.model} skip_ai={cfg.skip_ai}\n")

    # 0 AI PREFLIGHT: validate provider/model/key up front (unless skipping, or AI already done on a
    # resume) so a bad model/key PAUSES for a fix (or 'turn off AI') instead of failing mid-run.
    if not cfg.skip_ai and not (done("ai_compounds") and done("ai_samples")):
        _ensure_ai_works(cfg, P, reporter, ai_fix_fn)

    # 1 FETCH
    if begin("fetch"):
        fetch_5000_ncbi.run(cfg.query, fetch_max, P, cfg.ncbi_key, reporter=reporter)
        mark("fetch")
    reporter.complete_stage("fetch")

    # 2 EXTRACT + PROTOCOL
    if begin("extract"):
        structured_extract.run(P, cfg.ncbi_key, cap_int, reporter=reporter, workers=cfg.concurrency)
        mark("extract")
    reporter.complete_stage("extract")

    # 3 PREP AI BATCHES
    if begin("prep"):
        prep_ai.build_batches(P, reporter=reporter)
        mark("prep")
    reporter.complete_stage("prep")

    # 4/5 AI PASSES — resilient to a MID-RUN provider outage: if the proxy dies after hours of work, PAUSE
    # for an inline fix (switch provider/model/key, or turn AI off) and resume per-batch, rather than crash
    # the whole run into 'error' with the completed batches stranded.
    if cfg.skip_ai:
        print("[AI] skipped")
    else:
        if begin("ai_compounds"):
            _run_ai_pass_resilient("compounds", P, cfg, reporter, ai_fix_fn)
            mark("ai_compounds")
        reporter.complete_stage("ai_compounds")
        if cfg.skip_ai:                       # user turned AI off during the compounds pass
            reporter.skip_stage("ai_samples")
        else:
            if begin("ai_samples"):
                _run_ai_pass_resilient("samples", P, cfg, reporter, ai_fix_fn)
                mark("ai_samples")
            reporter.complete_stage("ai_samples")

    # 6 MERGE
    if begin("merge"):
        merge_ai.merge_compounds(P)
        merge_ai.merge_samples(P)
        mark("merge")
    reporter.complete_stage("merge")

    # 7 BUILD (idempotent — rewrites the tables + cellline_index.json)
    summary = None
    if begin("build"):                 # respect the done-check on resume (begin handles in-range + skip)
        summary = build_final.build_all(P, module=getattr(cfg, "module", "bulk_rna_seq") or "bulk_rna_seq")
        mark("build")
    reporter.complete_stage("build")

    print(f"\n=== Tables in {P.tables_dir} ===  Headline: ncbi_final_splicing.csv")

    # 8-14 DEEP DIVE (+ cluster handoff): best cell line -> Run Selector metadata + SraAccList
    _DEEP = ("runtable_fetch", "runtable_build", "cellline_match", "runtable_annotate")
    _CLUSTER = ("cluster_bundle", "cluster_submit")
    _STAR = ("star_bundle", "star_submit")
    _BED = ("bed_bundle", "bed_submit")
    _PSI = ("psi_bundle", "psi_submit")
    _CONCORDANCE = ("concordance_bundle", "concordance_submit")
    if not cfg.deep_dive:
        for k in ("select",) + _DEEP + _CLUSTER + _STAR + _BED + _PSI + _CONCORDANCE:
            reporter.skip_stage(k)
    else:
        import deepdive_select
        deep = None
        if begin("select"):
            deepdive_select.consolidate(P, reporter)   # merge cell-line NAME variants BEFORE ranking
            ranked = deepdive_select.rank_candidates(P)
            if not ranked:
                print("[deep-dive] no real cell line in the splicing table -> skipping deep dive")
            else:
                chosen = None
                if cfg.pick_mode == "manual":
                    picker = select_fn or reporter.await_selection
                    chosen = picker(ranked)
                deep = deepdive_select.run(P, reporter, chosen=chosen)
            if deep is not None:       # only mark done once a selection actually succeeded (else resume re-selects)
                mark("select")
        else:
            # resume: reuse the prior selection if present (guarded), else re-select from the current index
            if os.path.exists(P.cellline_selection):
                try:
                    with open(P.cellline_selection, encoding="utf-8") as f:
                        deep = json.load(f)
                except Exception as e:
                    print(f"[deep-dive] cellline_selection.json unreadable ({e}); re-selecting")
                    deep = None
            if deep is None:
                deepdive_select.consolidate(P, reporter)
                deep = deepdive_select.run(P, reporter)
        reporter.complete_stage("select")

        # PROJECT ISOLATION: scope the cluster JOB_TAG (-> the cluster folder AND every stage's job names) by
        # the cell line, so reusing ONE instance name for a DIFFERENT project never shares the prior project's
        # cluster folder/jobs. A resume keeps whatever tag the bundle was ALREADY deployed under, so an
        # in-flight run is never relocated. Normalized slug -> stable even if the AI respells the line.
        if deep is not None and cfg.cluster_mode != "off" and cfg.cluster_cfg is not None:
            import cluster_deploy as _cd
            _deployed = _cd._read_config_jobtag(P)
            cfg.cluster_cfg["JOB_TAG"] = _deployed or _cd.project_job_tag(cfg.cluster_cfg.get("JOB_TAG", ""), deep)

        if deep is None:
            for k in _DEEP + _CLUSTER + _STAR + _BED:
                reporter.skip_stage(k)
        else:
            if begin("runtable_fetch"):
                import runtable_fetch
                _fr = runtable_fetch.run(P, deep, cfg.ncbi_key, reporter=reporter, workers=cfg.concurrency)
                _nf = (_fr or {}).get("n_failed", 0); _ns = (_fr or {}).get("studies", 0) or 1
                # Do NOT silently mark DONE (and then build a PARTIAL runtable) when studies failed to fetch.
                # A HIGH miss rate is environmental (disk full / network) -> STOP so the user fixes it and
                # resumes; fetch re-runs because it's NOT marked done, and skips the already-cached studies.
                # A FEW misses are likely withdrawn studies -> proceed, but still leave it unmarked so a
                # later resume retries them. (Before this, an ENOSPC burst dropped 967/1329 studies silently.)
                if _nf == 0:
                    mark("runtable_fetch")
                elif _nf > max(5, _ns // 10):
                    raise RuntimeError("runtable_fetch: %d/%d studies could not be fetched (likely disk space "
                                       "or network). Fix it and resume -- refusing to build a partial runtable." % (_nf, _ns))
                else:
                    print("  runtable_fetch: %d/%d studies unfetched (likely withdrawn); proceeding but NOT "
                          "marking done so a resume retries them." % (_nf, _ns))
            reporter.complete_stage("runtable_fetch")

            if begin("runtable_build"):
                import runtable_build
                runtable_build.run(P, deep, reporter=reporter)
                mark("runtable_build")
            reporter.complete_stage("runtable_build")

            if begin("cellline_match"):
                import cellline_match
                # Splicing module (bulk_rna_seq): keep ONLY RNA-Seq runs so STAR/AltAnalyze never process
                # ChIP/MeDIP/ATAC/WGS/OTHER(RASL) runs. Other modules keep every assay (assay_keep=None).
                _assay_keep = ({"rnaseq"} if (getattr(cfg, "module", "bulk_rna_seq") or "bulk_rna_seq")
                               == "bulk_rna_seq" else None)
                cellline_match.run(P, deep, _ai_cfg_from(cfg), cfg.skip_ai, reporter=reporter, assay_keep=_assay_keep)
                mark("cellline_match")
            reporter.complete_stage("cellline_match")

            if begin("runtable_annotate"):
                import runtable_annotate
                runtable_annotate.run(P, deep, reporter=reporter)
                mark("runtable_annotate")
            reporter.complete_stage("runtable_annotate")

            # 13-14 CLUSTER HANDOFF (gated on cluster_mode)
            submit_res = None
            star_submit_res = None
            if cfg.cluster_mode == "off":
                for k in _CLUSTER:
                    reporter.skip_stage(k)
            else:
                import cluster_deploy
                if begin("cluster_bundle"):
                    cluster_deploy.build_bundle(P, deep, cfg.cluster_cfg, reporter=reporter)
                    mark("cluster_bundle")
                reporter.complete_stage("cluster_bundle")

                submit_res = None
                if cfg.cluster_mode == "autonomous":
                    if begin("cluster_submit"):
                        submit_res = _submit_with_retry(P, cfg, deep, secrets, reporter, cluster_fix_fn)
                        if (submit_res or {}).get("submitted"):
                            mark("cluster_submit")   # only mark done if the upload/launch actually succeeded
                    reporter.complete_stage("cluster_submit")
                else:
                    reporter.skip_stage("cluster_submit")
                _save_cluster_status(P, cfg.cluster_mode, submit_res)

            # 15-16 STAR ALIGNMENT (Bulk RNA-seq module; auto-chained AFTER the download)
            star_on = (getattr(cfg, "module", "bulk_rna_seq") == "bulk_rna_seq"
                       and cfg.cluster_mode != "off")
            if not star_on:
                for k in _STAR:
                    reporter.skip_stage(k)
            else:
                import cluster_deploy
                import star_deploy
                download_root = (cluster_deploy._read_config_root(P)
                                 or cluster_deploy._effective_root(cfg.cluster_cfg, deep))
                dl_tag = (cfg.cluster_cfg or {}).get("JOB_TAG", "sra")
                star_built = None
                if begin("star_bundle"):
                    star_built = star_deploy.build_star_bundle(
                        P, deep, download_root, getattr(cfg, "star_cfg", None),
                        download_job_tag=dl_tag, reporter=reporter)
                    mark("star_bundle")
                reporter.complete_stage("star_bundle")

                star_submit_res = None
                bundle_ready = (star_built is not None
                                or os.path.exists(os.path.join(P.star_dir, "config.sh")))
                # T5.1: the STAR launcher polls <download_root>/PIPELINE_COMPLETE.txt. Arm it only if the
                # upstream download actually went (submitted) OR was DELIBERATELY skipped by the phase range
                # (then we pre-touch the sentinel). If the download submit was attempted in-range and
                # FAILED, arming the launcher would poll forever -> skip STAR submit instead.
                download_go = (not in_range("cluster_submit")) or bool((submit_res or {}).get("submitted"))
                if not bundle_ready:
                    reporter.skip_stage("star_submit")
                elif not download_go:
                    reporter.skip_stage("star_submit")
                    print("  STAR SUBMIT: skipped — the upstream download submit failed, so not arming a "
                          "launcher that would poll a sentinel that never appears (fix the download, then resume).")
                    reporter.set_detail("STAR submit skipped — download submit failed")
                elif cfg.cluster_mode == "autonomous":
                    if begin("star_submit"):
                        star_submit_res = star_deploy.submit_star_over_ssh(
                            P, cfg.cluster_cfg, secrets, download_root, reporter=reporter,
                            prior_skipped=not in_range("cluster_submit"))
                        if (star_submit_res or {}).get("submitted"):
                            mark("star_submit")      # only mark done if the SSH submit actually succeeded
                    reporter.complete_stage("star_submit")
                else:
                    reporter.skip_stage("star_submit")
                _save_star_status(P, cfg, star_built, star_submit_res)

            # 17-18 BAM->BED (AltAnalyze junction/exon; auto-chained AFTER STAR; on by default for bulk_rna_seq)
            bed_on = (getattr(cfg, "module", "bulk_rna_seq") == "bulk_rna_seq"
                      and cfg.cluster_mode != "off"
                      and str((getattr(cfg, "bed_cfg", None) or {}).get("enabled", "1")).lower()
                          not in ("0", "off", "false", "no"))
            if not bed_on:
                for k in _BED:
                    reporter.skip_stage(k)
            else:
                import cluster_deploy
                import bed_deploy
                download_root = (cluster_deploy._read_config_root(P)
                                 or cluster_deploy._effective_root(cfg.cluster_cfg, deep))
                dl_tag = (cfg.cluster_cfg or {}).get("JOB_TAG", "sra")
                bam_out_root = f"{download_root.rstrip('/')}/STAR_bams"
                star_tag = f"{dl_tag}_star"
                bed_built = None
                if begin("bed_bundle"):
                    bed_built = bed_deploy.build_bed_bundle(
                        P, deep, bam_out_root, getattr(cfg, "bed_cfg", None),
                        star_job_tag=star_tag, reporter=reporter)
                    mark("bed_bundle")
                reporter.complete_stage("bed_bundle")

                bed_submit_res = None
                bed_ready = (bed_built is not None
                             or os.path.exists(os.path.join(P.bed_dir, "config.sh")))
                # T5.1: arm the BED launcher only if STAR actually went (submitted) OR was deliberately
                # phase-range skipped (then pre-touch the sentinel). A failed in-range STAR submit cascades
                # the skip to BED rather than arming a launcher that polls forever.
                star_go = (not in_range("star_submit")) or bool((star_submit_res or {}).get("submitted"))
                if not bed_ready:
                    reporter.skip_stage("bed_submit")
                elif not star_go:
                    reporter.skip_stage("bed_submit")
                    print("  BED SUBMIT: skipped — the upstream STAR submit failed, so not arming a launcher "
                          "that would poll a sentinel that never appears (fix STAR, then resume).")
                    reporter.set_detail("BED submit skipped — STAR submit failed")
                elif cfg.cluster_mode == "autonomous":
                    if begin("bed_submit"):
                        bed_submit_res = bed_deploy.submit_bed_over_ssh(
                            P, cfg.cluster_cfg, secrets, bam_out_root, reporter=reporter,
                            prior_skipped=not in_range("star_submit"))
                        if (bed_submit_res or {}).get("submitted"):
                            mark("bed_submit")       # only mark done if the SSH submit actually succeeded
                    reporter.complete_stage("bed_submit")
                else:
                    reporter.skip_stage("bed_submit")
                _save_bed_status(P, cfg, bed_built, bed_submit_res)

            # 19-20 AltAnalyze splicing (PSI; auto-chained AFTER BAM->BED; on by default for bulk_rna_seq)
            psi_on = (getattr(cfg, "module", "bulk_rna_seq") == "bulk_rna_seq"
                      and cfg.cluster_mode != "off"
                      and str((getattr(cfg, "psi_cfg", None) or {}).get("enabled", "1")).lower()
                          not in ("0", "off", "false", "no"))
            if not psi_on:
                for k in _PSI:
                    reporter.skip_stage(k)
            else:
                import cluster_deploy
                import psi_deploy
                download_root = (cluster_deploy._read_config_root(P)
                                 or cluster_deploy._effective_root(cfg.cluster_cfg, deep))
                dl_tag = (cfg.cluster_cfg or {}).get("JOB_TAG", "sra")
                bam_out_root = f"{download_root.rstrip('/')}/STAR_bams"
                # Phase B: if the user defined comparison groups, assign samples (fixed code + AI) and ship
                # that `group` column; else build_psi_bundle defaults to the treated-vs-control split.
                group_col, labelmap = _psi_group_inputs(P, cfg, deep, reporter)
                psi_built = None
                if begin("psi_bundle"):
                    psi_built = psi_deploy.build_psi_bundle(
                        P, deep, bam_out_root, getattr(cfg, "psi_cfg", None),
                        download_job_tag=dl_tag, reporter=reporter,
                        group_col=group_col, labelmap=labelmap)
                    mark("psi_bundle")
                reporter.complete_stage("psi_bundle")

                psi_submit_res = None
                psi_ready = (psi_built is not None
                             or os.path.exists(os.path.join(P.psi_dir, "config.sh")))
                # T5.1: arm the PSI launcher only if BED actually went (submitted) OR was deliberately
                # phase-range skipped (then pre-touch the BED sentinel). A failed in-range BED submit
                # cascades the skip to PSI rather than arming a launcher that polls forever.
                _bed_res = locals().get("bed_submit_res")
                bed_go = (not in_range("bed_submit")) or bool((_bed_res or {}).get("submitted"))
                if not psi_ready:
                    reporter.skip_stage("psi_submit")
                elif not bed_go:
                    reporter.skip_stage("psi_submit")
                    print("  PSI SUBMIT: skipped — the upstream BED submit failed, so not arming a launcher "
                          "that would poll a sentinel that never appears (fix BED, then resume).")
                    reporter.set_detail("PSI submit skipped — BED submit failed")
                elif cfg.cluster_mode == "autonomous":
                    if begin("psi_submit"):
                        psi_submit_res = psi_deploy.submit_psi_over_ssh(
                            P, cfg.cluster_cfg, secrets, bam_out_root, reporter=reporter,
                            prior_skipped=not in_range("bed_submit"))
                        if (psi_submit_res or {}).get("submitted"):
                            mark("psi_submit")       # only mark done if the SSH submit actually succeeded
                    reporter.complete_stage("psi_submit")
                else:
                    reporter.skip_stage("psi_submit")
                _save_psi_status(P, cfg, psi_built, psi_submit_res)

            # 21-22 splicing concordance (drug PSI vs cancer atlas; auto-chained AFTER PSI; on by default for bulk_rna_seq)
            concordance_on = (getattr(cfg, "module", "bulk_rna_seq") == "bulk_rna_seq"
                              and cfg.cluster_mode != "off"
                              and str((getattr(cfg, "concordance_cfg", None) or {}).get("enabled", "1")).lower()
                                  not in ("0", "off", "false", "no"))
            if not concordance_on:
                for k in _CONCORDANCE:
                    reporter.skip_stage(k)
            else:
                import cluster_deploy
                import concordance_deploy
                download_root = (cluster_deploy._read_config_root(P)
                                 or cluster_deploy._effective_root(cfg.cluster_cfg, deep))
                dl_tag = (cfg.cluster_cfg or {}).get("JOB_TAG", "sra")
                bam_out_root = f"{download_root.rstrip('/')}/STAR_bams"
                concordance_built = None
                if begin("concordance_bundle"):
                    concordance_built = concordance_deploy.build_concordance_bundle(
                        P, deep, bam_out_root, getattr(cfg, "concordance_cfg", None),
                        download_job_tag=dl_tag, reporter=reporter, ai_cfg=_ai_cfg_from(cfg))
                    mark("concordance_bundle")
                reporter.complete_stage("concordance_bundle")

                concordance_submit_res = None
                concordance_ready = (concordance_built is not None
                                     or os.path.exists(os.path.join(P.concordance_dir, "config.sh")))
                # arm the concordance launcher only if PSI actually went (submitted) OR was deliberately
                # phase-range skipped (then pre-touch the PSI sentinel). A failed in-range PSI submit cascades
                # the skip to concordance rather than arming a launcher that polls a sentinel that never appears.
                _psi_res = locals().get("psi_submit_res")
                psi_go = (not in_range("psi_submit")) or bool((_psi_res or {}).get("submitted"))
                if not concordance_ready:
                    reporter.skip_stage("concordance_submit")
                elif not psi_go:
                    reporter.skip_stage("concordance_submit")
                    print("  CONCORDANCE SUBMIT: skipped — the upstream PSI submit failed/was off, so not arming "
                          "a launcher that would poll a sentinel that never appears (fix PSI, then resume).")
                    reporter.set_detail("Concordance submit skipped — PSI not submitted")
                elif cfg.cluster_mode == "autonomous":
                    if begin("concordance_submit"):
                        concordance_submit_res = concordance_deploy.submit_concordance_over_ssh(
                            P, cfg.cluster_cfg, secrets, bam_out_root, reporter=reporter,
                            prior_skipped=not in_range("psi_submit"))
                        _cres = concordance_submit_res or {}
                        # mark done on a real submit OR a clean no-atlas SKIP (concordance is OPTIONAL — PSI is
                        # the terminal stage; a cell line with no cancer atlas finishes cleanly at PSI).
                        if _cres.get("submitted") or _cres.get("skipped"):
                            mark("concordance_submit")
                        if _cres.get("skipped"):
                            reporter.set_detail("concordance skipped — no cancer atlas for this cell line "
                                                "(PSI is the final output)")
                    reporter.complete_stage("concordance_submit")
                else:
                    reporter.skip_stage("concordance_submit")
                _save_concordance_status(P, cfg, concordance_built, concordance_submit_res)

    deep_dive = _deep_summary(P)
    if deep_dive:
        summary = dict(summary or {}, deep_dive=deep_dive)
        print(f"\n=== DEEP DIVE: {deep_dive.get('canonical')} -> {P.sra_acc_list} "
              f"({deep_dive.get('n_accessions', 0)} run accessions) ===")
    cluster = _cluster_summary(P)
    if cluster:
        summary = dict(summary or {}, cluster=cluster)
        if cluster.get("submitted"):
            print(f"=== CLUSTER: uploaded & launched on {cluster.get('host', 'the cluster')} "
                  f"(root {cluster.get('pipeline_root')}) ===")
        else:
            print(f"=== CLUSTER BUNDLE -> {P.cluster_bundle_zip} (root {cluster.get('pipeline_root')}) ===")
    star = _star_summary(P)
    if star:
        summary = dict(summary or {}, star=star)
        if star.get("submitted"):
            print(f"=== STAR: alignment auto-chained on the cluster (organism {star.get('organism')}; "
                  f"BAMs -> {star.get('bam_out')}) ===")
        elif star.get("bundle"):
            print(f"=== STAR BUNDLE -> {P.star_bundle_zip} (organism {star.get('organism')}) ===")
    bed = _bed_summary(P)
    if bed:
        summary = dict(summary or {}, bed=bed)
        if bed.get("submitted"):
            print(f"=== BED: BAM->BED auto-chained on the cluster (species {bed.get('species')}; "
                  f"BEDs -> {bed.get('bam_input')}) ===")
        elif bed.get("bundle"):
            print(f"=== BED BUNDLE -> {P.bed_bundle_zip} (species {bed.get('species')}) ===")
    psi = _psi_summary(P)
    if psi:
        summary = dict(summary or {}, psi=psi)
        if psi.get("submitted"):
            print(f"=== PSI: AltAnalyze splicing auto-chained on the cluster (species {psi.get('species')}; "
                  f"{'grouped dPSI' if psi.get('grouped') else 'groupless PSI'}) ===")
        elif psi.get("bundle"):
            print(f"=== PSI BUNDLE -> {P.psi_bundle_zip} (species {psi.get('species')}) ===")
    concordance = _concordance_summary(P)
    if concordance:
        summary = dict(summary or {}, concordance=concordance)
        if concordance.get("submitted"):
            print(f"=== CONCORDANCE: drug-vs-cancer concordance auto-chained on the cluster "
                  f"(atlas {concordance.get('atlas')}, {concordance.get('n_queries')} query/queries) ===")
        elif concordance.get("bundle"):
            print(f"=== CONCORDANCE BUNDLE -> {P.concordance_bundle_zip} (atlas {concordance.get('atlas')}) ===")

    print(f"\n=== DONE. run_dir={P.run_dir} ===")
    try:
        files = _list_outputs(P)
    except Exception as e:                  # a summary/listing error must not mask a successful run
        print(f"  (warning: listing outputs failed: {e})")
        files = []
    reporter.finish(result=summary, files=files)
    return {"summary": summary, "files": files, "run_dir": P.run_dir}


if __name__ == "__main__":
    main()
