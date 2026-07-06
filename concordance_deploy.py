# -*- coding: utf-8 -*-
"""
Splicing-concordance stage deploy -- the drug-repurposing readout that runs AFTER AltAnalyze PSI on the
cluster. When the analysis module is "bulk_rna_seq" and the cluster is on, this fills the vendored
concordance_template/ config.sh from the form + the run, ships it to <download_root>/concordance, and
AUTO-CHAINS it: a launcher job (concordance_launch.sh) waits on the PSI stage's PIPELINE_COMPLETE.txt,
then runs ./run_concordance_pipeline.sh, which gathers the per-drug PSI signatures and scores each against
one or more CANCER-SUBTYPE atlases (the vendored splicingConcordance_advanced.py) -> a ranked
reversal-candidate summary per atlas.

The scorer + ranker + (for AML) the patient-count table are VENDORED in the bundle (all-in-one). The only
external dependency is AltAnalyze's export/UI/unique modules (the scorer imports them); the PSI stage
already resolved an AltAnalyze install, so we reuse it on PYTHONPATH. The cancer atlas is auto-selected from
the cell line via cancer_atlas_registry.json (MDS-L -> AML/MDS, A549 -> lung LUAD+LUSC); the GUI can override.

Reuses cluster_deploy's fill_config / SSH transport / diagnose_failure.
"""
import os
import re
import json
import shutil

from progress import NULL
import cluster_deploy
import star_deploy   # reuse detect_organism (organism consistency across stages)
from bed_deploy import organism_to_species
from cellline_match import _slug as _cl_slug

HERE = os.path.dirname(os.path.abspath(__file__))
CONCORDANCE_TEMPLATE_DIR = os.path.join(HERE, "concordance_template")
ATLAS_REGISTRY = os.path.join(HERE, "cancer_atlas_registry.json")

# config.sh vars we fill (name -> default). Numerics are written unquoted.
CONCORDANCE_CONFIG_DEFAULTS = {
    "PSI_ROOT": "/data/CHANGE_ME/psi",
    "PSI_EVENTS_DIR": "",            # "" => config derives $PSI_ROOT/output/AltResults/AlternativeOutput/Events-dPSI_0.1_rawp
    "PIPELINE_ROOT": "/data/CHANGE_ME/concordance",
    "ALTANALYZE_HOME": "",           # "" => $PSI_ROOT/altanalyze_home (resolved at submit to one with export/UI/unique)
    "CONCORD_SCRIPT": "",            # "" => $SCRIPTS_DIR/splicingConcordance_advanced.py (vendored)
    "RAWP": "0.05",
    "DPSI": "0.1",
    "REMOVE_IR": "0",
    "MIN_OVERLAP": 5,
    "CONC_THRESHOLD": "0.3",
    "CANCER_ATLAS": "auto",
    "ORGANISM": "Homo sapiens",
    "SPECIES": "",
    "EXPNAME": "splicing",
    "THREADS": 1,
    "MEM_MB": 8000,
    "WALL": "1108:00",   # -W: normal-queue MAX (66480 min ~ 46 days) so the job never dies to walltime
    "LSF_QUEUE": "",
    "JOB_TAG": "concordance",
    "WATCHDOG_INTERVAL_MIN": 30,
    "MAX_RESUBMITS": 2,
    "ABSOLUTE_MAX_PASSES": 960,
    "MAX_WALL_HOURS": 336,
    "PYTHON_MODULE": "python/2.7.5",
}
CONCORDANCE_NUMERIC = {"MIN_OVERLAP", "THREADS", "MEM_MB", "WATCHDOG_INTERVAL_MIN", "MAX_RESUBMITS",
                       "ABSOLUTE_MAX_PASSES", "MAX_WALL_HOURS"}
# concord_cfg keys that are NOT config.sh vars (deploy-time only) -- stripped before fill_config.
_DEPLOY_ONLY = ("enabled", "cancer_atlas", "QUERY_DIRS")

# AltAnalyze installs to fall back to when resolving export/UI/unique for the scorer's PYTHONPATH.
_ALTANALYZE_FALLBACKS = ("/data/salomonis2/software/AltAnalyze-91/AltAnalyze",
                         "/data/salomonis2/software/AltAnalyze")


def _copy_lf(srcf, destf):
    txt = open(srcf, encoding="utf-8", errors="replace").read().replace("\r\n", "\n").replace("\r", "\n")
    with open(destf, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def _resolve_concordance_cfg(concord_cfg):
    vals = dict(CONCORDANCE_CONFIG_DEFAULTS)
    for k in CONCORDANCE_CONFIG_DEFAULTS:
        if concord_cfg and k in concord_cfg and str(concord_cfg[k]).strip() != "":
            vals[k] = concord_cfg[k]
    return vals


def _set_config_var(cfg_path, name, value):
    txt = open(cfg_path, encoding="utf-8").read()
    rep = f"{name}={cluster_deploy._shval(name, value, CONCORDANCE_NUMERIC, CONCORDANCE_CONFIG_DEFAULTS)}"
    txt = re.sub(rf"(?m)^{re.escape(name)}=.*$", lambda m, r=rep: r, txt, count=1)
    with open(cfg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def _read_cfg_var(cfg_path, name):
    try:
        m = re.search(rf'(?m)^{re.escape(name)}="?(.*?)"?\s*$', open(cfg_path, encoding="utf-8").read())
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def _norm(name):
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


# ---- cancer atlas resolution (cell line -> atlas of OncoSplice subtype signatures) --------------
def _load_registry():
    try:
        with open(ATLAS_REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  CONCORDANCE: could not load atlas registry ({e})")
        return {}


def _resolve_atlas(sel, species, override=None, ai_cfg=None):
    """Return (atlas_key, atlas_obj) for this run, or (None, None). Resolution order:
      1. a GUI CANCER_ATLAS override (an atlases.<key>) wins;
      2. the explicit celllines.<norm> map;
      3. an AI FALLBACK that maps the cell line to the best available atlas (only when ai_cfg is supplied).
    None => concordance no-ops for this line (until an atlas is set in the GUI)."""
    reg = _load_registry()
    atlases = (reg or {}).get("atlases", {})
    if override and str(override).strip().lower() not in ("", "auto") and override in atlases:
        return override, atlases[override]
    celllines = (reg or {}).get("celllines", {})
    names = []
    if sel:
        names.append(sel.get("canonical", ""))
        names += list(sel.get("aliases", []) or [])
    for nm in names:
        key = celllines.get(_norm(nm))
        if key and key in atlases:
            atlas = atlases[key]
            if not species or not atlas.get("species") or atlas["species"] == species:
                return key, atlas
    return _ai_resolve_atlas(sel, atlases, species, ai_cfg)   # no explicit mapping -> AI fallback (or None)


_ATLAS_INSTRUCTIONS = (
    "You map a human CANCER CELL LINE to the single best-matching cancer atlas for splicing concordance.\n"
    "You receive a JSON object with `cell_line` (canonical name), `aliases`, and `atlases` (an array of "
    "{key,label} -- the ONLY atlases available). Work out the cell line's cancer of origin (tissue + "
    "subtype) and pick the atlas whose cancer type matches best. Rules:\n"
    "- Call the emit tool ONCE with exactly one result.\n"
    "- atlas_key: EXACTLY one of the provided keys, or 'none' if no atlas is a reasonable cancer-of-origin "
    "match. Prefer the same tissue/lineage (e.g. a myeloid-leukemia line -> an AML atlas; a lung "
    "adenocarcinoma line -> a lung atlas; a breast line -> a breast atlas). NEVER invent a key.\n"
    "- reason: a few words (e.g. 'K562 = CML, closest myeloid = aml_mds')."
)

_ATLAS_TOOL = {
    "name": "emit_atlas_pick",
    "description": "Return one result: the best-matching cancer atlas key for the cell line, or 'none'.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["atlas_key", "reason"],
                    "properties": {
                        "atlas_key": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            }
        },
    },
}


def _ai_resolve_atlas(sel, atlases, species, ai_cfg):
    """AI FALLBACK: with no registry mapping, ask the model to map the cell line to the best AVAILABLE atlas
    key (grounded in the list, so it can't invent one). Returns (key, atlas) or (None, None). Best-effort:
    any failure (no key/provider, bad reply, endpoint down) yields (None, None) so the stage just no-ops."""
    if not ai_cfg or not sel:
        return None, None
    opts = [{"key": k, "label": (a or {}).get("label", k)}
            for k, a in atlases.items()
            if not (a or {}).get("cell_line_specific")          # ENCODE cell-line atlases: explicit-map only
            and (not species or not (a or {}).get("species") or (a or {}).get("species") == species)]
    if not opts:
        return None, None
    user = {"cell_line": sel.get("canonical", ""),
            "aliases": list(sel.get("aliases", []) or []), "atlases": opts}

    async def _go():
        import llm_providers
        provider = llm_providers.normalize_provider(ai_cfg.get("provider", "anthropic"))
        if provider != "ollama" and not llm_providers.have_key(provider):
            return None
        model = llm_providers.resolve_model(provider, ai_cfg.get("model"))
        client = llm_providers.make_client(provider, ai_cfg.get("max_retries", 8),
                                           base_url=ai_cfg.get("base_url"))
        try:
            results, _ = await llm_providers.classify(
                client, provider, model, _ATLAS_INSTRUCTIONS, user, _ATLAS_TOOL,
                ai_cfg.get("max_tokens", 2000), disable_reasoning=True)   # a lookup, not a reasoning task
            return results
        finally:
            await llm_providers.close_client(client)

    try:
        import asyncio
        results = asyncio.run(_go())
    except Exception as e:
        print(f"  CONCORDANCE: AI atlas fallback failed ({e}) -> no atlas resolved")
        return None, None
    key = str((results[0] or {}).get("atlas_key", "")).strip() if results else ""
    if key and key.lower() != "none" and key in atlases:
        atlas = atlases[key]
        if not species or not atlas.get("species") or atlas["species"] == species:
            print(f"  CONCORDANCE: AI mapped {sel.get('canonical', '?')!r} -> atlas '{key}' "
                  f"({(results[0] or {}).get('reason', '')})")
            return key, atlas
    return None, None


def _write_queries_tsv(dest, atlas_obj, concord_root):
    """Write queries.tsv (name<TAB>query_dir<TAB>counts_kind<TAB>counts_path). A vendored 'tsv' counts file
    (relative) resolves to the bundle upload dir (= concord_root, where SCRIPTS_DIR lands). Returns row count."""
    rows = []
    for q in (atlas_obj or {}).get("queries", []):
        kind = (q.get("counts_kind") or "none").strip()
        cf = (q.get("counts_file") or "").strip()
        if kind == "tsv" and cf and not cf.startswith("/"):
            cpath = concord_root.rstrip("/") + "/" + cf
        else:
            cpath = cf
        rows.append((q.get("name", "atlas"), q.get("query_dir", ""), kind, cpath))
    with open(dest, "w", encoding="utf-8", newline="\n") as f:
        f.write("# name\tquery_dir\tcounts_kind\tcounts_path\n")
        for name, qdir, kind, cpath in rows:
            f.write(f"{name}\t{qdir}\t{kind}\t{cpath}\n")
    return len(rows)


# ---- self-rescheduling launcher (waits on the PSI stage's PIPELINE_COMPLETE.txt) ----------------
def _concordance_launch_sh(psi_root, concord_root, concord_tag, check_min=30, max_wait_hours=336):
    pr = psi_root.rstrip("/")
    cr = concord_root.rstrip("/")
    return (
        "#!/usr/bin/env bash\n"
        "# concordance_launch.sh -- generated by SpliceScout. Self-rescheduling: checks if AltAnalyze PSI\n"
        "# finished; if so launches the concordance pipeline, else re-queues itself. Runs as short LSF jobs.\n"
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f"PSI_ROOT={cluster_deploy.shq(pr)}\n"
        f"CONCORD_ROOT={cluster_deploy.shq(cr)}\n"
        f"CHECK_MIN={int(check_min)}\n"
        f"MAX_WAIT_HOURS={int(max_wait_hours)}\n"
        f"JT={cluster_deploy.shq(concord_tag)}\n"
        "command -v bsub >/dev/null 2>&1 || { echo '[concord_launch] no bsub here' >&2; exit 0; }\n"
        "# concordance already finalized -> nothing to do\n"
        'if [ -f "$CONCORD_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$CONCORD_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        '  echo "[concord_launch] concordance already finalized -> stop"; exit 0\n'
        "fi\n"
        "# PSI finished (or stalled -> concordance on whatever signatures exist) -> TRY to launch. On failure\n"
        "# fall through to reschedule + retry (run_concordance_pipeline.sh is idempotent). Stop only on success.\n"
        'if [ -f "$PSI_ROOT/PIPELINE_COMPLETE.txt" ] || [ -f "$PSI_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        '  if [ ! -f "$PSI_ROOT/PIPELINE_COMPLETE.txt" ] && [ -f "$PSI_ROOT/PIPELINE_STALLED.txt" ]; then\n'
        '    mkdir -p "$CONCORD_ROOT" 2>/dev/null\n'
        '    echo "upstream PSI STALLED at $(date) -- scoring whatever drug signatures exist." \\\n'
        '      > "$CONCORD_ROOT/PIPELINE_INCOMPLETE_UPSTREAM.txt" 2>/dev/null\n'
        "  fi\n"
        '  echo "[concord_launch] PSI finished -> launching concordance pipeline"\n'
        '  if bash "$HERE/run_concordance_pipeline.sh"; then\n'
        '    echo "[concord_launch] concordance pipeline launched -> stop"; exit 0\n'
        "  fi\n"
        '  echo "[concord_launch] run_concordance_pipeline.sh FAILED -> will retry in $CHECK_MIN min" >&2\n'
        "fi\n"
        "# Bounded wait: abort only if past MAX_WAIT_HOURS AND PSI's watchdog.log is stale (dead chain).\n"
        'STAMP="$HERE/.launch_first_seen"\n'
        '[ -f "$STAMP" ] || date +%s > "$STAMP" 2>/dev/null\n'
        'now=$(date +%s); first=$(cat "$STAMP" 2>/dev/null || echo "$now")\n'
        'upwd="$PSI_ROOT/watchdog.log"; up_age=999999999\n'
        '[ -f "$upwd" ] && up_age=$(( now - $(stat -c %Y "$upwd" 2>/dev/null || echo "$now") ))\n'
        'if [ "$(( now - first ))" -gt "$(( MAX_WAIT_HOURS * 3600 ))" ] && [ "$up_age" -gt "$(( CHECK_MIN * 180 ))" ]; then\n'
        '  mkdir -p "$CONCORD_ROOT" 2>/dev/null\n'
        '  echo "concordance launcher gave up at $(date): PSI never finalized and its watchdog.log went stale (>${MAX_WAIT_HOURS}h)." \\\n'
        '    > "$CONCORD_ROOT/PIPELINE_LAUNCH_TIMEOUT.txt" 2>/dev/null\n'
        '  echo "[concord_launch] upstream dead -> giving up (PIPELINE_LAUNCH_TIMEOUT.txt written)" >&2; exit 0\n'
        "fi\n"
        "when=$(date -d \"+$CHECK_MIN min\" '+%Y:%m:%d:%H:%M' 2>/dev/null) || "
        "when=$(date -v+\"${CHECK_MIN}\"M '+%Y:%m:%d:%H:%M' 2>/dev/null)\n"
        'bsub -L /bin/bash -n 1 -M 1000 -W 66480 -b "$when" -J "${JT}_launch" \\\n'
        '     -o "$CONCORD_ROOT/launch.out" -e "$CONCORD_ROOT/launch.err" \\\n'
        '     "$HERE/concordance_launch.sh" >/dev/null 2>&1\n'
        'echo "[concord_launch] not done / will retry -> next check scheduled for $when"\n'
    )


def _write_concordance_instructions(P, vals, psi_root, concord_root, atlas_key, nqueries):
    atxt = (f"{atlas_key} ({nqueries} cancer atlas query/queries)" if atlas_key
            else "NO cancer atlas resolved for this cell line -- set 'cancer atlas' in the GUI")
    txt = (
        "Splicing concordance bundle (Bulk RNA-seq module)\n"
        "=================================================\n"
        f"Drug signatures from : {vals['PSI_EVENTS_DIR'] or psi_root + '/output/AltResults/AlternativeOutput/Events-dPSI_0.1_rawp'}\n"
        f"Cancer atlas         : {atxt}\n"
        f"Results to           : {concord_root}/results/<atlas>/  (concordance.txt + ranked_concordance_summary.txt)\n"
        f"Concordance          : 1=drug mimics subtype, 0=drug reverses it; candidates < {vals['CONC_THRESHOLD']}\n\n"
        "Autonomous mode launches this automatically AFTER AltAnalyze PSI finishes. To run it manually:\n"
        f"  cd {concord_root.rstrip('/')}\n"
        "  chmod +x *.sh\n"
        "  ./run_concordance_pipeline.sh\n"
        "Watch:  bash status.sh   (or tail -f <concord_root>/watchdog.log)\n"
        "Done when <concord_root>/PIPELINE_COMPLETE.txt appears.\n"
    )
    with open(os.path.join(P.concordance_dir, "RUN_CONCORDANCE_ON_CLUSTER.txt"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(txt)


def build_concordance_bundle(P, sel, bam_out_root, concord_cfg, download_job_tag="sra", reporter=NULL, ai_cfg=None):
    """Assemble runtable/concordance/ (filled config.sh + vendored scorer/ranker + launcher + queries.tsv),
    zip it, return a summary. Returns None if the template is missing."""
    if not os.path.isdir(CONCORDANCE_TEMPLATE_DIR):
        print(f"  CONCORDANCE BUNDLE: vendored template missing at {CONCORDANCE_TEMPLATE_DIR} -> skipping")
        return None
    reporter.set_detail("assembling splicing-concordance bundle…")
    if os.path.isdir(P.concordance_dir):
        shutil.rmtree(P.concordance_dir, ignore_errors=True)
    os.makedirs(P.concordance_dir, exist_ok=True)

    # top-level template files (LF), everything except config.sh (generated): scorer, ranker, libs, atlas tsv
    for name in sorted(os.listdir(CONCORDANCE_TEMPLATE_DIR)):
        srcf = os.path.join(CONCORDANCE_TEMPLATE_DIR, name)
        if os.path.isfile(srcf) and name != "config.sh":
            _copy_lf(srcf, os.path.join(P.concordance_dir, name))

    organism = star_deploy.detect_organism(P, sel, (concord_cfg or {}).get("ORGANISM"))
    bo = bam_out_root.rstrip("/")
    download_root = os.path.dirname(bo)              # .../STAR_bams -> the per-cell-line run root
    psi_root = f"{download_root}/psi"
    concord_root = f"{download_root}/concordance"

    vals = _resolve_concordance_cfg(concord_cfg)
    vals["PSI_ROOT"] = psi_root
    vals["PIPELINE_ROOT"] = concord_root
    vals["ORGANISM"] = organism
    vals["SPECIES"] = (concord_cfg or {}).get("SPECIES") or organism_to_species(organism)
    vals["JOB_TAG"] = f"{(download_job_tag or 'sra')}_concordance"
    if not str(vals.get("EXPNAME") or "").strip():
        vals["EXPNAME"] = _cl_slug(sel.get("canonical", "splicing")) if sel else "splicing"

    # resolve the cancer atlas (registry; GUI 'cancer_atlas' override) + ship queries.tsv
    override = (concord_cfg or {}).get("CANCER_ATLAS") or (concord_cfg or {}).get("cancer_atlas")
    atlas_key, atlas_obj = _resolve_atlas(sel, vals["SPECIES"], override, ai_cfg=ai_cfg)
    nqueries = 0
    if atlas_obj:
        nqueries = _write_queries_tsv(os.path.join(P.concordance_dir, "queries.tsv"), atlas_obj, concord_root)
        vals["CANCER_ATLAS"] = atlas_key
        print(f"  CONCORDANCE BUNDLE: atlas '{atlas_key}' -> {nqueries} query/queries "
              f"({', '.join(q.get('name','?') for q in atlas_obj.get('queries', []))})")
    else:
        # ship a queries.tsv with only a comment so the stage no-ops cleanly until an atlas is configured
        with open(os.path.join(P.concordance_dir, "queries.tsv"), "w", encoding="utf-8", newline="\n") as f:
            f.write("# no cancer atlas resolved for this cell line -- set 'cancer atlas' in the GUI "
                    "(or add the line to cancer_atlas_registry.json)\n")
        print(f"  CONCORDANCE BUNDLE: NO cancer atlas for cell line "
              f"{sel.get('canonical') if sel else '?'!r} -> shipping an empty queries.tsv (stage will skip)")

    # strip deploy-only keys before writing config.sh
    for k in _DEPLOY_ONLY:
        vals.pop(k, None)

    vals["ALERT_EMAIL"] = vals.get("ALERT_EMAIL") or cluster_deploy._alert_email()   # cluster-side email
    cluster_deploy.bake_diagnose_model(vals)                                         # optional diagnose-AI model path / cache dir
    template = open(os.path.join(CONCORDANCE_TEMPLATE_DIR, "config.sh"), encoding="utf-8").read()
    with open(os.path.join(P.concordance_dir, "config.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(cluster_deploy.fill_config(template, vals, numeric=CONCORDANCE_NUMERIC,
                                           defaults=CONCORDANCE_CONFIG_DEFAULTS))
    with open(os.path.join(P.concordance_dir, "concordance_launch.sh"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_concordance_launch_sh(psi_root, concord_root, vals["JOB_TAG"]))

    _write_concordance_instructions(P, vals, psi_root, concord_root, atlas_key, nqueries)
    cluster_deploy._zip_dir(P.concordance_dir, P.concordance_bundle_zip)
    print(f"  CONCORDANCE BUNDLE: tag={vals['JOB_TAG']} atlas={atlas_key or '(none)'} "
          f"-> {os.path.basename(P.concordance_bundle_zip)}")
    reporter.set_detail(f"concordance bundle ready (atlas {atlas_key or 'none'})")
    return {"psi_root": psi_root, "concord_root": concord_root, "job_tag": vals["JOB_TAG"],
            "atlas": atlas_key, "n_queries": nqueries, "species": vals["SPECIES"], "organism": organism}


# ---- cluster-side AltAnalyze-deps resolution (the scorer needs export/UI/unique on PYTHONPATH) ---
def _resolve_altanalyze_home(host, port, user, keyfile, password, psi_root, override=""):
    """Find the first AltAnalyze install carrying export.py/UI.py/unique.py: the PSI config's resolved home,
    an override, $psi_root/altanalyze_home, then the lab fallbacks. Returns the path (or "" if none found)."""
    shq = cluster_deploy.shq
    cands = []
    if override:
        cands.append(override)
    # the home the PSI stage actually resolved (best match)
    cands.append("$(sed -n 's/^ALTANALYZE_HOME=\"\\{0,1\\}\\([^\"]*\\)\"\\{0,1\\}/\\1/p' "
                 + shq(psi_root + "/config.sh") + " 2>/dev/null | head -1)")
    cands.append(psi_root.rstrip("/") + "/altanalyze_home")
    cands += list(_ALTANALYZE_FALLBACKS)
    inner = "; ".join(
        f'h={c if c.startswith("$(") else shq(c)}; '
        '[ -n "$h" ] && [ -s "$h/export.py" ] && [ -s "$h/UI.py" ] && [ -s "$h/unique.py" ] '
        '&& { echo "AAHOME:$h"; exit 0; }'
        for c in cands
    )
    cmd = "{ " + inner + "; }; echo AAHOME_NONE"
    try:
        if password:
            out = cluster_deploy._ssh_capture_paramiko(host, port, user, password, keyfile, cmd)
        else:
            out = cluster_deploy._ssh_capture_systemssh(host, port, user, keyfile, cmd)
    except Exception as e:
        print(f"  CONCORDANCE SUBMIT: AltAnalyze-deps probe failed ({e}) -> using default")
        return ""
    for line in (out or "").splitlines():
        if line.startswith("AAHOME:"):
            return line.split(":", 1)[1].strip()
    return ""


def submit_concordance_over_ssh(P, cluster_cfg, secrets, bam_out_root, reporter=NULL, prior_skipped=False):
    """Upload the concordance bundle + resolve the AltAnalyze deps home (for the scorer's PYTHONPATH) and
    submit the auto-chain launcher (waits on PSI). Non-fatal. prior_skipped=True (phase-range START at
    concordance): PSI was skipped, so pre-create <psi_root>/PIPELINE_COMPLETE.txt so concordance runs now."""
    cfg = cluster_cfg or {}
    secrets = secrets or {}
    host = (cfg.get("ssh_host") or "").strip()
    user = (cfg.get("ssh_user") or "").strip()
    port = str(cfg.get("ssh_port") or "22").strip() or "22"
    keyfile = (cfg.get("ssh_key") or "").strip()
    password = secrets.get("ssh_password") or ""
    dl_tag = (cfg.get("JOB_TAG") or "sra").strip() or "sra"
    concord_tag = f"{dl_tag}_concordance"
    bo = bam_out_root.rstrip("/")
    download_root = os.path.dirname(bo)
    psi_root = f"{download_root}/psi"
    concord_root = f"{download_root}/concordance"

    if not host or not user:
        print("  CONCORDANCE SUBMIT: missing SSH host/user -> bundle still downloadable")
        return {"submitted": False, "reason": "missing host/user",
                "diagnosis": cluster_deploy.diagnose_failure("missing host/user")}
    if not os.path.isdir(P.concordance_dir):
        return {"submitted": False, "reason": "no bundle",
                "diagnosis": cluster_deploy.diagnose_failure("", "no bundle")}

    # if no cancer atlas was resolved (empty queries.tsv -> only a comment), don't arm a launcher that would
    # STALL -- leave the (downloadable) bundle and report it.
    qpath = os.path.join(P.concordance_dir, "queries.tsv")
    has_query = False
    try:
        for ln in open(qpath, encoding="utf-8"):
            s = ln.strip()
            if s and not s.startswith("#"):
                has_query = True
                break
    except Exception:
        pass
    if not has_query:
        print("  CONCORDANCE SUBMIT: no cancer atlas for this cell line -> cleanly SKIPPED (concordance is "
              "OPTIONAL; PSI is the final stage and the chain completes there). Add a 'cancer atlas' in the "
              "GUI / cancer_atlas_registry.json to enable it.")
        # A clean, INTENDED skip -- NOT a failure: omit `diagnosis` (so the GUI doesn't flag a fixable error)
        # and set skipped=True so the orchestrator marks the stage done and the run completes cleanly at PSI.
        return {"submitted": False, "skipped": True, "reason": "no cancer atlas for this cell line"}

    # resolve the AltAnalyze-deps home BEFORE upload so config.sh ships with the correct ALTANALYZE_HOME.
    cfg_path = os.path.join(P.concordance_dir, "config.sh")
    override = (_read_cfg_var(cfg_path, "ALTANALYZE_HOME") or "").strip()
    resolved = _resolve_altanalyze_home(host, port, user, keyfile, password, psi_root, override)
    if resolved:
        _set_config_var(cfg_path, "ALTANALYZE_HOME", resolved)
        print(f"  CONCORDANCE SUBMIT: AltAnalyze deps (export/UI/unique) at {resolved}")
    else:
        print("  CONCORDANCE SUBMIT: could not confirm export/UI/unique on the cluster -> leaving the "
              "config default ($PSI_ROOT/altanalyze_home); the stage's setup.sh will flag it if missing.")

    shq = cluster_deploy.shq
    # DETACH the launcher bsub (setsid, backgrounded in a subshell so only the bsub is async) so the deploy
    # ssh returns immediately instead of hanging on "Pending job threshold reached. Retrying in 60s" under a
    # saturated pending-job quota. See star_deploy.submit_star_over_ssh for the full rationale.
    _lo = shq(concord_root + '/launch.out')
    launch = (
        f"( setsid bsub -L /bin/bash -n 1 -M 1000 -W 66480 -J {shq(concord_tag + '_launch')} "
        f"-o {_lo} -e {shq(concord_root + '/launch.err')} "
        f"{shq(concord_root + '/concordance_launch.sh')} "
        f"</dev/null >>{_lo} 2>&1 & )"
    )
    if prior_skipped:
        # PSI was phase-range skipped -> the launcher polls <psi_root>/PIPELINE_COMPLETE.txt which no PSI run
        # will write. Pre-create it so concordance runs on the existing signatures -- but ONLY if PSI never
        # ran here (no marker AND no watchdog.log), else a running/finished PSI is left to finalize itself.
        _pm = psi_root + "/PIPELINE_COMPLETE.txt"; _pl = psi_root + "/watchdog.log"
        launch = (f"if [ ! -f {shq(_pm)} ] && [ ! -f {shq(_pl)} ]; then mkdir -p {shq(psi_root)} && "
                  f"touch {shq(_pm)}; fi; " + launch)
        print(f"  CONCORDANCE SUBMIT: PSI phase-skipped -> pre-touch {psi_root}/PIPELINE_COMPLETE.txt ONLY if no PSI ran there")
    print(f"=== CONCORDANCE SUBMIT: {user}@{host}:{port} -> {concord_root} ===")
    try:
        if password:
            try:
                import paramiko  # noqa: F401
            except Exception:
                raise RuntimeError("password auth needs paramiko (pip install paramiko) or use an SSH key")
            res = cluster_deploy._submit_paramiko(P, host, port, user, password, keyfile, concord_root,
                                                  reporter, src_dir=P.concordance_dir, launch_cmd=launch)
        else:
            res = cluster_deploy._submit_systemssh(P, host, port, user, keyfile, concord_root,
                                                   reporter, src_dir=P.concordance_dir, launch_cmd=launch)
        print("  CONCORDANCE SUBMIT: self-rescheduling launcher armed on " + host + " — concordance starts on "
              "the cluster when AltAnalyze PSI finishes (safe to close SpliceScout now)")
        res = dict(res or {}); res["altanalyze_home"] = resolved
        return res
    except Exception as e:
        output = getattr(e, "output", "") or str(e)
        diag = cluster_deploy.diagnose_failure(output, str(e))
        print(f"  CONCORDANCE SUBMIT FAILED: {e}  -> {diag['title']}")
        print("  -> the concordance bundle is still downloadable (RUN_CONCORDANCE_ON_CLUSTER.txt).")
        reporter.set_detail(f"concordance submit failed: {diag['title']}")
        return {"submitted": False, "reason": str(e), "diagnosis": diag}
