# -*- coding: utf-8 -*-
"""
The SpliceScout Assistant — an agentic chat brain for the web UI.

A plain-English helper that, through tool-calling, fills/controls every pipeline setting, writes & tests
the NCBI GEO query from a description, answers "what does X do / what happened", runs SQL over a run's
metadata tables, builds Plotly charts, and retrieves local + cluster logs. PREPARE-ONLY: it never
launches, kills, or deploys anything — it sets up the form and the user presses Start; cluster access is
read-only. It uses whatever LLM provider/model/key the user configured in the UI (via llm_providers.chat).

Public entry point: `run_turn(messages, settings, save_settings=None, instance_tag="") -> dict` returning
  {"reply", "trace":[{tool,summary}], "settings_changed":{...}, "charts":[fig...], "usage":{...}}.
The browser holds the visible conversation (list of {role:"user"|"assistant","content"}) and sends it
each turn; tool rounds happen inside one run_turn and are summarized back as `trace`.
"""
import asyncio
import json
import os
import re
import urllib.parse
import urllib.request

import llm_providers
import stage_docs
import plot_data
import run_data
import chart_engine
from pipeline_paths import Paths

_ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(_ROOT, "runs")
MAX_ROUNDS = 8                 # cap model<->tool rounds per turn so a loop can't run away
MAX_TOKENS = 4096
_REDACT = ("api_keys", "api_key", "ncbi_key", "ssh_password")
_SQL_BLOCK = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|"
                        r"vacuum|reindex|begin|commit|truncate)\b", re.I)


# ----------------------------- run discovery / local reads -----------------------------
def _run_dir(run_id):
    """Resolve a run id to its directory (basename only — no path traversal), or None."""
    if not run_id:
        return None
    name = os.path.basename(str(run_id).strip())
    d = os.path.join(RUNS_DIR, name)
    return d if os.path.isdir(d) else None


def _list_runs():
    out = []
    try:
        names = [n for n in os.listdir(RUNS_DIR) if os.path.isdir(os.path.join(RUNS_DIR, n))]
    except OSError:
        return out
    for n in names:
        d = os.path.join(RUNS_DIR, n)
        rec = {"run_id": n}
        try:
            cfg = json.load(open(os.path.join(d, "config.json"), encoding="utf-8"))
            rec["query"] = cfg.get("query", "")
            rec["module"] = cfg.get("module", "")
        except Exception:
            pass
        try:
            pr = json.load(open(os.path.join(d, "progress.json"), encoding="utf-8"))
            rec["state"] = pr.get("state", "")
            rec["current"] = _current_stage(pr)
        except Exception:
            pass
        rec["_mtime"] = os.path.getmtime(d)
        out.append(rec)
    out.sort(key=lambda r: r.get("_mtime", 0), reverse=True)
    for r in out:
        r.pop("_mtime", None)
    return out


def _current_stage(pr):
    """progress.json's `current` is the active STAGE KEY (a string) with its label in `current_label` --
    not a dict. (Reading it as {name:...} raised AttributeError, so list_runs/get_run_status returned no
    stage info at all.) Tolerates an older dict form too."""
    cur = pr.get("current")
    if isinstance(cur, dict):
        return cur.get("label") or cur.get("key") or cur.get("name") or ""
    return pr.get("current_label") or (cur or "")


def _run_status(run_id):
    d = _run_dir(run_id)
    if not d:
        return {"error": f"no run named {run_id!r} (use list_runs)"}
    out = {"run_id": os.path.basename(d)}
    try:
        pr = json.load(open(os.path.join(d, "progress.json"), encoding="utf-8"))
        out["state"] = pr.get("state", "")
        out["error"] = pr.get("error", "")
        out["current"] = _current_stage(pr)
        # snapshot stages carry key/label (not name) -- see progress.RunReporter.snapshot
        out["stages"] = [{"name": s.get("label") or s.get("key") or s.get("name"), "key": s.get("key"),
                          "status": s.get("status"), "done": s.get("done"), "total": s.get("total"),
                          "detail": s.get("detail")}
                         for s in (pr.get("stages") or [])]
        out["log_tail"] = [e.get("text", "") for e in (pr.get("log") or [])[-12:]]
    except Exception as e:
        out["progress_error"] = str(e)
    try:
        cfg = json.load(open(os.path.join(d, "config.json"), encoding="utf-8"))
        out["query"] = cfg.get("query", "")
        out["module"] = cfg.get("module", "")
        out["cluster_mode"] = cfg.get("cluster_mode", "")
    except Exception:
        pass
    return out


def _read_local(run_id, target, max_bytes=12000):
    d = _run_dir(run_id)
    if not d:
        return f"no run named {run_id!r} (use list_runs)"
    alias = {"progress": "progress.json", "config": "config.json", "state": "pipeline_state.json"}
    rel = alias.get((target or "").strip(), (target or "").strip().lstrip("/"))
    if not rel or ".." in rel or not re.fullmatch(r"[A-Za-z0-9._/ -]+", rel):
        return ("invalid target — give a file relative to the run dir, e.g. progress.json, config.json, "
                "ncbi_raw.json, ai_work/sample_index.json")
    path = os.path.join(d, rel)
    if not os.path.isfile(path):
        return f"{rel} not found in this run yet"
    try:
        with open(path, "rb") as f:
            blob = f.read(max_bytes + 1)
        text = blob[:max_bytes].decode("utf-8", "replace")
        if rel == "config.json":                              # never surface secrets
            try:
                obj = json.loads(text)
                for k in ("ncbi_key", "api_key", "api_keys"):
                    obj.pop(k, None)
                if isinstance(obj.get("cluster_cfg"), dict):
                    obj["cluster_cfg"].pop("ssh_password", None)
                text = json.dumps(obj, indent=1)
            except Exception:
                pass
        if len(blob) > max_bytes:
            text += f"\n…(truncated at {max_bytes} bytes)"
        return text
    except OSError as e:
        return f"read error: {e}"


# ----------------------------- GEO query test -----------------------------
def _geo_count(query, ncbi_key=""):
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=gds&retmode=json&retmax=0"
           f"&term={urllib.parse.quote(query or '')}" + (f"&api_key={ncbi_key}" if ncbi_key else ""))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode())
    return int(data["esearchresult"]["count"])


# ----------------------------- SQL over ALL the run's data -----------------------------
def _guard_select(q):
    """Read-only guard shared by run_sql and make_chart's sql= path."""
    if not re.match(r"(?is)^\s*(select|with)\b", q):
        return "ERROR: only SELECT / WITH (read-only) queries are allowed."
    if _SQL_BLOCK.search(q):
        return "ERROR: write/DDL statements are not allowed — this is read-only."
    return None


def _run_sql(run_id, sql):
    d = _run_dir(run_id)
    if not d:
        return f"no run named {run_id!r} (use list_runs)"
    q = (sql or "").strip().rstrip(";")
    err = _guard_select(q)
    if err:
        return err
    res = run_data.query(d, q, max_rows=200)
    if res["error"]:
        return (f"{res['error']}\nTables available: {', '.join(res['tables'])}\n(CSV-derived columns are "
                "TEXT — cast with CAST(x AS INTEGER); studies/samples/pipeline_stages/data_funnel have "
                "typed numeric columns. Call list_data to see all tables + columns.)")
    rows = res["rows"]
    lines = ["tables: " + ", ".join(res["tables"]), "columns: " + ", ".join(res["cols"]),
             f"rows: {len(rows)}" + (" (capped at 200)" if len(rows) >= 200 else "")]
    lines.append(json.dumps(rows[:60], ensure_ascii=False))
    return "\n".join(lines)


def _list_data(run_id):
    d = _run_dir(run_id)
    if not d:
        return f"no run named {run_id!r} (use list_runs)"
    return json.dumps(run_data.inventory(d), ensure_ascii=False)


# ----------------------------- universal Plotly chart over ANY run data -----------------------------
def _funnel_full(run_id, include_cluster, ctx):
    """The data-volume funnel rows (local), optionally extended with cluster BAM/BED/PSI counts."""
    d = _run_dir(run_id)
    rows = run_data.funnel_rows(d) if d else []
    if include_cluster and ctx:
        try:
            rows = rows + _funnel_cluster(ctx.get("settings") or {}, ctx.get("instance_tag", ""))
        except Exception:
            pass
    return rows


def _funnel_cluster(settings, instance_tag):
    """Optional: count aligned BAMs / junction BEDs / PSI outputs on the cluster (read-only SSH)."""
    import cluster_deploy
    sc = settings.get("cluster") or {}
    host = (sc.get("ssh_host") or "").strip()
    user = (sc.get("ssh_user") or "").strip()
    if not host or not user:
        return []
    port = str(sc.get("ssh_port") or "22").strip() or "22"
    keyfile = (sc.get("ssh_key") or "").strip()
    password = (settings.get("ssh_password") or "").strip()
    tag = re.sub(r"[^A-Za-z0-9_]", "", str(instance_tag or sc.get("JOB_TAG") or ""))
    try:
        st = cluster_deploy.remote_status(host, user, port, keyfile, password, tag,
                                          (sc.get("PIPELINE_ROOT") or "").strip())
        root = (st.get("root") if isinstance(st, dict) else "") or (sc.get("PIPELINE_ROOT") or "").strip()
    except Exception:
        return []
    if not root:
        return []
    root = root.rstrip("/")
    cmd = (f"b=$(find {root} -name '*.bam' 2>/dev/null | wc -l); "
           f"j=$(find {root} -name '*__junction.bed*' 2>/dev/null | wc -l); "
           f"p=$(find {root} -path '*psi*' -name '*.bed' 2>/dev/null | wc -l); echo \"$b $j $p\"")
    out = (cluster_deploy._ssh_capture_paramiko(host, port, user, password, keyfile, cmd, timeout=60)
           if password else
           cluster_deploy._ssh_capture_systemssh(host, port, user, keyfile, cmd, timeout=60))
    nums = (out or "").strip().split()
    if len(nums) < 3:
        return []
    try:
        b, j, p = int(nums[0]), int(nums[1]), int(nums[2])
    except ValueError:
        return []
    return [{"step_order": 900 + i, "phase": "process", "step": s, "label": lab, "n": v, "unit": "samples"}
            for i, (s, lab, v) in enumerate([("bams", "BAMs aligned (cluster)", b),
                                             ("beds", "Junction BEDs (cluster)", j),
                                             ("psi", "PSI BEDs (cluster)", p)], start=1)]


def _data_funnel(run_id, include_cluster, ctx):
    rows = _funnel_full(run_id, include_cluster, ctx)
    if not rows:
        return f"no funnel data for {run_id!r} yet (needs ncbi_raw.json; use list_runs)"
    return json.dumps({"funnel": rows, "hint": "chart it with make_chart source='funnel' "
                       "(type 'funnel', 'bar', or 'waterfall')."}, ensure_ascii=False)


def _make_chart(run_id, args, ctx):
    """Build a Plotly figure from a SQL query, a named table, the funnel, or the cell-line sample table."""
    d = _run_dir(run_id)
    if not d:
        return None, f"no run named {run_id!r} (use list_runs)"
    spec = {k: args.get(k) for k in ("type", "x", "y", "color", "agg", "title",
                                     "orientation", "sort", "stack", "colorscale")}
    sql = (args.get("sql") or "").strip().rstrip(";")
    source = (args.get("source") or "").strip()
    if sql:
        err = _guard_select(sql)
        if err:
            return None, err
        res = run_data.query(d, sql, max_rows=5000)
        if res["error"]:
            return None, f"{res['error']} | tables: {', '.join(res['tables'])} (call list_data for columns)"
        rows = res["rows"]
    elif source == "funnel":
        rows = _funnel_full(run_id, args.get("include_cluster"), ctx)
        spec["type"] = spec.get("type") or "funnel"
        spec["x"] = spec.get("x") or "label"
        spec["y"] = spec.get("y") or "n"
    elif source == "cellline":
        data = plot_data.build_plot_data(Paths(d))
        if not data.get("available"):
            return None, ("the cell-line sample table isn't ready yet (it exists after the cellline_match "
                          "stage). Meanwhile chart 'samples'/'studies' with source= or sql=.")
        rows = data.get("samples", [])
    elif source:
        safe = re.sub(r"[^A-Za-z0-9_]", "", source)
        res = run_data.query(d, f'SELECT * FROM "{safe}" LIMIT 5000', max_rows=5000)
        if res["error"]:
            return None, f"no table {source!r}. Available: {', '.join(res['tables'])} (or pass sql=)."
        rows = res["rows"]
    else:
        return None, ("tell me what to chart: pass sql=<SELECT…> OR source=<table|funnel|cellline>. "
                      "Call list_data first to see the tables and columns.")
    if not rows:
        return None, "no rows to chart (the query/source returned nothing)."
    fig, err = chart_engine.build_figure(rows, spec)
    if not fig:
        return None, err or "couldn't build that chart."
    if ctx is not None:
        ctx["charts"].append(fig)
    return fig, f"chart ready ({len(rows)} rows; type {fig['data'][0].get('type')})."


# ----------------------------- cluster log retrieval (read-only SSH) -----------------------------
_SAFE_REL = re.compile(r"[A-Za-z0-9._/ -]+")


def _cluster_log(settings, target, lines, instance_tag):
    import cluster_deploy
    sc = settings.get("cluster") or {}
    host = (sc.get("ssh_host") or "").strip()
    user = (sc.get("ssh_user") or "").strip()
    if not host or not user:
        return "No cluster SSH is configured (set the cluster ssh_host / ssh_user in settings first)."
    port = str(sc.get("ssh_port") or "22").strip() or "22"
    keyfile = (sc.get("ssh_key") or "").strip()
    password = (settings.get("ssh_password") or "").strip()
    tag = re.sub(r"[^A-Za-z0-9_]", "", str(instance_tag or sc.get("JOB_TAG") or "")) or ""
    try:
        lines = max(1, min(int(lines or 200), 1000))
    except Exception:
        lines = 200

    def _run(cmd):
        if password:
            return cluster_deploy._ssh_capture_paramiko(host, port, user, password, keyfile, cmd, timeout=45)
        return cluster_deploy._ssh_capture_systemssh(host, port, user, keyfile, cmd, timeout=45)

    t = (target or "").strip()
    if t in ("bjobs", "jobs", "status"):
        if not tag:
            return "No job tag known — set the cluster JOB_TAG or pass the run's instance tag."
        cmd = f"bjobs -w 2>/dev/null | grep -i '{tag}' | head -80 || echo '(no matching jobs)'"
        try:
            return _run(cmd) or "(no output)"
        except Exception as e:
            return f"cluster query failed: {e}"
    # otherwise treat target as a path RELATIVE to the discovered pipeline root (read-only tail)
    rel = t.lstrip("/")
    if not rel or ".." in rel or not _SAFE_REL.fullmatch(rel):
        return ("Give a log path relative to the cluster pipeline root (e.g. watchdog.log, "
                "psi/watchdog.log, psi/logs/psi_job.err, PIPELINE_COMPLETE.txt) or target='bjobs'.")
    try:
        st = cluster_deploy.remote_status(host, user, port, keyfile, password, tag,
                                          (sc.get("PIPELINE_ROOT") or "").strip())
        root = (st.get("root") if isinstance(st, dict) else "") or (sc.get("PIPELINE_ROOT") or "").strip()
    except Exception as e:
        return f"couldn't reach the cluster: {e}"
    if not root:
        return "Couldn't discover the cluster pipeline root (no live jobs and no PIPELINE_ROOT set)."
    cmd = (f"f=$(ls -1t {root.rstrip('/')}/{rel} 2>/dev/null | head -1); "
           f"[ -n \"$f\" ] && tail -n {lines} \"$f\" 2>&1 | head -c 24000 || echo 'not found: {rel}'")
    try:
        return _run(cmd) or "(empty)"
    except Exception as e:
        return f"cluster read failed: {e}"


# ----------------------------- cluster health / stall alert (read-only SSH) -----------------------------
def _cluster_health(settings):
    """Scan the whole cluster pipeline root for ANY stage that STALLED/ORPHANED/hit a LAUNCH_TIMEOUT --
    the 'is anything silently stuck?' check. Read-only."""
    import cluster_deploy
    sc = settings.get("cluster") or {}
    host = (sc.get("ssh_host") or "").strip()
    user = (sc.get("ssh_user") or "").strip()
    if not host or not user:
        return "No cluster SSH is configured (set the cluster ssh_host / ssh_user in settings first)."
    port = str(sc.get("ssh_port") or "22").strip() or "22"
    keyfile = (sc.get("ssh_key") or "").strip()
    password = (settings.get("ssh_password") or "").strip()
    root = (sc.get("PIPELINE_ROOT") or "").strip()
    try:
        r = cluster_deploy.remote_alerts(host, user, port, keyfile, password, root)
    except Exception as e:
        return f"couldn't reach the cluster: {e}"
    if not r.get("ok"):
        return "cluster health check failed: " + str(r.get("error") or "unknown")
    alerts = r.get("alerts") or []
    if not alerts:
        return json.dumps({"healthy": True, "root": r.get("root"), "alerts": [],
                           "note": "No STALLED/ORPHANED/LAUNCH_TIMEOUT markers under the pipeline root — "
                                   "nothing is silently stuck."}, ensure_ascii=False)
    return json.dumps({"healthy": False, "root": r.get("root"), "alert_count": len(alerts),
                       "alerts": alerts, "note": "STAGE FAILURES — these stages are STALLED/failed and "
                       "will NOT auto-advance. Fix the cause (or drop the stuck items) and re-arm that "
                       "stage's watchdog; a stalled BED also needs psi_launch re-armed."}, ensure_ascii=False)


# ----------------------------- settings helpers -----------------------------
def _redact_settings(settings):
    s = {k: v for k, v in (settings or {}).items() if k not in _REDACT}
    keys = settings.get("api_keys") or {}
    s["providers_with_key"] = sorted([p for p, v in keys.items() if v]) + (
        ["ncbi"] if settings.get("ncbi_key") else [])
    if isinstance(s.get("cluster"), dict):
        s["cluster"] = {k: v for k, v in s["cluster"].items() if k != "ssh_password"}
    return s


# ============================ tools ============================
TOOLS = [
    {"name": "get_settings", "description": "Read the current pipeline settings the user has configured "
     "(API keys, passwords and the NCBI key are redacted; a 'providers_with_key' list shows which "
     "providers already have a key).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "update_settings", "description": "Fill/overwrite pipeline settings (same store as the form). "
     "Pass a partial patch — only the keys you set change. Top-level keys: query, scope, cap (int or "
     "'unlimited'), provider, model, ncbi_key, concurrency (int), skip_ai (bool), disable_reasoning "
     "(bool), module ('bulk_rna_seq'), pick_mode ('auto'|'manual'), cluster_mode ('off'|'manual'|"
     "'autonomous'); nested objects: cluster {ssh_host,ssh_user,ssh_port,ssh_key,PIPELINE_ROOT,JOB_TAG}, "
     "star{}, bed{}, psi{}, groups{}. NEVER guess cluster hosts/paths or keys — ask the user.",
     "input_schema": {"type": "object", "properties": {"patch": {"type": "object",
      "description": "settings keys to set"}}, "required": ["patch"]}},
    {"name": "test_geo_query", "description": "Run an NCBI GEO (gds) esearch for a query string and return "
     "the number of matching studies, so you can validate/refine a query before saving it.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                      "required": ["query"]}},
    {"name": "list_runs", "description": "List existing pipeline runs (run_id, query, module, state).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_run_status", "description": "Summarize a run's progress.json (state, per-stage status, "
     "current stage, error, recent log lines) and key config.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}},
                      "required": ["run_id"]}},
    {"name": "read_local_log", "description": "Read a bounded slice of a local run artifact. target is a "
     "file relative to the run dir, e.g. progress.json, config.json, ncbi_raw.json, "
     "ai_work/sample_index.json. (config.json is auto-redacted.)",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"},
      "target": {"type": "string"}}, "required": ["run_id", "target"]}},
    {"name": "list_data", "description": "List everything queryable/chartable in a run: every table with "
     "its columns and row count, plus the data-volume funnel steps. Call this BEFORE run_sql/make_chart so "
     "you use the exact table and column names that exist in this run.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}},
                      "required": ["run_id"]}},
    {"name": "run_sql", "description": "Run a read-only SQL SELECT/WITH over ALL of a run's data (in-memory "
     "SQLite). Tables: studies (GEO studies — assay, organism, gdstype, n_geo_samples, year, bioproject), "
     "study_protocol (strategy, selection, instrument), samples (extracted samples — cell_line, source, "
     "spots, n_compounds, compounds), pipeline_stages (status/done/total), data_funnel "
     "(step_order,phase,label,n,unit), plus every tables/*.csv and runtable/*.csv (table = file name w/o "
     ".csv). JSON-derived numeric columns are typed; CSV columns are TEXT (CAST for math). Use list_data "
     "for the exact tables+columns. Answers any counting/grouping/filtering question.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"},
      "sql": {"type": "string"}}, "required": ["run_id", "sql"]}},
    {"name": "make_chart", "description": "Render a Plotly chart from ANY of the run's data. Give ONE data "
     "source: sql=<a SELECT over the run tables> (preferred — shape the data in SQL, then chart its "
     "columns) OR source=<table name | 'funnel' | 'cellline'>. Pick type (bar, hbar, line, area, scatter, "
     "histogram, box, violin, pie, funnel, waterfall, heatmap) and columns x / y / color. agg "
     "(count|sum|mean|min|max) groups rows by x when not pre-aggregated (skip it if you GROUP BY in SQL). "
     "source='funnel' draws the data-volume funnel (studies/samples surviving each step); add "
     "include_cluster=true to also count cluster BAM/BED/PSI. Call list_data first for field names.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"},
      "sql": {"type": "string"}, "source": {"type": "string"}, "x": {"type": "string"},
      "y": {"type": "string"}, "color": {"type": "string"}, "type": {"type": "string"},
      "agg": {"type": "string"}, "title": {"type": "string"}, "orientation": {"type": "string"},
      "sort": {"type": "string"}, "include_cluster": {"type": "boolean"}}, "required": ["run_id"]}},
    {"name": "data_funnel", "description": "Get the data-volume funnel as NUMBERS: an ordered list of "
     "pipeline steps with how many studies/samples survive each (GEO studies found → sequencing → "
     "extracted → samples extracted → AI-cleaned → picked cell line → [cluster BAM/BED/PSI]). Use to "
     "explain 'how much data after each step' in words; chart it with make_chart source='funnel'. "
     "include_cluster=true adds cluster processing counts (slower, read-only SSH).",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"},
      "include_cluster": {"type": "boolean"}}, "required": ["run_id"]}},
    {"name": "fetch_cluster_log", "description": "Read-only retrieve a cluster log over SSH. target='bjobs' "
     "for live job status, or a log path relative to the pipeline root: watchdog.log, psi/watchdog.log, "
     "psi/logs/psi_job.err, psi/logs/psi_job.out, psi/output/AltAnalyze_report-*.log, PIPELINE_*.txt. "
     "lines = how many trailing lines (default 200).",
     "input_schema": {"type": "object", "properties": {"target": {"type": "string"},
      "lines": {"type": "integer"}}, "required": ["target"]}},
    {"name": "cluster_health", "description": "Scan the ENTIRE cluster pipeline root for any stage that "
     "STALLED, ORPHANED, or hit a LAUNCH_TIMEOUT (download/STAR/BED/PSI, across all runs). Call this "
     "whenever the user asks how a run is going or whether anything is stuck/failed — a STALLED stage does "
     "NOT auto-advance, so this is how a silent stall gets surfaced. Returns healthy=true (nothing stuck) "
     "or the list of failed stages with their paths + timestamps.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "explain", "description": "Get the detailed doc for a pipeline stage (title/what/inputs/"
     "outputs). topic = a stage key (fetch, extract, prep, ai_compounds, ai_samples, merge, build, "
     "select, cellline_match, cluster_submit, star_submit, bed_submit, psi_submit, …) or 'stages' for "
     "the full list.",
     "input_schema": {"type": "object", "properties": {"topic": {"type": "string"}},
                      "required": ["topic"]}},
    {"name": "skipped_studies", "description": "List studies in a run that were fetched from GEO but "
     "yielded ZERO downloadable samples because they have no SRA sequencing runs (microarray, "
     "processed-only, or computational re-analysis studies). Use this to explain why a GEO study/sample "
     "the user can see (a GSE/GSM) shows 0 samples here — it has no raw data to download. Returns counts "
     "+ the biggest skipped studies with their assay type.",
     "input_schema": {"type": "object", "properties": {"run_id": {"type": "string"}},
                      "required": ["run_id"]}},
]


def _explain(topic):
    t = (topic or "").strip()
    docs = stage_docs.STAGE_DOCS
    if t in ("stages", "all", ""):
        return json.dumps({k: v.get("title", "") for k, v in docs.items()}, ensure_ascii=False)
    if t in docs:
        return json.dumps(docs[t], ensure_ascii=False)
    hits = {k: v for k, v in docs.items() if t.lower() in k.lower()
            or t.lower() in (v.get("title", "").lower())}
    return json.dumps(hits or {"note": f"no stage matching {t!r}; try 'stages' for the list"},
                      ensure_ascii=False)


def _skipped_studies(run_id):
    d = _run_dir(run_id)
    if not d:
        return f"no run named {run_id!r} (use list_runs)"
    try:
        import build_final
        rows = build_final.skipped_no_sra(Paths(d))
    except Exception as e:
        return f"couldn't compute skipped studies: {e}"
    if not rows:
        return ("No studies were skipped for lack of SRA data in this run (or extraction hasn't produced "
                "structured_samples yet).")
    total = sum(r["n_geo_samples"] for r in rows)
    head = [{"gse": r["gse"], "geo_samples": r["n_geo_samples"], "assay": r["gdstype"],
             "title": (r["title"] or "")[:80]} for r in rows[:20]]
    return json.dumps({
        "skipped_study_count": len(rows), "geo_samples_skipped": total,
        "why": "these studies extracted 0 downloadable samples. 'Expression profiling by array' studies "
               "genuinely have no SRA sequencing data. 'high throughput sequencing' studies showing 0 may "
               "have been missed by an OLDER extractor whose GEO→SRA elink was broken (fixed 2026-06-15: it "
               "now also resolves the study's SRA-study/BioProject) — RE-RUN the extract stage to recover "
               "any that actually have SRA. Use the assay type to tell which is which.",
        "biggest": head}, ensure_ascii=False)


def _exec_tool(name, args, ctx):
    """Run one tool; return a STRING result. Side effects (settings/charts) recorded on ctx."""
    args = args or {}
    try:
        if name == "get_settings":
            return json.dumps(_redact_settings(ctx["settings"]), ensure_ascii=False)
        if name == "update_settings":
            patch = args.get("patch") or {}
            if not isinstance(patch, dict) or not patch:
                return "patch must be a non-empty object of settings to set"
            ctx["settings"].update(patch)                      # reflect for later get_settings this turn
            if ctx.get("save_settings"):
                ctx["save_settings"](patch)
            ctx["settings_changed"].update(patch)
            return "updated: " + ", ".join(sorted(patch.keys()))
        if name == "test_geo_query":
            try:
                n = _geo_count(args.get("query", ""), ctx["settings"].get("ncbi_key") or "")
                return f"{n} GEO studies match that query."
            except Exception as e:
                return f"couldn't run esearch: {e}"
        if name == "list_runs":
            return json.dumps(_list_runs(), ensure_ascii=False)
        if name == "get_run_status":
            return json.dumps(_run_status(args.get("run_id")), ensure_ascii=False)
        if name == "read_local_log":
            return _read_local(args.get("run_id"), args.get("target"))
        if name == "list_data":
            return _list_data(args.get("run_id"))
        if name == "run_sql":
            return _run_sql(args.get("run_id"), args.get("sql"))
        if name == "make_chart":
            _, note = _make_chart(args.get("run_id"), args, ctx)
            return note
        if name == "data_funnel":
            return _data_funnel(args.get("run_id"), args.get("include_cluster"), ctx)
        if name == "fetch_cluster_log":
            return _cluster_log(ctx["settings"], args.get("target"), args.get("lines"),
                                ctx.get("instance_tag", ""))
        if name == "cluster_health":
            return _cluster_health(ctx["settings"])
        if name == "explain":
            return _explain(args.get("topic"))
        if name == "skipped_studies":
            return _skipped_studies(args.get("run_id"))
        return f"unknown tool {name!r}"
    except Exception as e:
        return f"tool {name} errored: {e}"


# ============================ system prompt ============================
def build_system_prompt(settings):
    stages = "\n".join(f"  - {k}: {v.get('title','')} — {(v.get('what','') or '').split('. ')[0]}."
                       for k, v in stage_docs.STAGE_DOCS.items())
    prov = llm_providers.normalize_provider(settings.get("provider"))
    return ("""You are the SpliceScout Assistant, a friendly guide for non-technical users of SpliceScout —
a pipeline that searches NCBI GEO for RNA-seq studies, extracts + AI-cleans the sample metadata, picks a
cell line, and (on an LSF cluster) downloads → STAR-aligns → makes junction/intron BED → runs AltAnalyze
to get splicing (PSI / dPSI) results.

YOUR JOB
- Fill in and control ALL the run settings for the user using the update_settings tool, based on what
  they tell you they want to study.
- Turn a plain-English description into a valid NCBI Entrez GEO query, and validate it with
  test_geo_query (report the hit count) before saving it.
- Answer questions about what the program does (use the stage list below / the explain tool) and what
  happened in a run (use get_run_status, read_local_log, fetch_cluster_log). Whenever the user asks how a
  run is going, whether it's stuck, or why there's no output, CALL cluster_health — a stage can STALL and
  it will NOT auto-advance to the next, so a silent stall is the #1 reason a run quietly stops; surface it.
- If the user asks why a study/sample they can see in GEO shows 0 samples or "isn't caught", use
  skipped_studies: MANY GEO studies are microarray or processed-only / re-analysis with NO SRA sequencing
  runs, so this download→splice pipeline has nothing to fetch for them — that's expected, not lost data.
  Name the assay type / reason.
- Answer data questions and draw ANY chart over ALL of the run's data — the user never has to touch the
  manual Plots tab. The whole run is queryable as SQL (run_sql) and chartable (make_chart); call
  list_data FIRST to see the exact tables + columns. Key tables: studies (GEO studies — assay, organism,
  n_geo_samples, year, gdstype, bioproject), study_protocol (strategy/selection/instrument), samples
  (every extracted sample — cell_line, source, spots, n_compounds, compounds), pipeline_stages, the
  synthesized data_funnel, plus any tables/* and runtable/* CSVs. The clean way to chart is to GROUP BY
  in make_chart's sql=, then chart the resulting columns (type = bar/hbar/line/scatter/pie/box/funnel/
  waterfall/heatmap). For "how much data after each step / does it rise then fall as it filters", use the
  data_funnel tool (numbers) or make_chart source='funnel' (visual; include_cluster=true also counts the
  cluster BAM/BED/PSI outputs).
- When a setting depends on what the USER wants and you cannot reasonably infer it, ASK a short, concrete
  question instead of guessing. NEVER invent cluster hostnames/paths, API keys, or SSH credentials —
  always ask the user for those.

IMPORTANT — you PREPARE, you do not RUN. You can set every field and test the query, but you cannot start,
resume, kill, or deploy anything. After you've set things up, tell the user to review the form and press
the Start button. Cluster access is read-only (logs/status only).

NCBI GEO QUERY FORMAT (Entrez, db=gds): terms ANDed with optional field tags, e.g.
  rna-seq[Description] AND "Homo sapiens"[Organism] AND drug
  "single cell"[Description] AND mouse[Organism] AND (knockout OR CRISPR)
Common tags: [Description], [Organism], [Title]. Free text without a tag is searched broadly. Keep it
specific enough to be useful but not so narrow it returns 0 — use test_geo_query to tune it.

KEY SETTINGS YOU CONTROL (update_settings): query; cap (max studies, int or 'unlimited'); provider +
model + (the API key is set elsewhere by the user — if a provider has no key, ask them to paste one in
the form); ncbi_key (speeds up fetching — optional); concurrency; skip_ai (run without AI cleaning);
module ('bulk_rna_seq' for the splicing pipeline); pick_mode ('auto' picks the top cell line, 'manual'
lets the user choose); cluster_mode ('off' = local only, 'manual' = build the cluster bundle to run by
hand, 'autonomous' = upload + self-drive on the cluster) and the cluster{} SSH details (ASK for these).

PIPELINE STAGES (for "what does X do"):
__STAGES__

STYLE: concise, warm, non-jargon. Put SQL in ```sql fences and JSON in ```json fences. The current
provider is __PROV__. Confirm what you changed.""".replace("__STAGES__", stages).replace("__PROV__", prov).strip())


# ============================ the turn loop ============================
async def _run(messages, settings, save_settings, instance_tag):
    provider = llm_providers.normalize_provider(settings.get("provider"))
    model = (settings.get("model") or "").strip() or llm_providers.DEFAULT_MODEL[provider]
    keys = settings.get("api_keys") or {}
    # .strip() the key: the launch path strips it (server.py) but the assistant did NOT, so a key pasted with
    # a stray leading/trailing space or TAB made the gateway reject ONLY the assistant (the confusing
    # "No connected db" / auth failure) while the pipeline agents worked — the actual agents-vs-assistant gap.
    key = (keys.get(provider) or settings.get("api_key") or "").strip()
    if key:
        os.environ[llm_providers.KEY_ENV[provider]] = key
    if provider != "ollama" and not llm_providers.have_key(provider):
        return {"reply": f"I need an API key for {llm_providers.PROVIDER_LABEL[provider]} to chat. Paste "
                "one into the AI‑provider box in the form (or pick a different provider), then ask me "
                "again.", "trace": [], "settings_changed": {}, "charts": [], "usage": {}, "error": "no_key"}
    base_url = (settings.get("base_urls") or {}).get(provider) or settings.get("base_url") or ""
    mt = (settings.get("model_max_tokens") or {}).get(model)          # per-model cap set in the UI
    try:
        max_tokens = max(256, int(mt)) if str(mt or "").strip() else MAX_TOKENS
    except Exception:
        max_tokens = MAX_TOKENS
    system = build_system_prompt(settings)
    ctx = {"settings": dict(settings), "save_settings": save_settings, "settings_changed": {},
           "charts": [], "instance_tag": instance_tag}
    work = [dict(m) for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    trace, reply, last_usage = [], "", {}
    client = llm_providers.make_client(provider, base_url=base_url)
    try:
        for _ in range(MAX_ROUNDS):
            r = await llm_providers.chat(client, provider, model, work, tools=TOOLS, system=system,
                                         max_tokens=max_tokens,
                                         disable_reasoning=bool(settings.get("disable_reasoning")))
            last_usage = r.get("usage", {}) or last_usage
            tcs = r.get("tool_calls") or []
            if not tcs:
                reply = r.get("text", "") or ""
                break
            work.append({"role": "assistant", "content": r.get("text", ""), "tool_calls": tcs})
            for tc in tcs:
                res = _exec_tool(tc["name"], tc.get("input") or {}, ctx)
                trace.append({"tool": tc["name"], "args": tc.get("input") or {},
                              "summary": (res[:160] + "…") if isinstance(res, str) and len(res) > 160 else res})
                work.append({"role": "tool", "tool_call_id": tc["id"], "content": str(res)})
        else:
            reply = reply or "(stopped after several tool steps — ask me to continue or narrow the request.)"
    finally:
        await llm_providers.close_client(client)
    return {"reply": reply, "trace": trace, "settings_changed": ctx["settings_changed"],
            "charts": ctx["charts"], "usage": last_usage}


def run_turn(messages, settings, save_settings=None, instance_tag=""):
    """Synchronous entry point for the server thread. Drives one agentic turn (model<->tools) and returns
    {reply, trace, settings_changed, charts, usage}."""
    try:
        return asyncio.run(_run(messages or [], settings or {}, save_settings, instance_tag))
    except Exception as e:
        diag = llm_providers.classify_ai_error(e)
        return {"reply": f"Sorry — {diag['title']}. {diag['detail']}", "trace": [],
                "settings_changed": {}, "charts": [], "usage": {}, "error": diag["category"]}
