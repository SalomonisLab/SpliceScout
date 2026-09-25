#!/usr/bin/env bash
# =============================================================================
# build_star_index.sh -- BUILD-ONCE STAR genome index (LSF job body). Heavy:
# ~1-2 h, tens of GB RAM (GRCh38 needs ~32-40 GB). Submitted by resolve_index.sh
# only when no usable index exists for the organism.
# Args: <organism> <target_index_dir> <fasta_url> <gtf_url> <threads> <sjdbOverhang>
# Idempotent: skips if a valid index is already present; only marks success after
# STAR genomeGenerate AND a validity check pass.
# =============================================================================
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.sh"
source "$HERE/lib_star.sh"
source "$HERE/lib_index.sh"
set -u
star_load_modules

ORG="${1:-organism}"; IDX="$2"; FAURL="$3"; GTFURL="$4"; THR="${5:-${BUILD_THREADS:-16}}"; OH="${6:-100}"
MARK="$IDX/.star_index_done"
# ONE EXIT trap for BOTH cleanups. (A second `trap … EXIT` further down used to REPLACE this one, so the
# cross-run build lock was never released -> every later run saw the lock, assumed "claimed by another run",
# submitted samples with no index dependency, and they all failed -> MELTDOWN.)
WORK=""
_buildidx_cleanup() {
  [ -n "$WORK" ] && rm -rf "$WORK"
  rmdir "${IDX%/}.buildlock" 2>/dev/null   # release the cross-run build lock resolve_index.sh took
  return 0
}
trap _buildidx_cleanup EXIT

command -v STAR >/dev/null 2>&1 || { echo "[buildidx] STAR not on PATH" >&2; exit 1; }
if star_index_valid "$IDX"; then
  echo "[buildidx] $ORG: a valid index already exists at $IDX -> skip"; touch "$MARK" 2>/dev/null || true; exit 0
fi
[ -n "$FAURL" ] || { echo "[buildidx] no FASTA URL for '$ORG'" >&2; exit 1; }
[ -n "$GTFURL" ] || { echo "[buildidx] no GTF URL for '$ORG'" >&2; exit 1; }

WORKBASE="${SCRATCH:-$(dirname "$BAM_OUT")}"
mkdir -p "$WORKBASE" "$IDX" || { echo "[buildidx] cannot mkdir workspace/index" >&2; exit 1; }
WORK="$WORKBASE/staridx_$(star_org_slug "$ORG")_${LSB_JOBID:-$$}"
mkdir -p "$WORK" || exit 1          # removed by _buildidx_cleanup (EXIT trap above) together with the lock

fetch() {                          # url -> echoes local uncompressed path
  local url="$1" gz out
  gz="$WORK/$(basename "$url")"
  if command -v curl >/dev/null 2>&1; then
    curl -fSL --retry 4 -o "$gz" "$url" || { echo "[buildidx] download failed: $url" >&2; return 1; }
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$gz" "$url" || { echo "[buildidx] download failed: $url" >&2; return 1; }
  else
    echo "[buildidx] neither curl nor wget available" >&2; return 1
  fi
  case "$gz" in
    *.gz) gunzip -f "$gz"; out="${gz%.gz}" ;;
    *)    out="$gz" ;;
  esac
  printf '%s' "$out"
}

echo "[buildidx] $ORG: downloading reference FASTA + GTF"
FA="$(fetch "$FAURL")" || exit 1
GTF="$(fetch "$GTFURL")" || exit 1

# Chromosome-NAMING guard: an Ensembl FASTA names chromosomes '1..22,X,Y,MT' while a GENCODE/UCSC GTF uses
# 'chr1..chrM'. STAR silently drops GTF lines whose chromosome isn't in the FASTA, so a mismatched pair builds
# an index with (almost) no annotated junctions -- or fails with "no valid exon lines". Detect the two styles
# and rewrite the GTF to the FASTA's convention before genomeGenerate.
fa_chr="$(awk 'substr($0,1,1)==">"{print substr($1,2); exit}' "$FA")"
gtf_chr="$(awk '!/^#/{print $1; exit}' "$GTF")"
case "$fa_chr" in chr*) fa_style=chr ;; *) fa_style=plain ;; esac
case "$gtf_chr" in chr*) gtf_style=chr ;; *) gtf_style=plain ;; esac
if [ -n "$fa_chr" ] && [ -n "$gtf_chr" ] && [ "$fa_style" != "$gtf_style" ]; then
  echo "[buildidx] $ORG: chromosome naming differs (FASTA '$fa_chr' vs GTF '$gtf_chr') -> rewriting GTF to $fa_style style"
  if [ "$fa_style" = plain ]; then
    awk 'BEGIN{OFS=FS="\t"} /^#/{print;next} {sub(/^chr/,"",$1); if($1=="M")$1="MT"; print}' "$GTF" > "$GTF.fixed"
  else
    awk 'BEGIN{OFS=FS="\t"} /^#/{print;next} {if($1=="MT")$1="M"; $1="chr"$1; print}' "$GTF" > "$GTF.fixed"
  fi
  mv -f "$GTF.fixed" "$GTF" || { echo "[buildidx] could not rewrite GTF chromosome names" >&2; exit 1; }
fi

echo "[buildidx] $ORG: STAR genomeGenerate -> $IDX (threads=$THR, sjdbOverhang=$OH)"
STAR --runMode genomeGenerate --runThreadN "$THR" \
     --genomeDir "$IDX" \
     --genomeFastaFiles "$FA" \
     --sjdbGTFfile "$GTF" \
     --sjdbOverhang "$OH" \
     --outTmpDir "$WORK/_STARtmp" \
     ${STAR_GENOME_EXTRA_ARGS:-}
RC=$?
if [ "$RC" -ne 0 ] || ! star_index_valid "$IDX"; then
  echo "[buildidx] $ORG: genomeGenerate FAILED (rc=$RC) -- index NOT marked valid (safe to resubmit)" >&2
  exit 1
fi
{ echo "organism=$ORG"; echo "fasta=$FAURL"; echo "gtf=$GTFURL"; echo "overhang=$OH"; } > "$MARK"
echo "[buildidx] $ORG: index built -> $IDX"
