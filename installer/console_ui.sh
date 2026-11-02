#!/usr/bin/env bash
# KubeDock installer console UI. Safe on TTYs, CI and piped/non-interactive runs.

ui_init() {
  UI_COLOR=0
  UI_INTERACTIVE=0
  UI_VERBOSE="$(printenv KUBEDOCK_INSTALL_VERBOSE 2>/dev/null || true)"
  local term_value no_color_value
  term_value="$(printenv TERM 2>/dev/null || true)"
  no_color_value="$(printenv NO_COLOR 2>/dev/null || true)"
  if [[ -t 1 && "$term_value" != "dumb" && -z "$no_color_value" ]]; then
    UI_COLOR=1
  fi
  if [[ -t 0 && -t 1 ]]; then
    UI_INTERACTIVE=1
  fi
}

_ui_color() {
  local code="$1"
  if [[ "$UI_COLOR" == "1" ]]; then printf '\033[%sm' "$code"; fi
}

_ui_reset() {
  if [[ "$UI_COLOR" == "1" ]]; then printf '\033[0m'; fi
}

ui_banner() {
  local subtitle="$1"
  if [[ -z "$subtitle" ]]; then subtitle="Secure infrastructure workspace installer"; fi
  printf '\n'
  _ui_color "38;5;141"
  printf '  ╭───────────────────────────────────────────────────────────────────────╮\n'
  printf '  │                                                                       │\n'
  printf '  │   ██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗  ██████╗  ██████╗ ██╗  ██╗ │\n'
  printf '  │   ██║ ██╔╝██║   ██║██╔══██╗██╔════╝██╔══██╗██╔═══██╗██╔════╝ ██║ ██╔╝ │\n'
  printf '  │   █████╔╝ ██║   ██║██████╔╝█████╗  ██║  ██║██║   ██║██║      █████╔╝ │\n'
  printf '  │   ██╔═██╗ ██║   ██║██╔══██╗██╔══╝  ██║  ██║██║   ██║██║      ██╔═██╗ │\n'
  printf '  │   ██║  ██╗╚██████╔╝██║  ██║███████╗██████╔╝╚██████╔╝╚██████╗ ██║  ██╗ │\n'
  printf '  │   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝ │\n'
  printf '  │                                                              │\n'
  printf '  ╰───────────────────────────────────────────────────────────────────────╯\n'
  _ui_reset
  _ui_color "1;37"
  printf '  KubeDock Installer\n'
  _ui_reset
  _ui_color "90"
  printf '  %s\n' "$subtitle"
  _ui_reset
  printf '\n'
}

ui_phase() {
  local current="$1" total="$2" title="$3" detail="$4"
  local width=26 filled percent bar="" i
  if [[ "$total" -le 0 ]]; then total=1; fi
  filled=$((current * width / total))
  percent=$((current * 100 / total))
  for ((i=0; i<width; i++)); do
    if (( i < filled )); then bar+="█"; else bar+="░"; fi
  done
  _ui_color "38;5;141"
  printf '  [%02d/%02d] ' "$current" "$total"
  _ui_reset
  _ui_color "1;37"
  printf '%s\n' "$title"
  _ui_reset
  if [[ -n "$detail" ]]; then
    _ui_color "90"
    printf '          %s\n' "$detail"
    _ui_reset
  fi
  if [[ "$UI_COLOR" == "1" ]]; then
    _ui_color "38;5;141"
    printf '          [%s]' "$bar"
    _ui_reset
    _ui_color "90"
    printf ' %3d%%\n' "$percent"
    _ui_reset
  else
    printf '          Progress: %d%%\n' "$percent"
  fi
  printf '\n'
}

ui_info() {
  _ui_color "36"
  printf '  i  '
  _ui_reset
  printf '%s\n' "$*"
}

ui_success() {
  _ui_color "32;1"
  printf '  ✓  '
  _ui_reset
  printf '%s\n' "$*"
}

ui_warn() {
  _ui_color "33;1"
  printf '  !  '
  _ui_reset
  printf '%s\n' "$*"
}

ui_error() {
  _ui_color "31;1"
  printf '  ✗  '
  _ui_reset
  printf '%s\n' "$*" >&2
}

ui_run_step() {
  local label="$1"
  shift
  local log rc pid frame_index=0 frame tmp_base
  tmp_base="$(printenv TMPDIR 2>/dev/null || true)"
  if [[ -z "$tmp_base" ]]; then tmp_base="/tmp"; fi
  if [[ "$UI_INTERACTIVE" != "1" ]]; then
    printf '[RUN] %s\n' "$label"
    "$@"
    return $?
  fi

  log="$(mktemp "$tmp_base/kubedock-installer.XXXXXX")" || {
    ui_warn "Could not create a temporary progress log; running command normally."
    "$@"
    return $?
  }

  "$@" >"$log" 2>&1 </dev/tty &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    frame_index=$(( (frame_index + 1) % 4 ))
    case "$frame_index" in
      0) frame="|" ;;
      1) frame="/" ;;
      2) frame="-" ;;
      *) frame="\\" ;;
    esac
    printf '\r  '
    _ui_color "38;5;141"
    printf '%s' "$frame"
    _ui_reset
    printf '  %s' "$label"
    sleep 0.12
  done

  if wait "$pid"; then
    printf '\r\033[2K'
    ui_success "$label"
    if [[ "$UI_VERBOSE" == "1" ]]; then cat "$log"; fi
    rm -f "$log"
    return 0
  else
    rc=$?
    printf '\r\033[2K'
    ui_error "$label failed (exit code $rc)"
    printf '\n'
    tail -n 24 "$log" >&2 || true
    rm -f "$log"
    return "$rc"
  fi
}

ui_complete() {
  local message="$1"
  if [[ -z "$message" ]]; then message="Installation completed successfully"; fi
  printf '\n'
  _ui_color "32;1"
  printf '  ╭──────────────────────────────────────────────────────────────╮\n'
  printf '  │   ✓  %s\n' "$message"
  printf '  ╰──────────────────────────────────────────────────────────────╯\n'
  _ui_reset
  printf '\n'
}

ui_failed() {
  local code="$1"
  if [[ -z "$code" ]]; then code=1; fi
  printf '\n'
  ui_error "Installation stopped (exit code $code)."
  ui_info "Review the error above and retry. Existing KubeDock installation was preserved where possible."
  printf '\n'
}
