# -*- coding: utf-8 -*-
"""
Phase B — assign each sample to a USER-DEFINED comparison group for the AltAnalyze (PSI) dPSI test.

Reuses the pipeline's "fixed code first, AI for the remainder" structure (the same shape that powers the
drug-treated classifier), but the TARGET categories come from the user instead of being hardwired:

  1. FIXED pass (deterministic): for each run, match every group's keywords against the run's FULL metadata
     row; the control group also uses normalize_v2.is_control / the compound map. First confident match wins.
  2. AI pass (only the unresolved remainder): one llm_providers.classify call, parametrized by the user's
     group names and fed the ENTIRE metadata row, picks a group or "Unassigned".
  3. Unresolved samples stay blank -> dropped from the comparison by build_groups.sh on the cluster.

ADDITIVE: writes a NEW `group` column into the filtered run table + a group_assignment_audit.csv. Does NOT
touch the existing `drug_treated` column or the splicing-amenable filter.

assign() returns (group_col, labelmap) for psi_deploy.build_psi_bundle, or (None, None) to fall back to the
default treated-vs-control split.
"""
import os
import re
import csv
import json
import asyncio

from progress import NULL
import llm_providers
from normalize_v2 import clean_compound
from runtable_annotate import treatment_columns, pick_treatment, control_like
from build_final import _safe_open
from cellline_match import _slug

_WORD = re.compile(r"[a-z0-9]+")


def _norm_groups(group_cfg):
    """-> list of {name, control(bool), keywords(list[str lowercased])}; the control group sorted first."""
    raw = (group_cfg or {}).get("groups") or []
    out = []
    for g in raw:
        name = str(g.get("name") or "").strip()
        if not name:
            continue
        kws = g.get("keywords") or g.get("match") or []
        if isinstance(kws, str):
            kws = re.split(r"[,;|]", kws)
        kws = [k.strip().lower() for k in kws if str(k).strip()]
        out.append({"name": name, "control": bool(g.get("control")), "keywords": kws})
    # control first (becomes group 1 = baseline); then UI order
    out.sort(key=lambda g: (0 if g["control"] else 1))
    return out


def _row_blob(row):
    """Full metadata row -> one lowercased text blob for keyword matching / the AI."""
    return " ; ".join(f"{k}={v}" for k, v in row.items() if str(v).strip())


def _fixed_assign(row, groups):
    """Deterministic group for one run, or '' if unresolved. First confident match wins (control first)."""
    blob = _row_blob(row).lower()
    # same column discovery + "control only if EVERY treatment value says control" rule as runtable_annotate
    treat = pick_treatment(row, treatment_columns(list(row.keys())), lambda v: False)
    for g in groups:
        if g["control"]:
            if treat and control_like(treat):
                return g["name"]
            c = clean_compound(treat) if treat else None   # vehicle/control -> clean_compound() is None
            if treat and c is None:
                return g["name"]
        for kw in g["keywords"]:
            if kw and kw in blob:
                return g["name"]
    return ""


def _build_tool(names):
    enum = list(names) + ["Unassigned"]
    return {
        "name": "emit_groups",
        "description": "Assign each sample's metadata to exactly one comparison group.",
        "input_schema": {
            "type": "object", "additionalProperties": False, "required": ["results"],
            "properties": {"results": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["raw", "group"],
                "properties": {"raw": {"type": "string"},
                               "group": {"type": "string", "enum": enum}}}}},
        },
    }


def _build_instructions(groups):
    lines = ["You assign human RNA-seq samples to a COMPARISON GROUP from this fixed list, using the "
             "sample's metadata. You receive a JSON array of strings; each is the full metadata of one "
             "sample. For EVERY input string call the emit tool once with a result object:",
             "- raw: the EXACT input string, unchanged.",
             "- group: EXACTLY one of [" + " | ".join([g["name"] for g in groups] + ["Unassigned"]) + "].",
             "Groups:"]
    for g in groups:
        hint = ("; keywords: " + ", ".join(g["keywords"])) if g["keywords"] else ""
        role = " (the control / baseline)" if g["control"] else ""
        lines.append(f"  - {g['name']}{role}{hint}")
    lines.append("Use 'Unassigned' when the metadata does not clearly place the sample in one group. Do "
                 "NOT guess — an honest 'Unassigned' is better than a wrong group. Return one result per "
                 "input, preserving every exact raw string.")
    return "\n".join(lines)


async def _ai_classify(blobs, groups, ai_cfg):
    provider = llm_providers.normalize_provider(ai_cfg.get("provider", "anthropic"))
    model = llm_providers.resolve_model(provider, ai_cfg.get("model"))
    tool = _build_tool([g["name"] for g in groups])
    instr = _build_instructions(groups)
    client = llm_providers.make_client(provider, ai_cfg.get("max_retries", 8), base_url=ai_cfg.get("base_url"))
    try:
        results, _usage = await llm_providers.classify(
            client, provider, model, instr, blobs, tool, ai_cfg.get("max_tokens", 16000),
            disable_reasoning=ai_cfg.get("disable_reasoning", False))
    finally:
        await llm_providers.close_client(client)
    out = {}
    for r in (results or []):
        if isinstance(r, dict) and r.get("raw"):
            g = (r.get("group") or "").strip()
            if g and g != "Unassigned":
                out[r["raw"]] = g
    return out


def _find_filtered_csv(P, slug):
    for cand in (P.runtable_filtered_csv(slug), P.runtable_filtered_csv(slug).replace(".csv", "_v2.csv")):
        if os.path.exists(cand):
            return cand
    return None


def assign(P, cfg, sel, reporter=NULL):
    """Classify each run into a user group; write the `group` column + an audit; return (group_col, labelmap).
    Returns (None, None) if there is no usable group config / run table (caller falls back to default)."""
    groups = _norm_groups(getattr(cfg, "group_cfg", None))
    if len(groups) < 2:
        raise ValueError(f"group_assign: need >=2 comparison groups for AltAnalyze (dPSI needs a baseline + at least one test group), got {len(groups)}")
    slug = _slug((sel or {}).get("canonical", "cellline")) if sel else ""
    src = _find_filtered_csv(P, slug)
    if not src:
        return None, None
    with open(src, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None, None

    # 1) fixed pass
    decided = {}        # row index -> group name
    unresolved = []     # (row index, blob)
    for i, r in enumerate(rows):
        g = _fixed_assign(r, groups)
        if g:
            decided[i] = g
        else:
            unresolved.append((i, _row_blob(r)))
    n_fixed = len(decided)

    # 2) AI pass on the unresolved remainder (deduped by blob), unless AI is off
    n_ai = 0
    if unresolved and not getattr(cfg, "skip_ai", False):
        uniq = sorted({b for _i, b in unresolved})
        ai_cfg = {"provider": getattr(cfg, "provider", "anthropic"), "model": getattr(cfg, "model", None),
                  "base_url": getattr(cfg, "base_url", "") or "", "max_retries": 8, "max_tokens": 16000,
                  "disable_reasoning": getattr(cfg, "disable_reasoning", False)}
        reporter.set_detail(f"AI-classifying {len(uniq)} unresolved samples into your groups…")
        try:
            blob2group = asyncio.run(_ai_classify(uniq, groups, ai_cfg))
        except Exception as e:
            print(f"  GROUPS: AI pass failed ({e}) -> leaving the remainder Unassigned")
            blob2group = {}
        valid = {g["name"] for g in groups}
        for i, b in unresolved:
            g = blob2group.get(b)
            if g in valid:
                decided[i] = g
                n_ai += 1

    if not decided:
        print("  ************************************************************")
        print("  GROUPS WARNING: 0 of %d samples could be assigned to ANY user group (fixed + AI)!" % len(rows))
        print("  GROUPS WARNING: check your group keywords -> falling back to default treated-vs-control")
        print("  ************************************************************")
        return None, None

    # 3) write the `group` column back into the run table (additive) + an audit
    base_cols = [c for c in rows[0].keys() if c != "group"]
    fields = base_cols + ["group"]
    for i, r in enumerate(rows):
        r["group"] = decided.get(i, "")
    out = _safe_open(src)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader()
        for r in rows:
            w.writerow(r)
    n_unassigned = len(rows) - len(decided)
    try:
        with open(_safe_open(os.path.join(P.runtable_dir, "group_assignment_audit.csv")), "w",
                  newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(["row", "group", "method"])
            ai_idx = {i for i, _b in unresolved if i in decided}
            for i, r in enumerate(rows):
                g = decided.get(i, "")
                method = "" if not g else ("ai" if i in ai_idx else "fixed")
                w.writerow([i, g or "Unassigned", method])
    except Exception as e:
        print(f"  GROUPS: WARNING could not write group_assignment_audit.csv ({e})")

    # 4) labelmap: control group -> 1 (baseline); others -> 2..N (UI order). Keys = group NAMES.
    labelmap = {}
    num = 1
    for g in groups:
        labelmap[g["name"]] = (num, _slug(g["name"]))
        num += 1
    print(f"  GROUPS: assigned {len(decided)}/{len(rows)} runs "
          f"(fixed={n_fixed}, ai={n_ai}, unassigned={n_unassigned}) into {len(groups)} groups")
    reporter.set_detail(f"groups: {len(decided)}/{len(rows)} runs assigned ({n_unassigned} unassigned/dropped)")
    return "group", labelmap
