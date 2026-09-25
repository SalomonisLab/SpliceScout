#!/usr/bin/env bash
# =============================================================================
# run_concordance_job.sh -- the SINGLE concordance job (LSF job body). Gathers the per-drug PSI
# signatures, then for EACH cancer atlas in queries.tsv runs the vendored scorer
# (splicingConcordance_advanced.py) and the ranker (rank_concordance.py), writing
#   RESULTS_DIR/<atlas>/concordance.txt  + overlaps-*-direction.txt  + ranked_concordance_summary.txt
#   * idempotent: skips an atlas whose concordance.txt already exists; skips entirely if all done
#   * verifies at least one concordance.txt landed (else exits non-zero so the watchdog resubmits)
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_concordance.sh"
set -u; shopt -s nullglob

concord_load_modules
command -v python >/dev/null 2>&1 || { echo "[concord] python (2.7) not on PATH" >&2; exit 1; }
mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$DRUG_SIG_DIR" "$RESULTS_DIR" || { echo "[concord] cannot mkdir dirs" >&2; exit 1; }

[ -s "$CONCORD_SCRIPT" ] || { echo "[concord] scorer not found at $CONCORD_SCRIPT" >&2; exit 1; }
[ -s "$QUERIES_FILE" ]   || { echo "[concord] no queries.tsv shipped -- nothing to score" >&2; exit 1; }
# the vendored scorer imports AltAnalyze's export/UI/unique -> put the resolved install on PYTHONPATH
for _m in export.py UI.py unique.py; do
  [ -s "$ALTANALYZE_HOME/$_m" ] || { echo "[concord] $_m missing under ALTANALYZE_HOME=$ALTANALYZE_HOME (the scorer needs it)" >&2; exit 1; }
done
export PYTHONPATH="$ALTANALYZE_HOME:${PYTHONPATH:-}"

# idempotent: all atlases already scored -> nothing to do
if concord_done && [ ! -f "$PIPELINE_ROOT/.force_rescore" ]; then
  # still skip only if EVERY listed atlas has a result (a partially-done set should finish the rest)
  _alldone=1
  while IFS=$'\t' read -r qname qdir ckind cpath || [ -n "${qname:-}" ]; do
    case "${qname:-}" in ''|'#'*) continue ;; esac
    [ -s "$RESULTS_DIR/$qname/concordance.txt" ] || { _alldone=0; break; }
  done < "$QUERIES_FILE"
  [ "${_alldone:-1}" = 1 ] && { echo "[concord] all atlases already scored -> skip"; exit 0; }
fi

# 1) gather the drug signatures (the "ref" set the scorer reads)
nsig="$(bash "$HERE/gather_signatures.sh")"; nsig="${nsig:-0}"
if [ "$nsig" -eq 0 ] 2>/dev/null; then
  echo "[concord] no drug signatures gathered from $PSI_EVENTS_DIR -- cannot score (PSI not done?)" >&2
  exit 1
fi
echo "[concord] $nsig drug signature(s) ready in $DRUG_SIG_DIR"

# removeIR flag for the scorer (it wants the literal 'True')
IRFLAG=()
[ "${REMOVE_IR:-0}" = "1" ] && IRFLAG=(--removeIR True)

# 2) score + rank each atlas
nscored=0
while IFS=$'\t' read -r qname qdir ckind cpath || [ -n "${qname:-}" ]; do
  case "${qname:-}" in ''|'#'*) continue ;; esac
  [ -n "${qdir:-}" ] || continue
  resdir="$RESULTS_DIR/$qname"
  if [ -s "$resdir/concordance.txt" ] && [ ! -f "$PIPELINE_ROOT/.force_rescore" ]; then
    echo "[concord] atlas '$qname' already scored -> skip"; nscored=$((nscored+1)); continue
  fi
  if [ ! -d "$qdir" ]; then
    echo "[concord] atlas '$qname': query dir missing ($qdir) -> skip" >&2; continue
  fi
  mkdir -p "$resdir" || continue
  # the scorer writes concordance.txt + overlaps-*-direction.txt + pair_stats.tsv into the REF dir (DRUG_SIG_DIR);
  # clear stale first
  rm -f "$DRUG_SIG_DIR"/concordance.txt "$DRUG_SIG_DIR"/overlaps-*-direction.txt "$DRUG_SIG_DIR"/pair_stats.tsv 2>/dev/null || true
  echo "[concord] scoring atlas '$qname' (query=$qdir)"
  if python "$CONCORD_SCRIPT" --ref "$DRUG_SIG_DIR" --query "$qdir" \
       --rawp "$RAWP" --dPSI "$DPSI" ${IRFLAG[@]+"${IRFLAG[@]}"} > "$LOG_DIR/scorer_${qname}.log" 2>&1; then
    # move the scorer's outputs from the ref dir into this atlas's results dir
    for of in concordance.txt overlaps-same-direction.txt overlaps-opposite-direction.txt pair_stats.tsv; do
      [ -f "$DRUG_SIG_DIR/$of" ] && mv -f "$DRUG_SIG_DIR/$of" "$resdir/$of" 2>/dev/null
    done
    if [ -s "$resdir/concordance.txt" ]; then
      # 3) rank into a human-readable reversal-candidate summary
      COPT=()
      case "${ckind:-none}" in
        tsv)          [ -s "${cpath:-}" ] && COPT=(--counts "$cpath") ;;
        mergedresult) [ -s "${cpath:-}" ] && COPT=(--mergedresult "$cpath") ;;
      esac
      # pair_stats.tsv (beside concordance.txt) -> analytic-null significance; the tables are capped per drug and
      # EVERY scored compound gets a line, so a few huge signatures can no longer hide the rest of the library
      python "$SCRIPTS_DIR/rank_concordance.py" --concordance "$resdir/concordance.txt" \
             --atlas "$qname" --threshold "$CONC_THRESHOLD" --out "$resdir/ranked_concordance_summary.txt" \
             --min-overlap "${MIN_OVERLAP:-25}" --alpha "${FDR_ALPHA:-0.05}" --per-drug "${SUMMARY_ROWS_PER_DRUG:-3}" \
             --psi-root "$PSI_ROOT" --concordance-root "$PIPELINE_ROOT" \
             ${COPT[@]+"${COPT[@]}"} >> "$LOG_DIR/scorer_${qname}.log" 2>&1 \
        || echo "[concord] ranker failed for '$qname' (concordance.txt still present)" >&2
      nscored=$((nscored+1))
      echo "[concord] atlas '$qname' done -> $resdir"
    else
      echo "[concord] atlas '$qname': scorer produced no concordance.txt (see $LOG_DIR/scorer_${qname}.log)" >&2
    fi
  else
    echo "[concord] atlas '$qname': scorer FAILED (see $LOG_DIR/scorer_${qname}.log)" >&2
  fi
done < "$QUERIES_FILE"

rm -f "$PIPELINE_ROOT/.force_rescore" 2>/dev/null || true

# 4) ANALYTIC NULL across ALL atlases (manuscript Methods): Benjamini-Hochberg over every scored pair of every atlas
#    -> scored_pairs_with_null.tsv (the manuscript figure scripts read it) + concordance_by_compound.tsv (one line per
#    drug contrast). Optional agent-set enrichment (Mann-Whitney) when ENRICH_AGENTS is set, e.g. "indisulam,gsk591".
_ps=()
for _f in "$RESULTS_DIR"/*/pair_stats.tsv; do [ -s "$_f" ] && _ps+=("$_f"); done
if [ "${#_ps[@]}" -gt 0 ] && [ -s "$SCRIPTS_DIR/score_with_null.py" ]; then
  _EOPT=()
  [ -n "${ENRICH_AGENTS:-}" ] && _EOPT=(--agents "$ENRICH_AGENTS" --enrichment-out "$RESULTS_DIR/mechanism_enrichment.tsv")
  python "$SCRIPTS_DIR/score_with_null.py" --out "$RESULTS_DIR/scored_pairs_with_null.tsv" \
         --by-compound "$RESULTS_DIR/concordance_by_compound.tsv" \
         --complete "$RESULTS_DIR/complete_drug_by_subtype.tsv" \
         --min-overlap "${MIN_OVERLAP:-25}" --alpha "${FDR_ALPHA:-0.05}" ${_EOPT[@]+"${_EOPT[@]}"} ${_ps[@]+"${_ps[@]}"} \
         >> "$LOG_DIR/score_with_null.log" 2>&1 \
    && echo "[concord] analytic null + BH across ${#_ps[@]} atlas(es) -> $RESULTS_DIR/scored_pairs_with_null.tsv" \
    || echo "[concord] score_with_null failed (per-atlas summaries still present; see $LOG_DIR/score_with_null.log)" >&2
fi

# 5) STUDY COVERAGE across all atlases: which studies reached the ranking, and the step where each other study
#    dropped out (PSI plan -> BED -> AltAnalyze-written comparisons -> study-matched gather -> scored).
if [ -s "$SCRIPTS_DIR/study_coverage.py" ]; then
  python "$SCRIPTS_DIR/study_coverage.py" --psi-root "$PSI_ROOT" --concordance-root "$PIPELINE_ROOT" \
         --out "$RESULTS_DIR/study_coverage.tsv" > "$RESULTS_DIR/study_coverage.txt" 2>>"$LOG_DIR/study_coverage.log" \
    && echo "[concord] study coverage -> $RESULTS_DIR/study_coverage.txt (+ .tsv)"
fi

if ! concord_done; then
  echo "[concord] no concordance.txt produced for any atlas -- not done (safe to resubmit)" >&2
  exit 1
fi
echo "[concord] complete -> $nscored atlas result(s) in $RESULTS_DIR"

# wake the watchdog now (pure accelerator; the timed poll is the fallback)
concord_nudge_watchdog "concordance" || true
