"""
Stage 4/5 — AI cleaning via the Anthropic API (replaces the Claude Code Workflow fan-out).

Two passes, each over one vocabulary, written to the same ai_work/*_results/ files that
merge_ai.py consumes:
  - "compounds": raw compound string -> {name (standard generic), is_drug}     (canon + synonym)
  - "samples":   title OR cell-tag value -> {cell_line, category, drug_treated} (title-classify + cell-canon)

Forced single tool-use returns an ARRAY of {raw, ...}; we pivot it into the {raw: {...}} map.
Shared instruction block is prompt-cached. Concurrency-capped, resumable, streamed (no timeouts).
Requires: pip install anthropic ; env ANTHROPIC_API_KEY.
"""
import os
import re
import json
import glob
import random
import asyncio

from progress import NULL
import llm_providers

RATE_RETRY_SECS = 30   # on a provider rate-limit (429), wait this long and re-submit the SAME batch
RATE_MAX_ATTEMPTS = 12  # ...but cap it (~6 min) so a permanently-throttled key can't hang a slot forever
# Non-rate transient failures (a model intermittently returning an EMPTY tool call -> "no output", a
# flaky proxy, a dropped connection) are common when many batches fan out at once against one endpoint
# (e.g. a high-concurrency local proxy). Retry with exponential backoff so a few unlucky batches self-
# heal as the burst clears, instead of giving up after a couple of quick tries and failing the stage.
BATCH_MAX_ATTEMPTS = 10   # provider-ERROR retries per unit before _HardFail (the batch is left for resume)
INCOMPLETE_RETRIES = 2    # retries on a PARTIAL/empty model reply at one size before SPLITTING the unit
COVERAGE_OK = 0.90        # a reply that classified >= this fraction of the unit's items is accepted
SPLIT_FLOOR = 25          # don't split a unit smaller than this — SALVAGE (keep classified, Unknown the rest)
# Failure handling (user, 2026-06-16) — recover as many samples as possible, never let a few bad batches
# kill the run: a unit that keeps coming back INCOMPLETE is SPLIT in half (smaller requests don't truncate)
# and whatever still won't classify is kept as-is with the residue Unknown-filled. A batch the PROVIDER
# can't answer at all (down/throttled) is left missing; afterwards the few stragglers are dropped, UNLESS
# more than DROP_CEILING fail (= provider down mid-pass) where we RAISE for --resume rather than gut data.
DROP_CEILING_FRAC = 0.10
DROP_CEILING_MIN = 5


class _HardFail(Exception):
    """Every retry for a unit hit a provider/transport ERROR (down/throttled) — distinct from the model
    merely returning an incomplete map. Lets the caller leave the batch for --resume, not fake Unknowns."""

CATEGORIES = ["Cell line", "Primary cells", "Immune/PBMC", "iPSC/ESC",
              "Organoid", "Tissue", "Patient/Tumor", "Single-cell", "Unknown"]

COMPOUND_INSTRUCTIONS = (
    "You canonicalize drug/compound names from NCBI RNA-seq metadata. You receive a JSON array "
    "of raw compound/treatment strings. For EVERY input string call the emit tool once with a "
    "result object: \n"
    "- raw: the EXACT input string, unchanged.\n"
    "- name: the canonical STANDARD GENERIC / INN name. Lowercase generics; strip salts/hydrates "
    "(imatinib mesylate -> imatinib); expand abbreviations (5FU/5-FU -> fluorouracil, DOX -> "
    "doxorubicin, AraC -> cytarabine, CDDP -> cisplatin, MTX -> methotrexate, ATRA -> tretinoin); "
    "convert trade names to generic (Gleevec -> imatinib, Taxol -> paclitaxel, Velcade -> "
    "bortezomib, Adriamycin -> doxorubicin); unify spellings (Rifampin = rifampicin); fix typos. "
    "For COMBINATIONS ('A + B', 'A and B') standardize each component and join the sorted "
    "components with ' + '. Keep research-tool compounds/codes with no generic as a tidy lowercase "
    "form. Keep herbal / natural-product names as a tidy form. If the value is not a treatment, "
    "set name to the cleaned original.\n"
    "- is_drug: true if it is a real drug / small molecule / compound / biologic / natural-product "
    "treatment; false if it is a control/vehicle (DMSO, PBS, untreated, none, water, mock), a "
    "genetic reagent (siRNA, shRNA, sgRNA, CRISPR, plasmid, overexpression, knockout, "
    "non-targeting), a non-compound label (e.g. 'factors', 'Polyplex'), a bare number, or a "
    "cell-line name.\n"
    "Return one result per input, preserving every exact raw string."
)

SAMPLE_INSTRUCTIONS = (
    "You classify human RNA-seq samples. You receive a JSON array of strings; each is EITHER a full "
    "sample title OR a short 'cell line' field value. For EVERY input string call the emit tool once "
    "with a result object:\n"
    "- raw: the EXACT input string, unchanged.\n"
    "- cell_line: the canonical human cell line name if identifiable (merge formatting variants: "
    "'Hep G2'/'HepG2 cells' -> 'HepG2'; extract from prose: 'colorectal cancer cell line HCT116' -> "
    "'HCT116'; keep genuinely distinct sublines/derivatives). If it is NOT a named cell line, set "
    "cell_line to the SAME value as category (the bucket name).\n"
    "- category: EXACTLY one of " + " | ".join(CATEGORIES) + ". Use 'Cell line' for a real line; "
    "map patient/sample codes (e.g. '14-00613','JNJ001','BRx-50','PDAC060') to 'Patient/Tumor'; "
    "pure numbers / '--' / cryptic plate codes to 'Unknown'.\n"
    "- drug_treated: if the input is a FULL SAMPLE TITLE, 'Drug Treated' when the sample was exposed "
    "to a drug / compound / small molecule / inhibitor; 'Not Drug Treated' for a clear control / "
    "vehicle / untreated / time-only sample or a genetic perturbation (KO/siRNA/shRNA/CRISPR/"
    "overexpression); and 'Undetermined' when the title does NOT make it clear either way (ambiguous, "
    "or no treatment information in the title). If the input is a short cell-line field value (not a "
    "full title), use 'N/A'.\n"
    "Return one result per input, preserving every exact raw string."
)

COMPOUND_TOOL = {
    "name": "emit_compounds",
    "description": "Return the canonicalization for every input compound string.",
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
                    "required": ["raw", "name", "is_drug"],
                    "properties": {
                        "raw": {"type": "string"},
                        "name": {"type": "string"},
                        "is_drug": {"type": "boolean"},
                    },
                },
            }
        },
    },
}

SAMPLE_TOOL = {
    "name": "emit_samples",
    "description": "Return cell line, category, and drug-treated status for every input string.",
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
                    "required": ["raw", "cell_line", "category", "drug_treated"],
                    "properties": {
                        "raw": {"type": "string"},
                        "cell_line": {"type": "string"},
                        "category": {"type": "string", "enum": CATEGORIES},
                        "drug_treated": {"type": "string",
                                         "enum": ["Drug Treated", "Not Drug Treated",
                                                  "Undetermined", "N/A"]},
                    },
                },
            }
        },
    },
}

PASSES = {
    "compounds": dict(in_dir="compound_batches", out_dir="compound_results", prefix="cmpd",
                      instr=COMPOUND_INSTRUCTIONS, tool=COMPOUND_TOOL, no_reasoning=True,
                      pivot=lambda r: {"name": r["name"], "is_drug": r["is_drug"]}),
    "samples": dict(in_dir="sample_batches", out_dir="sample_results", prefix="samp",
                    instr=SAMPLE_INSTRUCTIONS, tool=SAMPLE_TOOL,
                    pivot=lambda r: {"cell_line": r["cell_line"], "category": r["category"],
                                     "drug_treated": r["drug_treated"]}),
}


def _result_ok(path):
    if not os.path.exists(path):
        return False
    try:
        return bool(json.load(open(path, encoding="utf-8")))
    except Exception:
        return False


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, path)


def _unknown_value(pass_name, raw):
    """The placeholder for a sample/compound the model couldn't classify. samples -> Unknown (these fall
    out of the cell-line deep-dive, which picks a real line); compounds -> the cleaned raw, not-a-drug."""
    if pass_name == "samples":
        return {"cell_line": "Unknown", "category": "Unknown", "drug_treated": "Undetermined"}
    return {"name": (raw or "").strip(), "is_drug": False}


def _merge_unknown(pass_name, items, have, stats=None):
    """Keep every item the model DID classify (`have`); Unknown-fill the rest. The output map covers EVERY
    input item. Returns (out, kept, dropped); accumulates counts into `stats` if given."""
    out, kept, dropped = {}, 0, 0
    for it in items:
        if not isinstance(it, str):
            continue
        v = have.get(it)
        if v:
            out[it], kept = v, kept + 1
        else:
            out[it], dropped = _unknown_value(pass_name, it), dropped + 1
    if stats is not None:
        stats["kept"] = stats.get("kept", 0) + kept
        stats["dropped"] = stats.get("dropped", 0) + dropped
    return out, kept, dropped


def _drop_fill(pass_name, in_path, out_path):
    """Unknown-fill an ENTIRE batch the provider couldn't answer, so the stage completes and the run
    continues instead of failing. Recorded in ai_work/dropped_batches.json by the caller."""
    try:
        with open(in_path, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        items = []
    out, _, _ = _merge_unknown(pass_name, items, {})
    _atomic_write(out_path, out)
    return len(out)


def _record_drops(P, pass_name, names):
    """Append the dropped batch names to ai_work/dropped_batches.json (audit trail of what was skipped)."""
    path = os.path.join(P.work_dir, "dropped_batches.json")
    try:
        prev = json.load(open(path, encoding="utf-8"))
        if not isinstance(prev, list):
            prev = []
    except Exception:
        prev = []
    prev.append({"pass": pass_name, "dropped": list(names)})
    _atomic_write(path, prev)


def drop_fill_missing(pass_name, P):
    """Unknown-fill any batch outputs still MISSING for a pass, so merge sees a COMPLETE set: every
    already-answered batch is kept, only the unanswered ones become Unknown. Returns the count drop-filled.
    Used by the mid-run 'turn off AI' path (provider died after hours of work): completing the pass this way
    PRESERVES the batches already done instead of discarding the whole pass."""
    spec = PASSES[pass_name]
    in_dir = os.path.join(P.work_dir, spec["in_dir"])
    out_dir = os.path.join(P.work_dir, spec["out_dir"])
    os.makedirs(out_dir, exist_ok=True)
    batches = sorted(glob.glob(os.path.join(in_dir, f'{spec["prefix"]}_*.json')))
    missing = [b for b in batches if not _result_ok(os.path.join(out_dir, os.path.basename(b)))]
    for b in missing:
        _drop_fill(pass_name, b, os.path.join(out_dir, os.path.basename(b)))
    if missing:
        _record_drops(P, pass_name, [os.path.basename(b) for b in missing])
        print(f"[AI:{pass_name}] drop-filled {len(missing)}/{len(batches)} unanswered batch(es) as Unknown "
              f"(AI turned off mid-pass) — {len(batches) - len(missing)} answered batch(es) kept.")
    return len(missing)


def _backoff(attempt):
    """Exponential backoff with EQUAL JITTER so many units that failed together don't all re-submit at the
    same instant (a synchronized retry just re-creates the burst that caused the empties)."""
    cap = min(2 ** attempt, 30)
    return cap / 2 + random.uniform(0, cap / 2)


def _log_cost(cost_log, spec, label, provider, model, items, out, usage):
    try:
        with open(cost_log, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "pass": spec["prefix"], "batch": label, "provider": provider, "model": model,
                "n_in": len(items), "n_out": len(out),
                "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read", 0), "cache_creation": usage.get("cache_creation", 0),
            }) + "\n")
    except Exception:
        pass


async def _classify_call(client, provider, spec, items, model, max_tokens, disable_reasoning):
    """ONE provider call -> a pivoted {raw: value} map (no completeness check — the caller decides). API
    errors propagate so the caller's retry / hard-fail logic can act on them."""
    results, usage = await llm_providers.classify(
        client, provider, model, spec["instr"], items, spec["tool"], max_tokens,
        disable_reasoning=disable_reasoning)
    out = {}
    for r in (results or []):
        if isinstance(r, dict) and r.get("raw"):
            try:
                out[r["raw"]] = spec["pivot"](r)
            except Exception:
                pass            # skip a malformed result rather than failing the whole unit
    return out, usage


async def _process_unit(client, provider, spec, pass_name, items, model, max_tokens, disable_reasoning,
                        cost_log, label, stats):
    """Classify `items`, returning a COMPLETE {raw: value} map (one entry per input). This is where the
    'returns nothing / incomplete' root causes are actually handled:
      • transient hiccups -> retry with jittered backoff;
      • an EMPTY reply from a reasoning model -> auto-resend with reasoning OFF (its chain-of-thought is the
        usual cause of an empty/truncated tool call), reverted if the endpoint rejects that body;
      • a persistently PARTIAL reply -> SPLIT the unit in half and recurse (a smaller request is far less
        likely to truncate — the real fix for big-batch incompleteness);
      • at the floor size -> SALVAGE: keep every item the model classified, Unknown-fill only the rest.
    Raises _HardFail only if the PROVIDER itself keeps erroring, so the caller can leave the batch for
    --resume instead of fabricating Unknowns for a whole dead-provider pass."""
    best = {}
    dr = bool(disable_reasoning)
    api_errors = rate_attempts = incompletes = 0
    just_flipped = False
    while True:
        try:
            out, usage = await _classify_call(client, provider, spec, items, model, max_tokens, dr)
        except Exception as e:
            if just_flipped:                       # the reasoning-off body may be unsupported -> revert it
                dr, just_flipped = bool(disable_reasoning), False
                continue
            if llm_providers.classify_ai_error(e).get("category") == "rate":
                rate_attempts += 1
                if rate_attempts > RATE_MAX_ATTEMPTS:
                    raise _HardFail(str(e))
                await asyncio.sleep(RATE_RETRY_SECS)
                continue
            api_errors += 1
            if api_errors >= BATCH_MAX_ATTEMPTS:
                raise _HardFail(str(e))
            await asyncio.sleep(_backoff(api_errors))
            continue
        just_flipped = False
        if len(out) > len(best):
            best = out
        covered = sum(1 for it in items if isinstance(it, str) and it in out)
        if items and covered >= len(items) * COVERAGE_OK:
            _log_cost(cost_log, spec, label, provider, model, items, out, usage)
            return out
        if not out and not dr and provider in ("openai", "gemini", "ollama"):
            dr, just_flipped = True, True          # empty reply -> try again with reasoning OFF
            continue
        incompletes += 1
        if incompletes >= INCOMPLETE_RETRIES:
            break
        await asyncio.sleep(_backoff(incompletes))
    # couldn't get a complete map at this size: SPLIT to beat truncation, else SALVAGE the residue
    if len(items) > SPLIT_FLOOR:
        mid = len(items) // 2
        left = await _process_unit(client, provider, spec, pass_name, items[:mid], model, max_tokens,
                                   disable_reasoning, cost_log, label + "a", stats)
        right = await _process_unit(client, provider, spec, pass_name, items[mid:], model, max_tokens,
                                    disable_reasoning, cost_log, label + "b", stats)
        return {**left, **right}
    merged, _, _ = _merge_unknown(pass_name, items, best, stats)
    return merged


def _load_items(path):
    """A batch's input strings, or None if the batch file is unreadable."""
    try:
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return None
    return [it for it in items if isinstance(it, str)] if isinstance(items, list) else None


def _load_result(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _resume_plan(batches, out_dir, prefix):
    """Which batches still need the model -> (todo, n_rebuilt). A result counts only if it covers EVERY string now
    in its batch. Batches are numbered, not content-addressed, so when PREP re-chunks a changed vocabulary (e.g.
    after a normalize_v2 fix turned the key 'treated by' into 'mitoxantrone') a stale cmpd_005 result used to be
    taken as done and the new strings never reached the model. A batch whose strings were ALL answered before (in
    any earlier result file) is rebuilt from those answers without a model call; only batches holding new strings
    are sent."""
    prior = {}
    for p in sorted(glob.glob(os.path.join(out_dir, f"{prefix}_*.json"))):
        prior.update(_load_result(p))
    todo, rebuilt = [], 0
    for b in batches:
        out_path = os.path.join(out_dir, os.path.basename(b))
        items = _load_items(b)
        if items is None:                                   # unreadable batch: the old existence-only rule
            if not _result_ok(out_path):
                todo.append(b)
            continue
        have = _load_result(out_path)
        if have and all(it in have for it in items):
            continue
        if items and all(it in prior for it in items):
            _atomic_write(out_path, {it: prior[it] for it in items})
            rebuilt += 1
            continue
        todo.append(b)
    return todo, rebuilt


async def _run_pass_async(pass_name, P, cfg, reporter=NULL):
    spec = PASSES[pass_name]
    provider = llm_providers.normalize_provider(cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, cfg.get("model"))
    concurrency = max(1, int(cfg.get("concurrency", 8) or 8))   # no provider clamp: the caller's value is used as-is
    in_dir = os.path.join(P.work_dir, spec["in_dir"])
    out_dir = os.path.join(P.work_dir, spec["out_dir"])
    os.makedirs(out_dir, exist_ok=True)
    batches = sorted(glob.glob(os.path.join(in_dir, f'{spec["prefix"]}_*.json')))
    todo, rebuilt = _resume_plan(batches, out_dir, spec["prefix"])
    print(f"[AI:{pass_name}] {len(batches)} batches, {len(todo)} to do "
          + (f"({rebuilt} rebuilt from earlier answers after PREP re-chunked them) " if rebuilt else "")
          + f"(provider={provider}, model={model}, concurrency={concurrency})")
    # progress: total = all batches; pre-credit any already-complete (resume)
    reporter.set_total(len(batches))
    if len(batches) - len(todo):
        reporter.advance(len(batches) - len(todo))
    reporter.set_detail(f"{len(todo)} of {len(batches)} batches to process")
    if not todo:
        return {"done": len(batches), "ran": 0}

    client = llm_providers.make_client(provider, cfg.get("max_retries", 8),
                                       base_url=cfg.get("base_url"))
    sem = asyncio.Semaphore(concurrency)
    completed = [len(batches) - len(todo)]
    salvaged = {}                                  # basename -> # of samples Unknown-filled (partial recovery)
    max_tokens = int(cfg.get("max_tokens", 60000) or 60000)
    # Compound CANONICALIZATION is a knowledge lookup, not a reasoning task: force reasoning off for that
    # pass (per-pass `no_reasoning`) so gpt-oss/MiMo don't burn ~70% of the output budget on chain-of-thought
    # (the slow part at a fixed tok/s) and the truncation->sequential-split it caused. Global disable_reasoning
    # still applies to every pass.
    disable_reasoning = bool(cfg.get("disable_reasoning", False)) or bool(spec.get("no_reasoning"))

    async def worker(b):
        async with sem:
            base = os.path.basename(b)
            out_path = os.path.join(out_dir, base)
            try:
                with open(b, encoding="utf-8") as f:
                    items = json.load(f)
            except Exception as e:
                print(f"  ! {base} unreadable: {e}")
                items = []
            stats = {"kept": 0, "dropped": 0}
            try:
                out = await _process_unit(client, provider, spec, pass_name, items, model, max_tokens,
                                          disable_reasoning, P.cost_log, base, stats)
                _atomic_write(out_path, out)
                if stats["dropped"]:
                    salvaged[base] = stats["dropped"]
                    print(f"  · {base}: kept {len(out) - stats['dropped']} samples, {stats['dropped']} "
                          f"unresolved -> Unknown (split/salvaged)")
            except _HardFail as e:
                # the PROVIDER couldn't answer this batch at all -> leave it missing; the final block drops
                # the few stragglers (or RAISES for --resume if too many = a mid-pass outage).
                print(f"  ! {base} provider error after retries (left for resume): {e}")
            completed[0] += 1
            reporter.advance(1)
            reporter.set_detail(f"batch {completed[0]}/{len(batches)}")

    # warm the prompt cache: run the first batch alone, then fan out the rest
    await worker(todo[0])
    if len(todo) > 1:
        await asyncio.gather(*(worker(b) for b in todo[1:]))
    await llm_providers.close_client(client)
    # A batch with no result file HARD-FAILED (the provider couldn't answer it at all — incomplete batches
    # were salvaged + written above). Drop the few stragglers so the run continues, but if so MANY failed
    # that the provider is clearly down mid-pass, RAISE so --resume retries them rather than gut the data.
    missing = [b for b in batches if not _result_ok(os.path.join(out_dir, os.path.basename(b)))]
    if missing:
        names = [os.path.basename(b) for b in missing]
        ceiling = max(DROP_CEILING_MIN, int(len(batches) * DROP_CEILING_FRAC))
        if len(missing) > ceiling:
            raise RuntimeError(
                f"[AI:{pass_name}] {len(missing)}/{len(batches)} batch(es) the provider couldn't answer "
                f"(> {ceiling} — the provider/proxy looks DOWN, not just flaky): "
                f"{', '.join(names[:8])}{' …' if len(names) > 8 else ''}. "
                f"Fix the provider, then resume — only these batches re-run.")
        for b in missing:
            _drop_fill(pass_name, b, os.path.join(out_dir, os.path.basename(b)))
        _record_drops(P, pass_name, names)
        print(f"[AI:{pass_name}] DROPPED {len(names)}/{len(batches)} batch(es) the provider couldn't answer "
              f"after retries — marked Unknown, run CONTINUES: "
              f"{', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
    unknown_filled = sum(salvaged.values())
    if unknown_filled:
        print(f"[AI:{pass_name}] salvaged {len(salvaged)} partial batch(es): kept every classifiable sample, "
              f"Unknown-filled {unknown_filled} that wouldn't resolve even when split down.")
    print(f"[AI:{pass_name}] complete.")
    return {"done": len(batches), "ran": len(todo), "dropped": len(missing), "unknown_filled": unknown_filled}


def run_pass(pass_name, P, cfg, reporter=NULL):
    """Sync entry point used by the orchestrator."""
    return asyncio.run(_run_pass_async(pass_name, P, cfg, reporter))


async def _preflight_async(provider, model, base_url=None, disable_reasoning=False):
    # a slow CPU-local Ollama can take >25s even for a 1-item probe -> give it room so a healthy local
    # model isn't mis-reported as a bad key/model (cloud providers stay snappy at 30s).
    client = llm_providers.make_client(provider, max_retries=2,
                                       timeout=(180 if provider == "ollama" else 30), base_url=base_url)
    try:
        results, _ = await llm_providers.classify(
            client, provider, model, COMPOUND_INSTRUCTIONS, ["aspirin"], COMPOUND_TOOL, max_tokens=200,
            disable_reasoning=disable_reasoning)
    finally:
        await llm_providers.close_client(client)
    if not results:
        raise RuntimeError("the model returned no structured output "
                           "(it may not support tool/function calling)")


def preflight(cfg):
    """Validate provider + model + key with ONE tiny live call before the batch fan-out.

    Returns None if AI cleaning is usable, else the raised exception (classify it with
    llm_providers.classify_ai_error). A valid-but-throttled provider still passes — a 1-item call is
    fast even when 250-item batches are slow — so this only trips on a real misconfig (bad key/model).
    """
    provider = llm_providers.normalize_provider(cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, cfg.get("model"))
    try:
        asyncio.run(_preflight_async(provider, model, cfg.get("base_url"),
                                     cfg.get("disable_reasoning", False)))
        return None
    except Exception as e:
        return e


def main():
    import argparse
    from pipeline_paths import Paths
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--pass", dest="pass_name", required=True, choices=list(PASSES))
    ap.add_argument("--provider", default="anthropic", choices=list(llm_providers.PROVIDERS))
    ap.add_argument("--model", default=None)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()
    P = Paths(a.run_dir).ensure_dirs()
    run_pass(a.pass_name, P, {"provider": a.provider, "model": a.model, "concurrency": a.concurrency})


if __name__ == "__main__":
    main()
