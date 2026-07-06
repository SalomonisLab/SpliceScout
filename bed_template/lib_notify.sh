#!/usr/bin/env bash
# =============================================================================
# lib_notify.sh -- shared error logging + email notification for SpliceScout
# cluster stages (vendored IDENTICALLY into every stage bundle; the deploy copies
# the whole template dir). Sourced by the watchdogs + long-running jobs so a
# cluster job can EMAIL THE USER DIRECTLY via the cluster's own `mail` -- an error
# reaches the user even when the PC GUI / alert poller is closed (the PC poller
# only ever saw PIPELINE_* MARKER files, so a RUN-but-frozen job emailed nothing).
# Every notify call also appends one line to a central EVENTS.log in PIPELINE_ROOT,
# so EVERY error/milestone is logged in one place.
#
# Inputs (from config.sh / env): ALERT_EMAIL (recipient; blank = email OFF),
#   STAGE (label; defaults to JOB_TAG), PIPELINE_ROOT (for EVENTS.log + dedup state).
# Best-effort: NEVER fails the caller. Dedup/rate-limit via marker files so a
# persistent error or a chatty stage cannot spam the inbox.
# =============================================================================
: "${PIPELINE_ROOT:=.}"
: "${STAGE:=${JOB_TAG:-splicescout}}"
: "${ALERT_EMAIL:=}"
: "${EVENTS_LOG:=$PIPELINE_ROOT/EVENTS.log}"
: "${NOTIFY_DIR:=$PIPELINE_ROOT/.notify}"
: "${NOTIFY_DEDUP_MIN:=360}"          # don't re-email the SAME key within this many minutes (default 6h)

_notify_ts() { date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '?'; }

# log_event LEVEL msg...   -> append to EVENTS.log (+ stderr). LEVEL = INFO | WARN | ERROR.
log_event() {
  local lvl="$1"; shift
  local line; line="[$(_notify_ts)] [$STAGE] [$lvl] $*"
  mkdir -p "$(dirname "$EVENTS_LOG")" 2>/dev/null
  printf '%s\n' "$line" >> "$EVENTS_LOG" 2>/dev/null || true
  printf '%s\n' "$line" >&2
}

# _notify_send subject body   -> email via the cluster's mail (best-effort; base64 dodges shell-quoting).
_notify_send() {
  [ -n "$ALERT_EMAIL" ] || return 0
  command -v mail >/dev/null 2>&1 || { log_event WARN "no 'mail' on PATH -> cannot email: $1"; return 0; }
  local b64; b64="$(printf '%s\n' "$2" | base64 2>/dev/null | tr -d '\n')"
  if [ -n "$b64" ]; then
    printf '%s' "$b64" | base64 -d 2>/dev/null | mail -s "$1" "$ALERT_EMAIL" 2>/dev/null \
      && log_event INFO "emailed -> $ALERT_EMAIL: $1" || log_event WARN "mail send FAILED: $1"
  else
    printf '%s\n' "$2" | mail -s "$1" "$ALERT_EMAIL" 2>/dev/null \
      && log_event INFO "emailed -> $ALERT_EMAIL: $1" || log_event WARN "mail send FAILED: $1"
  fi
}

# _notify_dedup key [minutes]  -> 0 (send + stamp) if not sent within `minutes`, else 1 (suppress).
_notify_dedup() {
  local key="$1" mins="${2:-$NOTIFY_DEDUP_MIN}" kf age now then
  mkdir -p "$NOTIFY_DIR" 2>/dev/null
  kf="$NOTIFY_DIR/$(printf '%s' "$key" | tr -c 'A-Za-z0-9._-' '_')"
  if [ -f "$kf" ]; then
    now="$(date +%s 2>/dev/null || echo 0)"; then="$(date -r "$kf" +%s 2>/dev/null || echo 0)"
    age=$(( (now - then) / 60 ))
    [ "$age" -ge 0 ] && [ "$age" -lt "$mins" ] && return 1
  fi
  : > "$kf" 2>/dev/null; return 0
}

# notify_error subject body [dedup_key]   -> log ERROR + email (deduped).
notify_error() {
  local subj="$1" body="$2" key="${3:-$1}"
  log_event ERROR "$subj -- $body"
  _notify_dedup "err:$key" "$NOTIFY_DEDUP_MIN" || { log_event INFO "error deduped (emailed recently): $subj"; return 0; }
  _notify_send "SpliceScout ERROR [$STAGE]: $subj" \
    "$(printf 'Run:   %s\nStage: %s\nHost:  %s\nTime:  %s\n\n%s\n' "$PIPELINE_ROOT" "$STAGE" "$(hostname 2>/dev/null)" "$(_notify_ts)" "$body")"
}

# notify_diagnose JOB_TAG PIPELINE_ROOT SCRIPTS_DIR LOG_DIR  -- on a STALL, bsub the CPU diagnostic-AI job
# (a self-contained local quantized LLM) when DIAGNOSE_ON_STALL=1 and it is installed. It emails an AI
# diagnosis of the stall and, with DIAGNOSE_AUTOFIX=1, applies a SAFE whitelisted fix (quarantine a corrupt
# bed / re-arm, budget-capped). No-op + never fails the caller if the AI isn't installed -- so the regular
# deterministic self-healing is wholly unaffected; this is the fallback for the stalls it couldn't resolve.
notify_diagnose() {
  [ "${DIAGNOSE_ON_STALL:-1}" = "1" ] || return 0
  local aih="${DIAGNOSE_AI_HOME:-/data/salomonis-archive/LabFiles/SpliceScout_AI}"
  [ -x "$aih/scripts/diagnose_job.sh" ] || { log_event INFO "CPU diagnostic AI not installed at $aih -> skipping AI diagnosis"; return 0; }
  command -v bsub >/dev/null 2>&1 || return 0
  bsub -L /bin/bash -n 8 -M 16000 -W 66480 -J "${1}_diagnose" \
       -o "${4}/diagnose.out" -e "${4}/diagnose.err" \
       "$aih/scripts/diagnose_job.sh" "$1" "$2" "$3" "$4" "${DIAGNOSE_AUTOFIX:-0}" "${ALERT_EMAIL:-}" "${DIAGNOSE_MODEL_PATH:-}" "${DIAGNOSE_MODEL_DIR:-}" >/dev/null 2>&1 \
    && log_event INFO "submitted CPU-AI diagnosis for the $1 STALL" \
    || log_event WARN "could not submit the CPU-AI diagnosis job"
}

# notify_update subject body   -> log INFO + email a milestone/progress note (deduped by subject).
notify_update() {
  local subj="$1" body="$2"
  log_event INFO "$subj -- $body"
  _notify_dedup "upd:$subj" "$NOTIFY_DEDUP_MIN" || return 0
  _notify_send "SpliceScout update [$STAGE]: $subj" \
    "$(printf 'Run:   %s\nStage: %s\nTime:  %s\n\n%s\n' "$PIPELINE_ROOT" "$STAGE" "$(_notify_ts)" "$body")"
}
