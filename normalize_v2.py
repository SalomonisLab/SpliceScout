"""Improved structural normalizer for treatment-field values (no drug-name guessing).
Collapses dose / duration / replicate / condition tokens (underscore-, space-, comma-,
or paren-delimited) so the same drug at multiple doses counts once. Keeps synonym
parens like (GDC-0941). Used by build_final.py."""
import re

NUM = r"\d+\.?\d*"
UNIT = (r"(?:[unmpµμ]m|[unmpµμ]g/?m?l?|mg/?m?l?|ng/?m?l?|g/ml|microg/?m?l?|"
        r"microm(?:olar)?|nanomolar|millimolar|mg/kg|percent|%)")

CONTROL_VALUES = {
    "dmso", "vehicle", "control", "untreated", "none", "no treatment", "no drug",
    "pbs", "water", "mock", "ctrl", "na", "n/a", "missing", "not applicable",
    "unstimulated", "untreated control", "no", "-", "--", "vehicle control",
    "negative control", "ethanol", "etoh", "wild type", "wt", "parental",
    "nt", "baseline", "pre-treatment", "pretreatment", "pre treatment",
}

# Strong "no real agent" tokens: if ANY appears as a token in the value (split on non-alphanumerics),
# it's a control arm even when joined to an experiment-model prefix -- e.g. "SARS2_Mock",
# "mock-infected", "EV_untreated", "shCtrl_uninfected". These words NEVER name a real drug. Solvent
# words (DMSO/ethanol/PBS/water/vehicle) are deliberately NOT here -- they can be a drug's carrier
# ("DrugX in 0.1% DMSO"), so they stay residue-based in is_control() below to avoid false positives.
STRONG_CONTROL_TOKENS = {
    "mock", "uninfected", "noninfected", "sham", "naive", "untreated", "unstimulated",
    "untransfected", "nontransfected", "parental", "nontreated", "unexposed", "nonexposed",
}

# Framing words that describe HOW an agent was applied, never WHAT it was -- stripped before deciding a
# value is a control, so "Exposure to DMSO for 48 hours" / "cells treated with vehicle" are controls like
# "DMSO". (Nouns such as drug/compound/inhibitor are deliberately NOT here: a bare "inhibitor" must not
# become a control.)
_FRAMING_WORDS = (r"\b(?:treat|treated|treatment|treatments|treating|expos(?:ed|ure)|incubat(?:ed|ion)|"
                  r"administ(?:ered|ration)|stimulat(?:ed|ion)|culture[sd]?|cultivated|grown|cells?|"
                  r"with|by|using|of|to|for|in|at|only|alone|condition|arm|group|sample|samples)\b")
# after stripping framing, a lone negation means "no agent": "Not treated", "non-treated", "without treatment"
_NEGATION_RESIDUE = {"not", "non", "no", "none", "without", "nil", "un"}
# a real control word -- framing words only complete a control when one of these is present (see is_control)
_CONTROL_ANCHOR = re.compile(r"dimethyl\s*sulfoxide|\bdmso\b|\bvehicle\b|\bethanol\b|\betoh\b|\bpbs\b|\bwater\b|"
                             r"\b(?:dd|d)?h2o\b|\bcontrols?\b|\bctrls?\b|\bmedi(?:um|a)\b|\bsaline\b", re.I)
# zero-dose arm of a dose series ("0 uM", "Erlotinib 0 nM", "0µM drug"): a vehicle control, not a drug
# condition -- the manuscript's "0 μM vehicle arm" was being scored as a compound signature.
_DOSE_TOK = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:[nuµμmp]m|nm|um|mm|[nuµμm]g\s*/\s*m?l|mg\s*/\s*kg|"
                       r"micro(?:m(?:olar)?|g)|nanomolar|millimolar)(?![a-z])", re.I)


def _zero_dose_only(v):
    """True if the value carries dose tokens and EVERY one of them is zero."""
    doses = [float(m.group(1)) for m in _DOSE_TOK.finditer(v or "")]
    return bool(doses) and all(d == 0 for d in doses)


# time-ZERO arm of a time course ("1mM dbcAMP_0h", "Day 0", "0 min"): harvested before the agent could act, i.e.
# the study's own baseline. Without this, a time-course study whose only reference is its 0 h arm had NO controls,
# so every later timepoint could only be compared cross-study -> quarantined -> the whole study vanished.
# Not when the zero is a recovery/washout time AFTER treatment ("10 uM, 0 h recovery" is a treated sample).
_TIME_TOK = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours|min|mins|minute|minutes|d|day|days)(?![a-z])"
                       r"|\b(?:day|d)\s*(\d+)\b", re.I)
_AFTER_WORDS = re.compile(r"\b(?:recover(?:y|ed)?|wash[\s-]?out|washed|post|after|release[d]?|chase|withdraw\w*)\b", re.I)


def _zero_time_only(v):
    """True if the value carries time tokens, EVERY one is zero, and none is a post-treatment recovery time."""
    times = [float(m.group(1) or m.group(2)) for m in _TIME_TOK.finditer(v or "")]
    return bool(times) and all(t == 0 for t in times) and not _AFTER_WORDS.search(v or "")


# multi-clause / combination markers -> value is a sentence, not a single compound.
# For these we strip ONLY safe trailing tokens (no greedy mid-string removal) and
# otherwise leave the text for AI Pass A, so we never mangle a drug name.
_COMPLEX = re.compile(r"(\s\+\s|\sand\s|\swith\b|\sor\s|followed|transfect|"
                      r"cocultur|co-cultur|supplement|grown in|:|;|/kg|non-demult)", re.I)

# HOW-it-was-applied framing IN FRONT of the agent ("treated by 50 nM mitoxantrone", "Treated with interferon-Beta",
# "Treated; 1.0uM PG", "treatment: erlotinib", "Exposure to rutaecarpin", "cells were treated with X"). Only with a
# connector word or punctuation after it, so an arm label such as "Treatment A" is left alone. Left in place, the
# dose strip below cut "treated by 50 nM mitoxantrone for 48 hours" down to "treated by" -- the AI compound pass
# only ever sees this cleaned key, could not name a drug, and whole A549 drug studies (GSE185207, GSE185209)
# were labelled Not Drug Treated.
_FRAMING_VERB = r"(?:treated|treatment|treatments|exposed|exposure|incubated|incubation|stimulated|stimulation|" \
                r"administered|dosed)"
_LEAD_FRAMING = re.compile(r"^(?:(?:the\s+)?cells?\s+(?:were\s+|was\s+)?)?" + _FRAMING_VERB +
                           r"(?:\s+(?:with|by|to|in|using|of)\b|\s*[:;,])\s*", re.I)
# a dose-splitting head that ENDS in framing ("A549 treated by | 50 nM X"): the agent is after the dose
_HEAD_FRAMED = re.compile(r"\b" + _FRAMING_VERB + r"(?:\s+(?:with|by|to|in|using|of))?\s*[:;,]?\s*$", re.I)
_DUR_UNIT = r"(?:h|hr|hrs|hours?|min|mins|minutes?|d|days?|wk|wks|weeks?|months?)"
_TRAIL_FOR_TIME = re.compile(r"[\s,;]+(?:for|during|over)\s+" + NUM + r"\s*" + _DUR_UNIT + r"\.?\s*$", re.I)
_LEAD_TIME = re.compile(r"^" + NUM + r"[_\s]*" + _DUR_UNIT + r"[_,\s]+", re.I)   # "1 week 50nM CFI-400945"


def _strip_framing(v):
    """Leading framing, a trailing 'for <duration>' and a leading duration -- never down to nothing."""
    for rx in (_LEAD_FRAMING, _LEAD_FRAMING, _TRAIL_FOR_TIME, _LEAD_TIME):
        s = rx.sub("", v, count=1).strip().strip(",;").strip()
        if s:
            v = s
    return v


# ENCODE-style treatment RECORDS ('treatment_term_id: CHEBI:52717; treatment_term_name: Bortezomib; treatment_type:
# chemical; duration: 12; duration_units: hour; temperature: 37; ...' -- the attribute ENCODE-derived K562/HepG2 series
# carry). Read whole, the record became the compound NAME: K562 GSE127062 (2026-09) had 48 such 'compounds', every
# duration its own name, and its DMSO arms never counted as controls.
_ENC_NAME = re.compile(r"treatment_term_name\s*:\s*([^;]+)", re.I)
_ENC_AMOUNT = re.compile(r"(?<![a-z_])amount\s*:\s*([0-9.]+)\s*;\s*amount_units\s*:\s*([^;]+)", re.I)
_ENC_DURATION = re.compile(r"(?<![a-z_])duration\s*:\s*([0-9.]+)\s*;\s*duration_units\s*:\s*([^;]+)", re.I)


def flatten_structured_treatment(val):
    """An ENCODE treatment record -> the free text the rest of the parser reads: '[<amount> <unit> ]<name>[ + <name2>]
    [<duration> <unit>]' ('Bortezomib 12 hour', 'DMSO 48 hour'). Any other value is returned unchanged."""
    if not val or "treatment_term_name" not in val.lower():
        return val
    names = [n.strip() for n in _ENC_NAME.findall(val) if n.strip()]
    if not names:
        return val
    out = " + ".join(dict.fromkeys(names))
    a = _ENC_AMOUNT.search(val)
    if a:
        out = "%s %s %s" % (a.group(1), a.group(2).strip(), out)
    d = _ENC_DURATION.search(val)
    if d:
        out = "%s %s %s" % (out, d.group(1), d.group(2).strip())
    return out


def normalize_compound(val):
    v = _strip_framing(flatten_structured_treatment((val or "").strip()).strip(",;"))
    complex_val = bool(_COMPLEX.search(v)) or len(v) > 48

    # trailing dose inside parens, e.g. '(100 mg/kg P.O.)'  (synonym parens kept)
    v = re.sub(r"\s*\(\s*" + NUM + r"\s*" + UNIT + r"[^)]*\)\s*$", "", v, flags=re.I)
    # trailing replicate / condition / batch tags (repeat a few times)
    for _ in range(4):
        v = re.sub(r"[_,\s]+(?:rep|replicate|con|cond|condition|batch|set|donor|day)"
                   r"\s*\d+\b\.?$", "", v, flags=re.I)
    # trailing descriptor words
    v = re.sub(r"[\s_]+(?:drug|treated|treatment|exposure)\b\.?\s*$", "", v, flags=re.I)
    # trailing timepoint ("dbcAMP_24h", "Nutlin 8 hr", "X_2d"): the same agent at another time is the same
    # compound (it used to count dbcAMP_6h / _12h / _24h as three compounds without the AI map)
    for _ in range(2):
        v = re.sub(r"[_,\s]+" + NUM + r"\s*(?:h|hr|hrs|hour|hours|min|mins|minutes?|d|days?|wk|wks|weeks?)\.?$",
                   "", v, flags=re.I)

    if complex_val:
        # only strip a dose sitting at the very end; never eat mid-string text
        v = re.sub(r"[_,\s]+" + NUM + r"[_\s]*" + UNIT + r"\s*$", "", v, flags=re.I)
        return v.strip().strip(",;_").strip()

    # simple single-compound value: full normalization. A dose normally FOLLOWS the agent ("Erlotinib 10 uM, 24h")
    # and everything from it on is noise -- unless only framing precedes it ("A549 treated by 50 nM X"): then the
    # agent is AFTER the dose, so keep that side instead of the framing.
    m = re.search(r"[_,\s]+" + NUM + r"[_\s]*" + UNIT + r"\b", v, flags=re.I)
    if m:
        head, tail = v[:m.start()], v[m.end():]
        framed = _HEAD_FRAMED.search(head) or not re.sub(_FRAMING_WORDS + r"|[\W_\d]+", "", head, flags=re.I)
        v = tail if (framed and re.search(r"[A-Za-z]{2}", tail)) else head
    v = re.sub(r"^" + NUM + r"[_\s]*" + UNIT + r"[_\s]+(?:of\s+)?", "", v, flags=re.I)   # "10 uM of cisplatin"
    v = re.sub(r"^" + NUM + r"\s*(?:h|hr|hrs|hours|d|day|days)[_\s]+", "", v, flags=re.I)
    return v.strip().strip(",;_").strip()


def is_control(val):
    v = flatten_structured_treatment((val or "").strip()).lower()
    if v in CONTROL_VALUES:
        return True
    # strong no-agent token anywhere in the value (handles underscore/hyphen-joined model prefixes
    # like "SARS2_Mock" / "mock-infected" that the residue logic below misses on non-space separators)
    if set(re.split(r"[^a-z0-9]+", v)) & STRONG_CONTROL_TOKENS:
        return True
    if _zero_dose_only(v) or _zero_time_only(v):
        return True
    # "no inhibitor" / "without drug" / "minus compound": a negated generic agent noun is the no-agent arm
    if re.fullmatch(r"(?:no|without|w/o|minus)\s+(?:drugs?|compounds?|inhibitors?|agents?|treatments?|stimul\w*|"
                    r"additives?|small\s+molecules?)", v):
        return True
    # "_" is a regex WORD char, so \bdmso\b / \bvehicle\b never matched "DMSO_24h" / "vehicle_control" (common
    # GEO spellings) -> treat underscores as spaces before the word-bounded solvent/framing strips.
    residue = re.sub(r"_+", " ", v)
    for pat in (r"dimethyl\s*sulfoxide", r"\bdmso\b", r"\bv/v\b", r"\bvehicle\b",
                r"\bethanol\b", r"\betoh\b", r"\bpbs\b", r"\bwater\b", r"\b(?:dd|d)?h2o\b",
                r"\bcontrols?\b", r"\bctrls?\b",
                # plain culture medium is the no-agent arm ("medium", "normal growth medium"); a CONDITIONED or
                # serum-free medium leaves a residue ("conditioned", "serumfree") and so is NOT a control
                r"\b(?:normal|complete|fresh|basal|regular|standard|growth|culture)\b(?=.*\b(?:medi(?:um|a)|saline)\b)",
                r"\bmedi(?:um|a)\b", r"\bsaline\b",     # AFTER the qualifier strip above (it looks ahead for them)
                r"\bincubation\b", r"\d+\.?\d*\s*%", r"\d+\.?\d*\s*h(?:rs?|ours?)?\b",
                r"\d+\.?\d*\s*(?:d|days?|min|mins|minutes?|wks?|weeks?)\b",
                r"(?<![\w.])\d+\s*[:/]\s*\d+(?![\w.])",                         # a dilution: "1/1000 DMSO", "1:1000"
                r"\b(?:rep|replicate|biorep|batch|set)\s*#?\s*\d+\b",           # "DMSO_rep1", "vehicle batch 2"
                r"\(.*?\)"):
        residue = re.sub(pat, "", residue, flags=re.I)
    punct = r"[\(\),/.%\s_\-]+"
    if re.sub(punct, "", residue) == "":
        return True
    # Framing words only complete a control when a control word was actually there ("Exposure to DMSO for 48 hours",
    # "cells treated with vehicle") or a negation is all that is left ("Not treated", "without treatment"). A bare
    # "treated" / "stimulated" / "Treated 24h" is the TREATED arm, not a control.
    framed = re.sub(punct, "", re.sub(_FRAMING_WORDS, " ", residue, flags=re.I))
    if framed == "" and _CONTROL_ANCHOR.search(re.sub(r"_+", " ", v)):
        return True
    return framed in _NEGATION_RESIDUE


# A value that says only THAT something was applied, never WHAT ('treated: yes', 'drug: +'): no agent name.
_NO_AGENT_VALUE = re.compile(r"\s*(?:yes|y|true|\+|positive|treated|treatment|drug|compound|agent)\s*", re.I)


def clean_compound(val):
    """Return canonical-ish name, or None if it's a control / empty / names no agent ('yes', 'treated')."""
    val = flatten_structured_treatment(val)
    if _zero_dose_only(val) or _zero_time_only(val):   # "Erlotinib 0 uM" / "dbcAMP_0h" are the study's baseline
        return None
    if _NO_AGENT_VALUE.fullmatch(val or ""):
        return None
    n = normalize_compound(val)
    if not n or is_control(n):
        return None
    return n
# Signed Nicholas Krol
