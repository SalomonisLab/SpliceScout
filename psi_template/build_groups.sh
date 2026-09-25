#!/usr/bin/env bash
# =============================================================================
# build_groups.sh -- construct AltAnalyze groups.txt + comps.txt from the shipped
# sample_groups.tsv (BioSample<TAB>group_num<TAB>group_label) INTERSECTED with the
# *__junction.bed files actually present in BED_INPUT_DIR (so samples that failed
# upstream are excluded). If fewer than 2 groups end up with enough present members
# (or groups/comps would be empty), this FAILS (exit 1) rather than shipping empty
# files -- empty/missing groups+comps is what wedges AltAnalyze.
#
# COMPARISONS: if a sample_comps.tsv (exp_group<TAB>base_group) is shipped beside
# sample_groups.tsv, those EXPLICIT matched pairs are used (multi-study per-GSE: each
# drug condition vs its OWN study's controls, with a nearest-neighbor control study
# for drug-GSEs that have none) -- keeping only pairs where BOTH groups survived the
# BED intersection + MIN_PER_GROUP filter. With no sample_comps.tsv we fall back to
# the DEFAULT: every qualifying group vs the lowest group_num (control=1).
# Pure bash + nullglob; NEVER `grep -c` (empty on the compute nodes -- see lib_bed.sh).
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
# run_psi_job.sh repoints to a junction-only symlink dir (and may quarantine from it); honor that so the
# groups it (re)builds match exactly the BEDs AltAnalyze will read. Falls back to the configured dir.
BED_INPUT_DIR="${PSI_BEDDIR:-$BED_INPUT_DIR}"
set -u; shopt -s nullglob

MIN_PER_GROUP=2     # a COMPARED group needs at least this many present samples (else no usable stats)
SAMPLE_COMPS="${SAMPLE_COMPS:-$SCRIPTS_DIR/sample_comps.tsv}"   # optional explicit matched comparisons

mkdir -p "$PIPELINE_ROOT" "$LOG_DIR" "$PSI_OUT/ExpressionInput" 2>/dev/null || true   # groups live in ExpressionInput

# Only clear+rebuild when we CAN rebuild (sample_groups.tsv present). A missing sample_groups must NOT
# wipe a correct groups.txt that was assigned upstream -- otherwise run_psi_job would then error
# "cannot run groupless" on a run that actually has perfectly good groups. (Footgun fixed 2026-06-10.)
if [ ! -s "$SAMPLE_GROUPS" ]; then
  echo "[psi] no sample_groups.tsv shipped -> keeping any existing groups/comps as-is"; exit 0
fi
[ -d "$BED_INPUT_DIR" ] || { echo "[psi] BED_INPUT_DIR missing: $BED_INPUT_DIR -> keeping existing groups" >&2; exit 0; }
rm -f "$GROUPS_FILE" "$COMPS_FILE" 2>/dev/null

# pass 1: candidate rows for samples whose junction BED is present; tally per-group counts in a temp file.
tmp_g="$PIPELINE_ROOT/.groups.tmp.$$"; : > "$tmp_g"
counts="$PIPELINE_ROOT/.gcount.$$";    : > "$counts"
present=0; absent=0
while IFS=$'\t' read -r bs gnum glabel || [ -n "${bs:-}" ]; do
  case "${bs:-}" in ''|'#'*) continue ;; esac
  [ -n "${gnum:-}" ] || continue
  if [ -e "$BED_INPUT_DIR/${bs}__junction.bed" ]; then
    printf '%s%s\t%s\t%s\n' "$bs" "$GROUP_KEY_SUFFIX" "$gnum" "${glabel:-group$gnum}" >> "$tmp_g"
    printf '%s\n' "$gnum" >> "$counts"
    present=$((present+1))
  else
    absent=$((absent+1))
  fi
done < "$SAMPLE_GROUPS"

# --- identifier-shape diagnostic -----------------------------------------------------------------
# Silent killer: sample_groups.tsv keyed by BioSample (SAMN...) while the BEDs are named by run
# accession (SRR...) gives a ZERO intersection and an opaque "only 0 group(s)" abort that looks like
# a data problem rather than an id-format problem. Show BOTH id shapes so the cause is obvious.
if [ "$present" -eq 0 ] || [ "$absent" -gt $((present * 3)) ]; then
  _ex_grp=""
  while IFS=$'	' read -r _a _b _c || [ -n "${_a:-}" ]; do
    case "${_a:-}" in ''|'#'*) continue ;; esac
    _ex_grp="$_a"; break
  done < "$SAMPLE_GROUPS"
  _ex_bed=""
  for _f in "$BED_INPUT_DIR"/*__junction.bed; do
    [ -e "$_f" ] || continue
    _b="${_f##*/}"; _ex_bed="${_b%__junction.bed}"; break
  done
  echo "[psi] identifier check: $present matched, $absent unmatched" >&2
  echo "[psi]   sample_groups.tsv id looks like : ${_ex_grp:-<none>}" >&2
  echo "[psi]   BED filename id looks like      : ${_ex_bed:-<none>}" >&2
  case "${_ex_grp:-}" in
    SAM*)
      case "${_ex_bed:-}" in
        SRR*|ERR*|DRR*)
          echo "[psi]   MISMATCH: groups are keyed by BioSample, BEDs by run accession." >&2
          echo "[psi]   Fix: remap sample_groups.tsv to run accessions using the Run/BioSample" >&2
          echo "[psi]   columns of the SRA run table before launching PSI." >&2 ;;
      esac ;;
  esac
fi

# distinct group numbers with >= MIN_PER_GROUP present members (pure-bash tally; no grep -c)
ok_groups=""
for g in $(sort -u "$counts" 2>/dev/null); do
  c=0; while IFS= read -r x; do [ "$x" = "$g" ] && c=$((c+1)); done < "$counts"
  [ "$c" -ge "$MIN_PER_GROUP" ] && ok_groups="$ok_groups $g"
done
rm -f "$counts" 2>/dev/null

nok=0; for _g in $ok_groups; do nok=$((nok+1)); done
if [ "$nok" -lt 2 ]; then
  rm -f "$tmp_g" 2>/dev/null
  echo "[psi] FATAL: only $nok group(s) with >= $MIN_PER_GROUP present samples (of $present present, $absent absent BED)." >&2
  echo "[psi] need >= 2 qualifying groups to write groups.txt/comps.txt; refusing to ship empty files (would wedge AltAnalyze)." >&2
  exit 1
fi

# ---- comparisons ---------------------------------------------------------------------------------
: > "$COMPS_FILE"
used_groups=""
if [ -s "$SAMPLE_COMPS" ]; then
  # MATCHED comps (per-GSE): keep each shipped exp<TAB>base pair where BOTH groups qualified.
  kept=0
  while IFS=$'\t' read -r eg bg || [ -n "${eg:-}" ]; do
    case "${eg:-}" in ''|'#'*) continue ;; esac
    [ -n "${bg:-}" ] || continue
    ine=0; inb=0
    for g in $ok_groups; do [ "$g" = "$eg" ] && ine=1; [ "$g" = "$bg" ] && inb=1; done
    if [ "$ine" = 1 ] && [ "$inb" = 1 ]; then
      printf '%s\t%s\n' "$eg" "$bg" >> "$COMPS_FILE"
      used_groups="$used_groups $eg $bg"; kept=$((kept+1))
    fi
  done < "$SAMPLE_COMPS"
  used_groups="$(printf '%s\n' $used_groups | sort -un | tr '\n' ' ')"
  echo "[psi] matched comps: kept $kept pair(s) from sample_comps.tsv (both groups present & >= $MIN_PER_GROUP)"
else
  # DEFAULT: every qualifying group vs the LOWEST group number (the control/baseline).
  base=""
  for g in $(printf '%s\n' $ok_groups | sort -n); do [ -z "$base" ] && base="$g"; done
  for g in $(printf '%s\n' $ok_groups | sort -n); do
    [ "$g" = "$base" ] && continue
    printf '%s\t%s\n' "$g" "$base" >> "$COMPS_FILE"
  done
  used_groups="$ok_groups"
fi

# groups.txt: keep only rows whose group participates in a kept comparison
: > "$GROUPS_FILE"
while IFS=$'\t' read -r key gnum glabel; do
  for g in $used_groups; do
    if [ "$g" = "$gnum" ]; then printf '%s\t%s\t%s\n' "$key" "$gnum" "$glabel" >> "$GROUPS_FILE"; break; fi
  done
done < "$tmp_g"
rm -f "$tmp_g" 2>/dev/null

# final guard: both files MUST be non-empty (empty/missing groups+comps wedges AltAnalyze) -> FAIL loudly.
if [ ! -s "$GROUPS_FILE" ] || [ ! -s "$COMPS_FILE" ]; then
  echo "[psi] FATAL: groups.txt and/or comps.txt ended up empty after build ($nok qualifying group(s))." >&2
  echo "[psi] refusing to ship empty files (would wedge AltAnalyze)." >&2
  exit 1
fi

ng=0; while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) ng=$((ng+1)) ;; esac; done < "$GROUPS_FILE"
nc=0; while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) nc=$((nc+1)) ;; esac; done < "$COMPS_FILE"
echo "[psi] groups.txt: $ng samples ; comps.txt: $nc comparison(s) ($absent shipped samples had no BED)"

# --- attrition report ----------------------------------------------------------------------------
# Cohorts shrink enormously between "aligned" and "compared", and the loss used to be invisible:
# on one A549 run 6,363 aligned libraries became 1,913 compared samples across 140 of 397 studies,
# with 286 viable groups (2,156 samples) never entering a comparison because no control arm was
# paired with them. Write the funnel out every run so that shrinkage is a number, not a surprise.
_ATT="$PIPELINE_ROOT/groups_attrition.tsv"
{
  printf 'step	samples	groups	note
'
  _shipped=0
  while IFS= read -r _l; do case "$_l" in (*[![:space:]]*) _shipped=$((_shipped+1)) ;; esac; done < "$SAMPLE_GROUPS"
  printf '1_shipped_in_sample_groups	%s		annotated treated/control arms
' "$_shipped"
  printf '2_bed_present	%s		%s shipped samples had no junction BED
' "$present" "$absent"
  printf '3_group_size_ge_%s		%s	groups meeting MIN_PER_GROUP
' "$MIN_PER_GROUP" "$nok"
  printf '4_written_to_groups_txt	%s		samples entering AltAnalyze
' "$ng"
  printf '5_comparisons		%s	a group is used ONLY if a comparison references it
' "$nc"
} > "$_ATT" 2>/dev/null
echo "[psi] attrition funnel -> $_ATT" >&2
if [ "$_shipped" -gt 0 ] && [ "$ng" -lt $((_shipped / 2)) ]; then
  echo "[psi] NOTE: only $ng of $_shipped annotated samples entered the comparison set." >&2
  echo "[psi] Most loss at this step is groups with no matched control arm, not bad data --" >&2
  echo "[psi] see groups_attrition.tsv before concluding the cohort was small." >&2
fi
