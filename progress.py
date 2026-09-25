"""
Run progress reporting — shared between the pipeline stages and the web front end.

A `RunReporter` is created per pipeline run. The orchestrator (pipeline.run_pipeline)
drives stage transitions; individual stages call `set_total` / `advance` / `set_detail`
to report fine-grained progress. The web server reads `snapshot()` (thread-safe) each
poll to render a stepper, progress bars, an ETA, and a live log tail.

`NULL` is a no-op reporter so stage modules can be run standalone (or from the CLI)
without a live reporter — every method is a harmless no-op.
"""
import json
import os
import threading
import time
from collections import deque

# Ordered stage list (key, human label). `ai_*` are marked skipped when skip_ai is set.
STAGES = [
    ("fetch", "Fetch GEO studies"),
    ("extract", "Extract SRA metadata"),
    ("prep", "Prepare AI batches"),
    ("ai_compounds", "AI: canonicalize compounds"),
    ("ai_samples", "AI: classify samples"),
    ("merge", "Merge AI results"),
    ("build", "Build tables"),
    # deep dive: Run Selector metadata for the single best cell line
    ("select", "Select best cell line"),
    ("runtable_fetch", "Fetch run metadata (SRA XML)"),
    ("runtable_build", "Reconstruct Run Selector table"),
    ("cellline_match", "AI: match cell-line names"),
    ("runtable_annotate", "Annotate drugs + workbook"),
    # cluster handoff
    ("cluster_bundle", "Build cluster bundle"),
    ("cluster_submit", "Upload + launch on cluster"),
    # STAR alignment (Bulk RNA-seq module; auto-chained after the download)
    ("star_bundle", "Build STAR bundle"),
    ("star_submit", "Launch STAR alignment"),
    # BAM -> BED (AltAnalyze junction/exon; auto-chained after STAR)
    ("bed_bundle", "Build BED bundle"),
    ("bed_submit", "Launch BAM->BED"),
    # AltAnalyze splicing (PSI; auto-chained after BAM->BED)
    ("psi_bundle", "Build AltAnalyze (PSI) bundle"),
    ("psi_submit", "Run AltAnalyze splicing"),
    # splicing concordance (drug PSI vs cancer atlas; auto-chained after PSI)
    ("concordance_bundle", "Build concordance bundle"),
    ("concordance_submit", "Run drug-vs-cancer concordance"),
]

# Rough fraction-of-wall-time weights used only to drive the *overall* progress bar
# and a coarse overall ETA. Extract (per-study network fetches, rate-limited) dominates.
# Skipped stages get their weight removed and the rest renormalized at snapshot time.
WEIGHTS = {
    "fetch": 0.05,
    "extract": 0.57,
    "prep": 0.01,
    "ai_compounds": 0.05,
    "ai_samples": 0.11,
    "merge": 0.01,
    "build": 0.04,
    "select": 0.01,
    "runtable_fetch": 0.06,
    "runtable_build": 0.02,
    "cellline_match": 0.02,
    "runtable_annotate": 0.02,
    "cluster_bundle": 0.01,
    "cluster_submit": 0.02,
    "star_bundle": 0.01,
    "star_submit": 0.02,
    "bed_bundle": 0.01,
    "bed_submit": 0.02,
    "psi_bundle": 0.01,
    "psi_submit": 0.02,
    "concordance_bundle": 0.01,
    "concordance_submit": 0.02,
}

# ---------------------------------------------------------------------------
# Phase-range control. The web UI's vertical dual-handle slider snaps to these
# ordered PHASES; the two handles pick the FIRST and LAST phase to run. A phase
# is a contiguous run of stages named by the boundary you can START from.
#   key       : stable slider id (== the start stage)
#   label     : shown beside the slider tick
#   stage     : the FIRST stage that runs when STARTING at this phase
#   end_stage : the LAST stage that runs when ENDING at this phase
#   inputs    : artifacts to supply when STARTING here on a FRESH run -- each
#               {field, label, kind('file'|'dir'), dest, optional?, desc}.
#               `dest` is a Paths attribute name to copy the supplied path into,
#               or the special 'runtable_filtered_csv' (resolved with the cell-line
#               slug at inject time). Stages before `stage` are pre-marked done;
#               stages after `end_stage` never run. RunConfig stores start_stage/
#               end_stage (the stage keys), NOT `key`.
# ---------------------------------------------------------------------------
_SEL_INPUT = {"field": "cellline_selection", "label": "cellline_selection.json", "kind": "file",
              "dest": "cellline_selection",
              "desc": "the chosen cell line + its studies (its name maps to the cluster folder where your data lives)"}

CHECKPOINTS = [
    {"key": "fetch", "label": "Fetch GEO studies", "stage": "fetch", "end_stage": "fetch",
     "inputs": []},
    {"key": "extract", "label": "Extract metadata", "stage": "extract", "end_stage": "extract",
     "inputs": [{"field": "raw_json", "label": "ncbi_raw.json", "kind": "file", "dest": "raw_json",
                 "desc": "raw GEO study JSON from a prior fetch"}]},
    {"key": "prep", "label": "AI clean + build tables", "stage": "prep", "end_stage": "build",
     "inputs": [{"field": "samples_jsonl", "label": "structured_samples.jsonl", "kind": "file",
                 "dest": "samples_jsonl", "desc": "structured per-sample metadata from a prior extract"}]},
    {"key": "select", "label": "Select cell line", "stage": "select", "end_stage": "select",
     "inputs": [{"field": "cellline_index", "label": "cellline_index.json", "kind": "file",
                 "dest": "cellline_index", "desc": "the all-cell-line index from a prior build"}]},
    {"key": "runtable_fetch", "label": "Build run table", "stage": "runtable_fetch", "end_stage": "runtable_annotate",
     "inputs": [_SEL_INPUT]},
    {"key": "cluster_bundle", "label": "Download reads", "stage": "cluster_bundle", "end_stage": "cluster_submit",
     "inputs": [_SEL_INPUT,
                {"field": "by_study_dir", "label": "by_study/ folder", "kind": "dir", "dest": "by_study_dir",
                 "desc": "per-study SraAccList.txt the cluster download consumes"}]},
    {"key": "star_bundle", "label": "STAR align", "stage": "star_bundle", "end_stage": "star_submit",
     "inputs": [_SEL_INPUT,
                {"field": "star_runtable", "label": "SraRunTable CSV (optional)", "kind": "file",
                 "dest": "runtable_filtered_csv", "optional": True,
                 "desc": "filtered run table so STAR merges runs of one BioSample into one BAM (optional; the FASTQs must already be on the cluster)"}]},
    {"key": "bed_bundle", "label": "BAM → BED", "stage": "bed_bundle", "end_stage": "bed_submit",
     "inputs": [_SEL_INPUT]},
    {"key": "psi_bundle", "label": "AltAnalyze PSI", "stage": "psi_bundle", "end_stage": "psi_submit",
     "inputs": [_SEL_INPUT]},
    {"key": "concordance_bundle", "label": "Drug concordance", "stage": "concordance_bundle",
     "end_stage": "concordance_submit", "inputs": [_SEL_INPUT]},
]


class _Stage:
    __slots__ = ("key", "label", "status", "done", "total", "detail",
                 "indeterminate", "started", "ended")

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.status = "pending"        # pending | active | done | skipped
        self.done = 0
        self.total = None              # None => indeterminate count
        self.detail = ""
        self.indeterminate = False
        self.started = None            # monotonic
        self.ended = None              # monotonic

    def fraction(self):
        if self.status == "done":
            return 1.0
        if self.status in ("pending", "skipped"):
            return 0.0
        # active
        if self.total:
            return min(1.0, self.done / self.total)
        return 0.0


class RunReporter:
    """Thread-safe progress tracker for one pipeline run."""

    def __init__(self, run_dir=None, progress_path=None):
        self._lock = threading.RLock()
        self._stages = [_Stage(k, lbl) for k, lbl in STAGES]
        self._by_key = {s.key: s for s in self._stages}
        self._cur = None               # current stage key
        self._t0 = time.monotonic()
        self._started_epoch = time.time()
        self._log = deque(maxlen=600)
        self._log_total = 0            # lines EVER appended (monotonic) -- the UI redraw key; len(_log) caps at 600
        self._state = "running"        # running | done | error
        self._error = None
        self._result = None
        self._files = []
        self._meta = {}
        # manual cell-line pick (deep dive): pause mid-run for a UI/CLI choice
        self._awaiting = False
        self._candidates = []
        self._selected = None
        self._select_event = threading.Event()
        # cluster upload retry: pause at the end to collect corrected SSH/cluster info
        self._awaiting_cluster = False
        self._cluster_prompt = None
        self._cluster_fix = None
        self._cluster_event = threading.Event()
        # AI preflight fix: pause if the provider/model/key is bad -> correct it or turn AI off
        self._awaiting_ai = False
        self._ai_prompt = None
        self._ai_fix = None
        self._ai_event = threading.Event()
        self.run_dir = run_dir
        self.progress_path = progress_path or (
            os.path.join(run_dir, "progress.json") if run_dir else None)
        self._last_flush = 0.0

    # ---- metadata --------------------------------------------------------
    def set_meta(self, **kw):
        with self._lock:
            self._meta.update({k: v for k, v in kw.items() if v is not None})
            self._flush_locked(force=True)

    # ---- stage lifecycle -------------------------------------------------
    def begin_stage(self, key, total=None, indeterminate=False, detail=""):
        with self._lock:
            st = self._by_key.get(key)
            if st is None:
                return
            st.status = "active"
            st.started = time.monotonic()
            st.total = total
            st.done = 0
            st.indeterminate = indeterminate or (total is None)
            st.detail = detail
            self._cur = key
            self._flush_locked(force=True)

    def complete_stage(self, key=None):
        with self._lock:
            st = self._by_key.get(key) if key else self._by_key.get(self._cur)
            if st is None:
                return
            if st.status == "skipped":   # a pre-skipped (out-of-range) stage must not flip to done
                return
            if st.total:
                st.done = st.total
            st.status = "done"
            st.ended = time.monotonic()
            st.indeterminate = False
            self._flush_locked(force=True)

    def skip_stage(self, key):
        with self._lock:
            st = self._by_key.get(key)
            if st is None:
                return
            st.status = "skipped"
            self._flush_locked(force=True)

    # ---- fine-grained progress (called by stages) ------------------------
    def set_total(self, total):
        with self._lock:
            st = self._by_key.get(self._cur)
            if st is not None:
                st.total = total
                st.indeterminate = total is None
                self._flush_locked()

    def advance(self, n=1):
        with self._lock:
            st = self._by_key.get(self._cur)
            if st is not None:
                st.done += n
                self._flush_locked()

    def set_detail(self, text):
        with self._lock:
            st = self._by_key.get(self._cur)
            if st is not None:
                st.detail = str(text)[:200]
                self._flush_locked()

    # ---- logging ---------------------------------------------------------
    def log(self, line):
        line = str(line).rstrip("\n")
        if not line:
            return
        with self._lock:
            ts = time.time() - self._started_epoch
            for piece in line.split("\n"):
                self._log.append({"t": round(ts, 1), "text": piece[:400]})
                self._log_total += 1
            self._flush_locked()

    # ---- manual cell-line selection (deep dive) --------------------------
    def await_selection(self, candidates, timeout=3600):
        """Block until a UI/CLI choice arrives (or timeout). Returns the chosen canonical or None.

        Sets `awaiting_selection` + `candidates` in the snapshot so the server can render the
        ranked list and POST /api/select. On timeout returns None (caller auto-picks rank 1).
        """
        with self._lock:
            self._candidates = candidates
            self._awaiting = True
            self._selected = None
            self._select_event.clear()
            self._flush_locked(force=True)
        got = self._select_event.wait(timeout)
        with self._lock:
            self._awaiting = False
            chosen = self._selected
            self._flush_locked(force=True)
        return chosen if got else None

    def provide_selection(self, canonical):
        """Called by the server (/api/select) to deliver the user's pick. Returns True if accepted."""
        with self._lock:
            if not self._awaiting:
                return False
            self._selected = canonical
            self._select_event.set()   # set under the lock to close the lost-wakeup race
        return True

    # ---- cluster upload retry (collect corrected SSH/cluster info; no full rerun) ---------
    def await_cluster_fix(self, diagnosis, current, timeout=3600):
        """Block until the UI/CLI supplies corrected cluster info (or timeout).

        Sets `awaiting_cluster_fix` + `cluster_prompt` (the diagnosis + current non-secret values)
        in the snapshot so the server can render a prefilled SSH form and POST /api/cluster_retry.
        Returns the fix payload {cluster:{...}, ssh_password?} (or {"action": "cancel"} to give up);
        None on timeout (caller leaves the bundle downloadable).
        """
        with self._lock:
            self._cluster_prompt = {"diagnosis": diagnosis, "current": current or {}}
            self._awaiting_cluster = True
            self._cluster_fix = None
            self._cluster_event.clear()
            self._flush_locked(force=True)
        got = self._cluster_event.wait(timeout)
        with self._lock:
            self._awaiting_cluster = False
            fix = self._cluster_fix
            self._flush_locked(force=True)
        return fix if got else None

    def provide_cluster_fix(self, payload):
        """Called by the server (/api/cluster_retry) to deliver corrected info. True if accepted."""
        with self._lock:
            if not self._awaiting_cluster:
                return False
            self._cluster_fix = payload
            self._cluster_event.set()   # set under the lock to close the lost-wakeup race
        return True

    # ---- AI preflight fix (bad provider/model/key -> correct it or turn AI off) ----------
    def await_ai_fix(self, diagnosis, current, timeout=3600):
        """Block until the UI/CLI supplies a corrected AI config or chooses to turn AI off.

        Sets `awaiting_ai_fix` + `ai_prompt` (the diagnosis + current provider/model) in the snapshot
        so the server can render a fix form and POST /api/ai_retry. Returns the fix payload
        {provider?, model?, api_key?} or {"action": "skip_ai"}; None on timeout (caller turns AI off).
        """
        with self._lock:
            self._ai_prompt = {"diagnosis": diagnosis, "current": current or {}}
            self._awaiting_ai = True
            self._ai_fix = None
            self._ai_event.clear()
            self._flush_locked(force=True)
        got = self._ai_event.wait(timeout)
        with self._lock:
            self._awaiting_ai = False
            fix = self._ai_fix
            self._flush_locked(force=True)
        return fix if got else None

    def provide_ai_fix(self, payload):
        """Called by the server (/api/ai_retry) to deliver corrected AI info. True if accepted."""
        with self._lock:
            if not self._awaiting_ai:
                return False
            self._ai_fix = payload
            self._ai_event.set()   # set under the lock to close the lost-wakeup race
        return True

    # ---- terminal states -------------------------------------------------
    def finish(self, result=None, files=None):
        with self._lock:
            # snap any active stage to done
            for st in self._stages:
                if st.status == "active":
                    if st.total:
                        st.done = st.total
                    st.status = "done"
                    st.ended = time.monotonic()
            self._state = "done"
            self._result = result
            self._files = files or []
            self._flush_locked(force=True)

    def fail(self, error):
        with self._lock:
            self._error = str(error)
            self._state = "error"
            for st in self._stages:
                if st.status == "active":
                    st.ended = time.monotonic()
            self._flush_locked(force=True)

    # ---- snapshot --------------------------------------------------------
    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._t0

            active_keys = [s.key for s in self._stages if s.status != "skipped"]
            wsum = sum(WEIGHTS[k] for k in active_keys) or 1.0

            overall_fraction = 0.0
            for s in self._stages:
                if s.status == "skipped":
                    continue
                overall_fraction += (WEIGHTS[s.key] / wsum) * s.fraction()
            overall_fraction = max(0.0, min(1.0, overall_fraction))

            # overall ETA: project from elapsed once we have a meaningful fraction
            overall_eta = None
            if self._state == "running" and overall_fraction > 0.02 and elapsed > 3:
                overall_eta = elapsed * (1 - overall_fraction) / overall_fraction

            stages_out = []
            cur_eta = None
            for s in self._stages:
                st_elapsed = None
                st_eta = None
                if s.started is not None:
                    end = s.ended if s.ended is not None else now
                    st_elapsed = end - s.started
                if (s.status == "active" and s.total and s.done > 0
                        and s.started is not None):
                    rate = s.done / max(1e-9, now - s.started)
                    if rate > 0:
                        st_eta = (s.total - s.done) / rate
                        if s.key == self._cur:
                            cur_eta = st_eta
                stages_out.append({
                    "key": s.key,
                    "label": s.label,
                    "status": s.status,
                    "done": s.done,
                    "total": s.total,
                    "detail": s.detail,
                    "indeterminate": s.indeterminate and s.status == "active",
                    "fraction": round(s.fraction(), 4),
                    "elapsed": round(st_elapsed, 1) if st_elapsed is not None else None,
                    "eta": round(st_eta, 1) if st_eta is not None else None,
                })

            cur = self._by_key.get(self._cur)
            return {
                "state": self._state,
                "error": self._error,
                "awaiting_selection": self._awaiting,
                "candidates": self._candidates if self._awaiting else [],
                "awaiting_cluster_fix": self._awaiting_cluster,
                "cluster_prompt": self._cluster_prompt if self._awaiting_cluster else None,
                "awaiting_ai_fix": self._awaiting_ai,
                "ai_prompt": self._ai_prompt if self._awaiting_ai else None,
                "meta": dict(self._meta),
                "elapsed": round(elapsed, 1),
                "started_epoch": self._started_epoch,
                "overall_fraction": round(overall_fraction, 4),
                "overall_eta": round(overall_eta, 1) if overall_eta is not None else None,
                "current": self._cur,
                "current_label": cur.label if cur else None,
                "current_eta": round(cur_eta, 1) if cur_eta is not None else None,
                "stages": stages_out,
                "result": self._result,
                "files": self._files,
                "log": list(self._log),
                "log_total": self._log_total,
            }

    # ---- disk flush (best-effort, throttled) -----------------------------
    def _flush_locked(self, force=False):
        if not self.progress_path:
            return
        now = time.monotonic()
        if not force and (now - self._last_flush) < 0.4:
            return
        self._last_flush = now
        try:
            tmp = self.progress_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f)   # RLock is reentrant in this thread
            os.replace(tmp, self.progress_path)
        except Exception as e:
            # best-effort; never let it break a run, but don't be fully silent on ENOSPC/EACCES
            print(f"WARN: progress flush failed ({self.progress_path}): {e}")


class _NullReporter:
    """No-op reporter: lets stage modules report unconditionally even with no run UI."""

    def set_meta(self, **kw):
        pass

    def begin_stage(self, *a, **k):
        pass

    def complete_stage(self, *a, **k):
        pass

    def skip_stage(self, *a, **k):
        pass

    def set_total(self, *a, **k):
        pass

    def advance(self, *a, **k):
        pass

    def set_detail(self, *a, **k):
        pass

    def log(self, *a, **k):
        pass

    def await_selection(self, candidates, timeout=3600):
        return None  # standalone/CLI: no UI to pick -> caller auto-picks rank 1

    def provide_selection(self, canonical):
        return False

    def await_cluster_fix(self, diagnosis, current, timeout=3600):
        return None  # standalone/CLI with no UI: caller leaves the bundle downloadable

    def provide_cluster_fix(self, payload):
        return False

    def await_ai_fix(self, diagnosis, current, timeout=3600):
        return None  # standalone/CLI with no UI: caller turns AI off

    def provide_ai_fix(self, payload):
        return False

    def finish(self, *a, **k):
        pass

    def fail(self, *a, **k):
        pass

    def snapshot(self):
        return {"state": "idle"}


NULL = _NullReporter()
