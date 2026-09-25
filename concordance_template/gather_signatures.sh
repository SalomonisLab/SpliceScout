#!/usr/bin/env bash
# =============================================================================
# gather_signatures.sh -- collect the per-drug PSI signatures the PSI stage produced into a clean
# "ref" directory (DRUG_SIG_DIR) for the concordance scorer. The scorer reads EVERY PSI.*.txt in the
# ref dir, so we copy ONLY the differential PSI.<drug>_vs_<control>.txt tables (not the per-sample
# master PSI table, not the event_summary). Idempotent; echoes the count gathered.
# Pure bash + nullglob; NEVER `grep -c` (empty on the compute nodes).
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
set -u; shopt -s nullglob

mkdir -p "$DRUG_SIG_DIR" 2>/dev/null || true

if [ ! -d "$PSI_EVENTS_DIR" ]; then
  echo "[concord] PSI events dir not found: $PSI_EVENTS_DIR" >&2
  echo 0; exit 0
fi

# Refresh: drop any stale scorer outputs that may be sitting in the ref dir from a prior run, so the
# scorer never re-reads them as "signatures" (they don't match PSI.*, but keep the dir pristine anyway).
rm -f "$DRUG_SIG_DIR"/concordance.txt "$DRUG_SIG_DIR"/overlaps-*-direction.txt "$DRUG_SIG_DIR"/pair_stats.tsv 2>/dev/null || true
# ...and the previously gathered signature COPIES (+ earlier quarantine), so a re-gather after the PSI stage was re-run
# never scores a stale contrast that no longer exists upstream. Only copies are removed -- the originals stay in
# PSI_EVENTS_DIR and are re-copied just below.
rm -f "$DRUG_SIG_DIR"/PSI.*.txt "$DRUG_SIG_DIR/../drug_signatures_cross_study"/PSI.*.txt 2>/dev/null || true

n=0
for f in "$PSI_EVENTS_DIR"/PSI.*_vs_*.txt; do
  [ -e "$f" ] || continue
  cp -p "$f" "$DRUG_SIG_DIR"/ 2>/dev/null && n=$((n+1))
done
# The PSI stage's post-completion compression GZIPS these tables (any >= 1MB) -> ALSO accept
# PSI.*_vs_*.txt.gz, decompressing each into the ref dir as plain .txt (the scorer reads plain text).
# Skip one already gathered uncompressed above. WITHOUT this the concordance gathers 0 and the watchdog
# waits forever for "PSI" output that is actually present but compressed (hit LIVE on MDS_L 2026-06-24).
for g in "$PSI_EVENTS_DIR"/PSI.*_vs_*.txt.gz; do
  [ -e "$g" ] || continue
  b="$(basename "${g%.gz}")"
  [ -e "$DRUG_SIG_DIR/$b" ] && continue
  gunzip -c "$g" > "$DRUG_SIG_DIR/$b" 2>/dev/null && n=$((n+1))
done
echo "[concord] gathered $n drug signature(s) from $PSI_EVENTS_DIR -> $DRUG_SIG_DIR" >&2
# --- STUDY-MATCHED CONTRAST FILTER ---------------------------------------------------------------
# A contrast whose treated and control arms come from DIFFERENT GSE studies is batch-dominated
# (median signature 10,046 events vs 2,141 within-study; 45% vs 10% spuriously below the 0.30
# reversal threshold) and, because rankings order by overlap size, monopolises the output.
# Quarantine such contrasts BEFORE the signature count is emitted, then recount.
# Builtins ONLY (no grep/ls/wc): an LSF job can start with a minimal PATH, and an external-command
# failure silently disabled this filter once (0 quarantined where 38 were cross-study), producing a
# plausible fully-populated ranking rather than an error.
_gse_of() {                          # first GSE<digits> token of $1 -> stdout ('' if none)
  case "$1" in *GSE*) ;; *) printf ''; return ;; esac
  _t="${1#*GSE}"; _o=''
  while [ -n "$_t" ]; do
    _c="${_t%"${_t#?}"}"
    case "$_c" in [0-9]) _o="$_o$_c"; _t="${_t#?}" ;; *) break ;; esac
  done
  if [ -n "$_o" ]; then printf 'GSE%s' "$_o"; else printf ''; fi
}
_is_cross() {                        # $1 = signature basename -> exit 0 if it is a CROSS-study contrast
  case "$1" in *_vs_*) ;; *) return 1 ;; esac
  # a MULTISTUDY-pooled arm (psi_deploy labels a baseline pooled across studies that way) is cross-study by
  # construction -- before, such pooled '..._vs_control' contrasts carried no GSE and slipped through
  case "$1" in *MULTISTUDY*) return 0 ;; esac
  _g1="$(_gse_of "${1%%_vs_*}")"
  _g2="$(_gse_of "${1#*_vs_}")"
  [ -n "$_g1" ] && [ -n "$_g2" ] && [ "$_g1" != "$_g2" ]
}
if [ "${STUDY_MATCHED_ONLY:-1}" = "1" ]; then
  _quar="$DRUG_SIG_DIR/../drug_signatures_cross_study"
  mkdir -p "$_quar" 2>/dev/null
  _x=0
  for _f in "$DRUG_SIG_DIR"/PSI.*.txt; do
    [ -e "$_f" ] || continue
    _b="${_f##*/}"
    if _is_cross "$_b"; then
      mv -f "$_f" "$_quar/" 2>/dev/null && _x=$((_x+1))
    fi
  done
  n=0; _leak=0
  for _f in "$DRUG_SIG_DIR"/PSI.*.txt; do
    [ -e "$_f" ] || continue
    n=$((n+1)); _b="${_f##*/}"
    _is_cross "$_b" && _leak=$((_leak+1))
  done
  echo "[concord] study-matched filter: quarantined $_x cross-study contrast(s); $n retained" >&2
  if [ "$_leak" -gt 0 ]; then
    echo "[concord] WARNING: $_leak cross-study contrast(s) SURVIVED the filter -- rankings are batch-confounded" >&2
  fi
fi
# --- end STUDY-MATCHED CONTRAST FILTER -----------------------------------------------------------
# --- REAGENT FILTER ------------------------------------------------------------------------------
# A contrast whose "drug" arm is a LAB REAGENT, not a treatment: metabolic RNA labels (4sU/4-thiouridine, 5-EU,
# BrU, IdU, EdU, BrdU), inducible-expression switches (doxycycline/dox, tetracycline), degron tags (dTAG, auxin/IAA,
# 5-Ph-IAA, Shield-1) and selection antibiotics (puromycin, blasticidin, G418, hygromycin, zeocin). Its signature
# is the experimental SYSTEM, not a drug (K562 2026-09: 4-thiouridine, dox induction and dTAG-7 ranked among the
# top 'AML reversers'). Matched as whole tokens of the drug arm, so doxorubicin etc. are untouched.
# EXCLUDE_REAGENTS=0 keeps them. Builtins only (bash >= 4 lowercase expansion; no tr/grep on minimal PATHs).
if [ "${EXCLUDE_REAGENTS:-1}" = "1" ]; then
  _rq="$DRUG_SIG_DIR/../drug_signatures_reagents"
  mkdir -p "$_rq" 2>/dev/null
  rm -f "$_rq"/PSI.*.txt 2>/dev/null
  _rre='[._-](4-?thiouridine|4su|5-?eu|5-?ethynyluridine|bru|bromouridine|5-?iododeoxyuridine|idu|edu|brdu|doxycycline|dox|tetracycline|dtag|dtag-?[0-9]+|dtagv-?1|auxin|iaa|5-?ph-?iaa|shield-?1|puromycin|blasticidin|g418|geneticin|hygromycin|zeocin)[._-]'
  _x=0
  for _f in "$DRUG_SIG_DIR"/PSI.*.txt; do
    [ -e "$_f" ] || continue
    _b="${_f##*/}"; _arm="${_b#PSI.}"; _arm="${_arm%%_vs_*}"; _arm="_${_arm,,}_"
    if [[ "$_arm" =~ $_rre ]]; then
      mv -f "$_f" "$_rq/" 2>/dev/null && _x=$((_x+1))
    fi
  done
  n=0
  for _f in "$DRUG_SIG_DIR"/PSI.*.txt; do [ -e "$_f" ] && n=$((n+1)); done
  echo "[concord] reagent filter: excluded $_x lab-reagent contrast(s) -> $_rq; $n retained" >&2
fi
# --- end REAGENT FILTER --------------------------------------------------------------------------
echo "$n"
