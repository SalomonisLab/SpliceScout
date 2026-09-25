"""
Stage 2 — EXTRACT (+ PROTOCOL): per-sample structured metadata from NCBI SRA/BioSample.

For each GEO study: GEO->SRA via elink (esearch fallback), batched efetch of SRA XML,
parse each EXPERIMENT_PACKAGE for {gsm, title, cell_line, source, treatments_raw[], spots}.
ALSO capture the study's library-construction protocol from the first experiment in the same
fetch (LIBRARY_CONSTRUCTION_PROTOCOL / STRATEGY / SELECTION / INSTRUMENT) -> study_protocol.json.

NO AI, NO keyword guessing. Compound dose/control normalization uses the shared normalize_v2
module (single source of truth). Resumable via the done-set.
"""
import os
import re
import json
import time
import html
import threading
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

from normalize_v2 import clean_compound  # single source of truth for dose/control logic
from progress import NULL

COMPOUND_TAGS = {
    "treatment", "treatments", "agent", "agents", "compound", "compounds",
    "drug", "drugs", "drug treatment", "chemical", "chemical compound",
    "treated with", "small molecule", "inhibitor", "perturbation",
    "perturbagen", "stimulus", "stimulation", "treatment agent",
    "drug/compound", "compound treatment", "treatment compound",
}
# More tags whose VALUE names the agent, seen in the real A549 run table: 'treated' ('Treated with Montelukast
# Sodium'), 'drugs_treated' ('5uM momelotinib and 1uM selumetinib'), 'treatment_description' ('1 h with 5 nM
# Dexamethasone'). ('drug treated' is NOT here: 'drug_treated' is runtable_annotate's own output column.)
COMPOUND_TAGS |= {"treated", "drugs treated", "treatment description"}

# Numbered / generically-qualified variants of a treatment tag ("treatment 2", "treatment_1", "agent 2", "drug
# treatment", "secondary treatment", "exposure"). The exact set missed them, so e.g. A549 GSE124636's
# dexamethasone arms (tag 'treatment_2') were never read -> the whole study had no drug call. The qualifier must be
# GENERIC: an agent-named tag ('tgf-beta_treatment', 'dox_treatment', 'ifn_treatment', 'infectious_agent') holds
# values like 'yes' / 'no' / '1 ng/ml for 24 h' or virus strains, which are not agent names.
_GENERIC_QUALIFIER = (r"(?:drugs?|chemicals?|compounds?|small molecules?|pharmacological|primary|secondary|first|"
                      r"second|third|additional|combination|co)")
_COMPOUND_TAG_RX = re.compile(r"^(?:" + _GENERIC_QUALIFIER + r"\s+)?(?:treatments?|drugs?|compounds?|agents?|"
                              r"exposures?|perturbations?|inhibitors?|chemicals?)(?:\s*\d+)?$")


def is_compound_tag(tag):
    """True for an attribute tag that names the treatment AGENT: COMPOUND_TAGS plus numbered / generically
    qualified variants (case-, underscore- and whitespace-insensitive). The single test for both the headline
    compound count (here) and the per-run annotation (runtable_annotate.treatment_columns), so the two can never
    disagree."""
    t = " ".join(str(tag or "").replace("_", " ").lower().split())
    return t in COMPOUND_TAGS or bool(_COMPOUND_TAG_RX.match(t))


CELL_TAGS = {"cell line", "cell-line", "cell_line", "cellline", "cell line name",
             "cell_line_name"}
SOURCE_TAGS = {"source_name", "source name", "tissue", "sample type",
               "cell type", "cell-type", "cell_type"}

# Global request pacer shared by the parallel extract workers. NCBI allows ~10 req/s with an API key,
# ~3 req/s without. _throttle() serializes only the request STARTS (it sleeps while holding the lock,
# then releases before the network call), so starts are spaced >= interval globally (rate <= 1/interval)
# while responses overlap across threads — that's what makes the API key's higher rate actually pay off
# WITHOUT ever exceeding NCBI's limit.
_state = {"key": None, "interval": 0.34, "baseline": 0.34, "last": 0.0, "last_429": 0.0}
_throttle_lock = threading.Lock()

EXTRACT_RETRY_PASSES = 2   # in-run retry passes over failed studies before treating them as unfetchable


def _configure(ncbi_key):
    _state["key"] = ncbi_key
    # a touch under NCBI's cap (10/s keyed, 3/s keyless) for headroom; _slow_pacer() raises it on a 429,
    # and _recover_locked() ramps it back to this baseline after a quiet minute.
    base = 0.13 if ncbi_key else 0.36
    _state["interval"] = base
    _state["baseline"] = base
    _state["last"] = 0.0
    _state["last_429"] = 0.0
    _state["throttled_notice"] = False


def _is_429(e):
    return isinstance(e, urllib.error.HTTPError) and e.code == 429


def _retry_after(e, default):
    """Honor NCBI's Retry-After header on a 429 when present, else the caller's backoff."""
    try:
        if isinstance(e, urllib.error.HTTPError):
            ra = e.headers.get("Retry-After")
            if ra:
                return max(default, min(120.0, float(ra)))
    except Exception:
        pass
    return default


def _slow_pacer():
    """On a 429, raise the GLOBAL request interval so EVERY parallel worker backs off and the rate-limit
    storm quells itself (not just the one thread that got the 429). NOT permanent: _recover_locked() ramps
    the interval back toward baseline once a quiet minute has passed since the last 429."""
    with _throttle_lock:
        _state["interval"] = min(_state["interval"] + 0.05, 1.0)
        _state["last_429"] = time.monotonic()
        if not _state.get("throttled_notice"):
            _state["throttled_notice"] = True
            print(f"  NCBI 429 rate-limit hit -> slowing ALL extract workers (req interval now "
                  f"~{_state['interval']:.2f}s); automatic + temporary (recovers ~1 min after the last 429).")


def _recover_locked():
    """Caller holds the throttle lock. Ramp the interval back DOWN toward the keyed/keyless baseline once a
    full quiet minute has passed since the last 429 (halving the remaining excess each minute, snapping to
    baseline when within ~0.02s). A fresh 429 re-raises it. Lets a brief throttle storm self-heal instead of
    permanently slowing the rest of the extract."""
    base = _state.get("baseline", _state["interval"])
    if _state["interval"] <= base:
        return
    if time.monotonic() - _state.get("last_429", 0.0) < 60:
        return
    _state["interval"] = max(base, base + (_state["interval"] - base) * 0.5)
    if _state["interval"] - base < 0.02:
        _state["interval"] = base
        _state["throttled_notice"] = False
    _state["last_429"] = time.monotonic()


def _ksuf():
    return f"&api_key={_state['key']}" if _state["key"] else ""


def _throttle():
    """Block until this thread may START a request, keeping the GLOBAL rate <= 1/interval. Self-heals the
    interval back toward baseline once throttling has been quiet for a minute (_recover_locked)."""
    with _throttle_lock:
        _recover_locked()
        wait = _state["interval"] - (time.monotonic() - _state["last"])
        if wait > 0:
            time.sleep(wait)
        _state["last"] = time.monotonic()


def fetch(url, retries=8):
    """GET with global pacing + resilient retries. A 429 (NCBI rate cap) is RIDDEN OUT: honor Retry-After,
    back off, and slow EVERY worker (so the parallel storm quells), retrying generously rather than failing
    the study. Non-429 errors get bounded retries. The network call is outside the throttle lock so responses
    still overlap across threads."""
    url += _ksuf()
    attempt = 0
    while True:
        _throttle()                      # global pacing (thread-safe); network call is outside the lock
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            attempt += 1
            if _is_429(e):
                _slow_pacer()                                       # slow ALL workers, not just this one
                if attempt <= 15:
                    time.sleep(_retry_after(e, min(60.0, 1.5 * (2 ** min(attempt, 5)))))
                    continue
                return None                                         # gave up after generous 429 retries
            if attempt >= retries:
                return None
            time.sleep(1.5 * attempt)


def parse_protocol(xml):
    """Study-level library-prep info from the first experiment in the XML."""
    def g(tag):
        m = re.findall(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
        return html.unescape(m[0]).strip() if m else ""
    return {
        "protocol": g("LIBRARY_CONSTRUCTION_PROTOCOL")[:600],
        "strategy": g("LIBRARY_STRATEGY"),
        "selection": g("LIBRARY_SELECTION"),
        "instrument": g("INSTRUMENT_MODEL"),
    }


def parse_samples(xml, study):
    """Yield one per-sample dict per EXPERIMENT_PACKAGE."""
    for p in re.split(r"<EXPERIMENT_PACKAGE>", xml)[1:]:
        gsm_m = re.search(r"\b(GSM\d+)\b", p)
        gsm = gsm_m.group(1) if gsm_m else ""
        title = ""
        for t in re.findall(r"<TITLE>(.*?)</TITLE>", p, re.S):
            t = html.unescape(t).strip()
            if t.startswith("GSM"):
                title = re.sub(r"^GSM\d+:\s*", "", t)
                break
        if not title:
            titles = re.findall(r"<TITLE>(.*?)</TITLE>", p, re.S)
            if titles:
                title = html.unescape(titles[-1]).strip()

        cell, source, treats = "", "", []
        for tag, val in re.findall(r"<TAG>(.*?)</TAG>\s*<VALUE>(.*?)</VALUE>", p, re.S):
            tg = html.unescape(tag).strip().lower()
            vl = html.unescape(val).strip()
            if not vl or vl.lower() in ("missing", "not applicable", "n/a", "na"):
                continue
            if tg in CELL_TAGS and not cell:
                cell = vl
            if tg in SOURCE_TAGS and not source:
                source = vl
            if is_compound_tag(tg):
                treats.append(vl)
        spots_list = [int(s) for s in re.findall(r'spots="(\d+)"', p)]
        spots = spots_list[0] if spots_list else 0

        comps = sorted({c for v in treats for c in [clean_compound(v)] if c})
        yield {
            "study": study, "gsm": gsm, "title": title,
            "cell_line": cell, "source": source, "treatments_raw": sorted(set(treats)),
            "compounds": comps, "spots": spots,
        }


def _study_meta(uid, meta_item=None):
    """(srp_or_bioproject, gsm_accessions, n_samples) from a gds esummary record. The GSMs drive the precise
    per-sample SRA resolution; the SRP/BioProject is only a coarse fallback term that MUST be guarded by
    n_samples before use -- a BioProject is frequently a SHARED umbrella (e.g. ENCODE PRJNA30709 spans 5867
    runs across thousands of 2-sample sub-series). '' / [] / 0 on none / fetch failure.

    PERF: Stage 1 (fetch_5000_ncbi) already loaded this study's esummary into raw_json with the IDENTICAL
    schema (extrelations/bioproject/samples/n_samples), so when run() passes it in as `meta_item` we reuse it
    and SKIP a redundant esummary round-trip per study (the common case, since path-2 esearch is ~always 0).
    Falls back to a live esummary fetch when meta_item is missing/empty (a uid Stage 1 couldn't summarize)."""
    rec = meta_item if isinstance(meta_item, dict) and meta_item else None
    if rec is None:
        s = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
                  f"?db=gds&id={uid}&retmode=json")
        if not s:
            return "", [], 0
        try:
            rec = json.loads(s)["result"][str(uid)]
        except Exception:
            return "", [], 0
    srp = ""
    for r in (rec.get("extrelations") or []):
        if (r.get("relationtype") or "").upper() == "SRA" and r.get("targetobject"):
            srp = r["targetobject"]                        # e.g. SRP301436
            break
    if not srp:
        srp = rec.get("bioproject") or ""                  # e.g. PRJNA691557 (may be a shared umbrella!)
    gsms = [x.get("accession") for x in (rec.get("samples") or []) if x.get("accession")]
    try:
        n = int(rec.get("n_samples") or 0)
    except Exception:
        n = 0
    return srp, gsms, n


def _esearch_count(term):
    """TOTAL SRA matches for a term (retmax=0) -- used to detect a shared-umbrella BioProject whose run count
    dwarfs the study's real sample count. None on fetch failure."""
    s = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
              f"?db=sra&term={urllib.parse.quote(term)}&retmax=0&retmode=json")
    if s is None:
        return None
    try:
        return int(json.loads(s)["esearchresult"]["count"])
    except Exception:
        return None


def sra_ids_for_study(uid, acc, meta_item=None):
    """SRA experiment UIDs for a GEO study, via THREE resolution paths. The entrez gds->sra elink is
    MISSING for some studies AND SRA's free-text index doesn't contain GEO accessions, which used to
    SILENTLY DROP studies that DO have SRA data (e.g. GSE164788: elink=none, esearch 'GSE164788'=0 — yet
    its SRA study SRP301436 has 765 runs). Order: (1) elink gds->sra; (2) esearch sra by the GSE
    accession; (3) esearch sra by the study's SRP/BioProject — GUARDED: skipped when its run count dwarfs the
    study's GEO sample count, since a BioProject is often a SHARED umbrella (ENCODE PRJNA30709 = 5867 runs
    across thousands of 2-sample sub-series, which used to be mis-attributed wholesale, truncated at retmax);
    (4) esearch sra by the study's OWN GSM accessions (from the esummary samples list) — the exact per-sample
    resolution that recovers an umbrella sub-series as just its handful of runs. Returns [] for a genuinely
    SRA-less study (e.g. a microarray series) and None only when EVERY esearch fetch failed (caller retries)."""
    e = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
              f"?dbfrom=gds&db=sra&id={uid}&retmode=json")
    if e is not None:
        try:
            ls = json.loads(e).get("linksets", [])
            if ls and ls[0].get("linksetdbs"):
                ids = ls[0]["linksetdbs"][0].get("links", [])
                if ids:
                    return ids
        except Exception:
            pass

    def _esearch_ids(term):
        s = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
                  f"?db=sra&term={urllib.parse.quote(term)}&retmax=2000&retmode=json")
        if s is None:
            return None
        try:
            return json.loads(s)["esearchresult"]["idlist"]
        except Exception:
            return []

    any_ok = False
    ids = _esearch_ids(acc)                                # 2. by the GSE accession (usually 0 — SRA has no GEO accns)
    if ids is not None:
        any_ok = True
        if ids:
            return ids

    srp, gsms, n_samples = _study_meta(uid, meta_item)     # reuse Stage-1 esummary (raw_json) if passed; else fetch

    # 3. by SRP/BioProject — but SKIP a SHARED UMBRELLA whose SRA run count dwarfs the study's GEO samples
    #    (ENCODE PRJNA30709: 5867 runs vs a 2-sample sub-series). count ~= n_samples => a real, dedicated study.
    if srp and srp != acc:
        cnt = _esearch_count(srp)
        if cnt is not None and cnt > max(200, 10 * n_samples):
            any_ok = True                                  # the fetch worked; we just refuse the umbrella's runs
            print(f"  {acc}: SRA project {srp} has {cnt} runs >> {n_samples} GEO samples "
                  f"(shared umbrella, e.g. ENCODE) -> resolving by the study's own GSMs instead")
        else:
            ids = _esearch_ids(srp)
            if ids is not None:
                any_ok = True
                if ids:
                    return ids

    # 4. by the study's OWN GSMs — exact per-sample, so an umbrella sub-series resolves to just its runs.
    gsm_ids = []
    for i in range(0, len(gsms), 100):                     # batch so the OR'd esearch term URL stays a sane length
        part = _esearch_ids(" OR ".join(gsms[i:i + 100]))
        if part is None:
            continue
        any_ok = True
        gsm_ids.extend(part)
    if gsm_ids:
        return list(dict.fromkeys(gsm_ids))

    return [] if any_ok else None


def process_study(uid, acc, meta_item=None):
    """Return (rows, protocol_dict_or_None) for one study, or (None, None) on fetch failure."""
    ids = sra_ids_for_study(uid, acc, meta_item)
    if ids is None:
        return None, None
    rows, protocol = [], None
    any_batch_ok = False
    for i in range(0, len(ids), 50):
        batch = ",".join(ids[i:i + 50])
        xml = fetch(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=sra&id={batch}")
        if not xml:
            continue
        any_batch_ok = True
        if protocol is None:  # capture protocol from the first successful batch
            protocol = parse_protocol(xml)
        rows.extend(parse_samples(xml, acc))
    # had SRA experiments but EVERY efetch batch failed -> a fetch failure to RETRY, not an empty study
    if ids and not any_batch_ok:
        return None, None
    return rows, (protocol or {"protocol": "", "strategy": "", "selection": "", "instrument": ""})


def _atomic_json(path, obj):
    """Write JSON atomically: temp file -> flush -> fsync -> os.replace. A kill mid-write then can't
    truncate the file and break the next --resume's json.load (which would otherwise lose all progress)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _checkpoint(P, done, protocols):
    """Persist the resume checkpoint (done-set + protocols), atomically. Called every N studies + at the
    end — batching keeps these growing O(n) JSON dumps off the per-study hot path on big runs. Safe because
    structured_samples.jsonl is keyed by GSM downstream (build_final), so a crash that re-fetches a few
    not-yet-checkpointed studies just rewrites the same rows (deduped), never corrupts the output."""
    _atomic_json(P.done_set, sorted(done))
    _atomic_json(P.study_protocol, protocols)


def run(P, ncbi_key=None, cap=None, reporter=NULL, workers=8):
    """Stage 2 entry. Writes P.samples_jsonl, P.done_set, P.study_protocol. Resumable.

    Studies are fetched in PARALLEL (ThreadPoolExecutor) behind the global _throttle() pacer, so the
    NCBI API key's higher rate (10/s vs 3/s) actually turns into speed — the pacer caps request STARTS
    to NCBI's limit while responses overlap, so we never exceed the limit. `workers` is capped to a safe
    ceiling; the rate limiter (not the thread count) is what bounds the request rate. Resume-safe: the
    done-set + protocols are checkpointed after each completed study under a write lock."""
    _configure(ncbi_key)
    with open(P.raw_json, "r", encoding="utf-8") as f:
        result = json.load(f)["result"]
    uids = result["uids"]
    # FETCH keeps EVERY search id in "uids" but writes a record only for ids whose esummary succeeded (the
    # rest go to missing_summaries.json) -> skip record-less ids instead of crashing the stage on KeyError.
    missing = [u for u in uids if not isinstance(result.get(u), dict)]
    if missing:
        print(f"  EXTRACT: {len(missing)} study id(s) have no esummary record (failed during FETCH; see "
              f"missing_summaries.json) -> skipped")
    # carry each study's Stage-1 esummary record (meta_item) so _study_meta reuses it instead of re-fetching
    pairs = [(u, result[u].get("accession"), result[u]) for u in uids if isinstance(result.get(u), dict)]
    if cap:
        pairs = pairs[:cap]

    done = set()
    if os.path.exists(P.done_set):
        try:
            done = set(json.load(open(P.done_set, "r", encoding="utf-8")))
        except Exception:
            done = set()
    protocols = {}
    if os.path.exists(P.study_protocol):
        try:
            protocols = json.load(open(P.study_protocol, "r", encoding="utf-8"))
        except Exception:
            protocols = {}

    # progress: total = all studies in scope; pre-credit ones already done / unaddressable
    reporter.set_total(len(pairs))
    skipped = sum(1 for (_, acc, _m) in pairs if (not acc) or (acc in done))
    if skipped:
        reporter.advance(skipped)
    todo = [(uid, acc, m) for (uid, acc, m) in pairs if acc and acc not in done]
    n_workers = max(1, min(int(workers or 8), 24))   # rate limiter caps req/s regardless of thread count
    reporter.set_detail(f"{len(todo)} studies to fetch · {n_workers} parallel · "
                        f"{'keyed ~10/s' if ncbi_key else 'keyless ~3/s'}")
    print(f"=== EXTRACT+PROTOCOL: {len(pairs)} studies (done already: {len(done)}; "
          f"{len(todo)} to fetch, {n_workers} parallel) ===")

    # Studies that already failed on a PRIOR run of this run-dir. We still retry each once more; if it
    # fails AGAIN it is treated as permanently unavailable and SKIPPED (warn) rather than blocking the
    # pipeline forever -> auto-escape after one resume, while genuinely transient failures still re-run.
    failed_path = os.path.join(os.path.dirname(os.path.abspath(P.done_set)), "failed_studies.json")
    prev_failed = set()
    if os.path.exists(failed_path):
        try:
            with open(failed_path, encoding="utf-8") as f:
                prev_failed = set(json.load(f))
        except Exception:
            prev_failed = set()

    jf = open(P.samples_jsonl, "a", encoding="utf-8")
    write_lock = threading.Lock()
    counters = {"processed": 0, "seen": 0}

    def handle(uid, acc, meta_item):
        try:
            rows, protocol = process_study(uid, acc, meta_item)
        except Exception:
            return uid, acc, meta_item, None, None   # never let a parse/transport error escape the pool
        return uid, acc, meta_item, rows, protocol

    def run_pass(work, advance):
        """One parallel pass over (uid, acc, meta_item) triples. Returns the triples that FAILED (rows None)."""
        failed_pairs = []
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            futs = [ex.submit(handle, uid, acc, m) for (uid, acc, m) in work]
            for fut in as_completed(futs):
                uid, acc, meta_item, rows, protocol = fut.result()   # handle() never raises
                if rows is None:
                    failed_pairs.append((uid, acc, meta_item))
                    print(f"  {acc} FETCH FAILED (will retry)")
                    if advance:
                        reporter.advance(1)
                    reporter.set_detail(f"{acc}: fetch failed (will retry)")
                    continue
                with write_lock:
                    for r in rows:
                        jf.write(json.dumps(r, ensure_ascii=False) + "\n")
                    jf.flush()
                    protocols[acc] = protocol
                    done.add(acc)
                    counters["processed"] += 1
                    counters["seen"] += 1
                    seen = counters["seen"]
                    if seen % 25 == 0:                 # periodic checkpoint (final one in finally)
                        _checkpoint(P, done, protocols)
                if advance:
                    reporter.advance(1)
                reporter.set_detail(f"{acc} (+{len(rows)} samples)")
                if counters["seen"] % 20 == 0:
                    print(f"  [{counters['seen']}/{len(todo)}] {acc}  (+{len(rows)} samples)")
        return failed_pairs

    failed = []
    try:
        failed = run_pass(todo, advance=True)
        for rp in range(EXTRACT_RETRY_PASSES):         # in-run retry of transient NCBI failures
            if not failed:
                break
            wait = 10 * (rp + 1)
            print(f"  Retry pass {rp + 1}/{EXTRACT_RETRY_PASSES} for {len(failed)} failed studies "
                  f"(waiting {wait}s)…")
            reporter.set_detail(f"retrying {len(failed)} failed studies (pass {rp + 1})…")
            time.sleep(wait)
            failed = run_pass(failed, advance=False)
    finally:
        _checkpoint(P, done, protocols)               # always persist final progress
        jf.close()

    now_failed = sorted({acc for (_uid, acc, _m) in failed})
    _atomic_json(failed_path, now_failed)
    new_failures = [a for a in now_failed if a not in prev_failed]
    print(f"  Done. Processed {counters['processed']} new studies "
          f"({len(now_failed)} still failed). Total done: {len(done)}.")
    if new_failures:
        # WS2: raise so the caller does NOT mark this stage done -> a --resume retries ONLY these
        # (the done-set is checkpointed). NCBI was likely throttling/down and the failures persisted.
        raise RuntimeError(
            f"extract: {len(now_failed)} study(ies) could not be fetched after {EXTRACT_RETRY_PASSES} "
            f"retries ({len(new_failures)} new this run, e.g. "
            f"{', '.join(new_failures[:10])}{' …' if len(new_failures) > 10 else ''}). "
            f"Progress is checkpointed — resume to retry only these.")
    if now_failed:
        # every remaining failure already failed on a prior run -> permanently unavailable: PROCEED
        # (loud warning) instead of blocking forever on deleted/restricted GEO studies.
        msg = (f"{len(now_failed)} study(ies) failed again across runs; SKIPPING as permanently "
               f"unavailable: {', '.join(now_failed[:10])}{' …' if len(now_failed) > 10 else ''}")
        print(f"  WARNING: {msg}")
        reporter.set_detail("proceeding; " + msg)
    return {"studies_done": len(done), "failed": now_failed}


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--ncbi-key", default=None)
    ap.add_argument("--cap", type=int, default=None)
    a = ap.parse_args()
    run(Paths(a.run_dir).ensure_dirs(), a.ncbi_key, a.cap)


if __name__ == "__main__":
    main()
