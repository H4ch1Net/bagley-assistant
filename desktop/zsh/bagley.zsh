# Bagley shell co-pilot for zsh.
#
#   ?? find files over 1GB    Enter puts a proposed command on the prompt without running it.
#                             Review or edit it, then Enter again runs it.
#   bagley why                Explains the last failed command, with a suggested fix.
#
# Load it from ~/.zshrc (anywhere after your plugin manager and Powerlevel10k setup):
#
#   source /path/to/bagley/desktop/zsh/bagley.zsh
#
# It only defines functions and hooks; nothing runs at shell startup. Set BAGLEY_BIN to run
# Bagley another way, e.g. BAGLEY_BIN="python -m bagley". See docs/shell.md.

# Only interactive shells have a prompt to fill.
[[ -o interactive ]] || return 0

zmodload zsh/datetime 2>/dev/null
zmodload zsh/zleparameter 2>/dev/null
autoload -Uz add-zsh-hook

# The last command and the last failure: cmd, status, cwd, time (epoch seconds), seq.
typeset -gA _bagley_last _bagley_fail
typeset -g _bagley_line='' _bagley_dir='' _bagley_started='' _bagley_guard=''
typeset -gi _bagley_seq=0 _bagley_pending=0

# A gray line on stderr, plain for NO_COLOR or when stderr is not a terminal.
_bagley_note() {
  if [[ -z ${NO_COLOR-} && -t 2 ]]; then
    print -u2 -r -- $'\e[38;2;122;122;122m'"$*"$'\e[0m'
  else
    print -u2 -r -- "$*"
  fi
}

# How to run Bagley: $BAGLEY_BIN split into words, else the `bagley` on PATH.
_bagley_bin() {
  reply=(${(z)${BAGLEY_BIN:-bagley}})
}

# Record each command line and, when it fails, keep it as the last failure. ---------------

_bagley_preexec() {
  _bagley_line=${1:-$3}
  _bagley_dir=$PWD
  _bagley_started=$EPOCHSECONDS
  (( ++_bagley_seq ))
  _bagley_pending=1
}

_bagley_precmd() {
  local st=$?
  _bagley_guard=''
  (( _bagley_pending )) || return 0
  _bagley_pending=0
  _bagley_last=(cmd "$_bagley_line" status "$st" cwd "$_bagley_dir" time "$_bagley_started"
    seq "$_bagley_seq")
  # Ctrl+C (130) and Ctrl+Z (148) are not failures to explain; nor is asking Bagley itself.
  (( st == 0 || st == 130 || st == 148 )) && return 0
  case $_bagley_line in
    ('??'*|'bagley why'*|'command bagley why'*) return 0 ;;
  esac
  _bagley_fail=("${(@kv)_bagley_last}")
}

# Keep `?? ...` requests out of the history; the command that runs is saved as usual.
_bagley_addhistory() {
  emulate -L zsh
  local line=${1#"${1%%[![:space:]]*}"}
  [[ $line != '??'(|[[:space:]]*) ]]
}

add-zsh-hook preexec _bagley_preexec
add-zsh-hook precmd _bagley_precmd
add-zsh-hook zshaddhistory _bagley_addhistory

# Ask for a command. Prints its lines: the command, then #danger:, #note: or #error: lines.
_bagley_ask_lines() {
  local -a reply
  _bagley_bin
  if (( ! ${+commands[${reply[1]}]} )) && [[ ! -x ${reply[1]} ]]; then
    print -r -- "#error: $reply[1] not found. Install Bagley or set BAGLEY_BIN."
    return 127
  fi
  command "${reply[@]}" suggest --zsh --cwd "$PWD" -- "$1" </dev/null 2>/dev/null
}

# Split _bagley_ask_lines output into the variables cmd, danger, note and err of the caller.
_bagley_parse() {
  local l
  for l in "${(@f)1}"; do
    case $l in
      ('#danger: '*) danger=${l#'#danger: '} ;;
      ('#note: '*) note=${l#'#note: '} ;;
      ('#error: '*) err=${l#'#error: '} ;;
      ('#'*) ;;
      (*) [[ -z $cmd && -n $l ]] && cmd=$l ;;
    esac
  done
}

# The `??` request: fill the prompt with a proposed command, never run it. -----------------

_bagley_suggest_widget() {
  emulate -L zsh
  local request=${BUFFER#'??'}
  request=${(j: :)${=request}}
  if [[ -z $request ]]; then
    zle -M "» bagley: say what you want after ??, e.g. ?? find files over 1GB"
    return 0
  fi
  zle -R "» bagley: thinking..."
  local out rc interrupted=0
  # Ctrl+C stops the request (Bagley gets the signal too) and keeps the line as it was.
  trap 'interrupted=1' INT
  out=$(_bagley_ask_lines "$request")
  rc=$?
  trap - INT
  if (( interrupted || rc == 130 )); then
    zle -M "» bagley: cancelled"
    return 0
  fi
  local cmd='' danger='' note='' err=''
  _bagley_parse "$out"
  if [[ -z $cmd ]]; then
    zle -M "» bagley: ${err:-no suggestion (exit $rc)}"
    return 0
  fi
  if [[ -n $danger ]]; then
    # Commented out: Enter does nothing until the leading # is removed.
    BUFFER="# $cmd"
    _bagley_guard=$danger
    zle -M "» bagley: CAREFUL: $danger. Remove the leading # to run it."
  else
    BUFFER=$cmd
    zle -M "» bagley: ${note:-review it, then Enter runs it}"
  fi
  CURSOR=${#BUFFER}
  return 0
}

# accept-line, wrapped: `??` lines go to Bagley, everything else to the previous widget.
_bagley_accept_line() {
  if [[ -n $_bagley_guard && $BUFFER == '#'* ]]; then
    zle -M "» bagley: CAREFUL: $_bagley_guard. Remove the leading # to run it."
    return 0
  fi
  _bagley_guard=''
  if [[ $BUFFER == '??' || $BUFFER == '??'[[:space:]]* ]]; then
    _bagley_suggest_widget
    return 0
  fi
  zle _bagley_orig_accept_line -- "$@"
}

# Wrap whatever accept-line is now (the builtin, or another plugin's wrapper), once per shell.
# zsh-autosuggestions and zsh-syntax-highlighting wrap it again later, which is fine.
_bagley_builtin_accept_line() {
  zle .accept-line -- "$@"
}

if (( ! ${+_bagley_wrapped} )); then
  typeset -g _bagley_wrapped=1
  case ${widgets[accept-line]-} in
    (user:*) zle -N _bagley_orig_accept_line "${widgets[accept-line]#user:}" ;;
    (*) zle -N _bagley_orig_accept_line _bagley_builtin_accept_line ;;
  esac
  zle -N accept-line _bagley_accept_line
fi

# Without the widget (another plugin replaced accept-line) `??` still asks Bagley, and the
# answer waits on the next prompt. noglob keeps `??` from matching two-letter file names.
_bagley_ask() {
  emulate -L zsh
  local request="$*"
  if [[ -z $request ]]; then
    _bagley_note "» bagley: say what you want after ??, e.g. ?? find files over 1GB"
    return 2
  fi
  local out rc cmd='' danger='' note='' err=''
  out=$(_bagley_ask_lines "$request")
  rc=$?
  _bagley_parse "$out"
  if [[ -z $cmd ]]; then
    _bagley_note "» bagley: ${err:-no suggestion (exit $rc)}"
    return 1
  fi
  if [[ -n $danger ]]; then
    _bagley_note "» bagley: CAREFUL: $danger. Remove the leading # to run it."
    print -z -- "# $cmd"
  else
    [[ -n $note ]] && _bagley_note "» bagley: $note"
    print -z -- "$cmd"
  fi
}
alias '??'='noglob _bagley_ask'

# `bagley why`: explain the last failure. Everything else goes to the real command. --------

bagley() {
  emulate -L zsh
  local -a reply
  _bagley_bin
  if [[ $1 != why ]]; then
    command "${reply[@]}" "$@"
    return
  fi
  shift
  local -a bin=("${reply[@]}")

  # Output piped in (`make 2>&1 | bagley why`): explain that pipeline, not the last failure.
  if [[ ! -t 0 ]]; then
    local piped=${_bagley_line%'|'*bagley*why*}
    piped=${piped%"${piped##*[![:space:]]}"}
    local -a extra=(--cwd "$PWD")
    [[ -n $piped && $piped != "$_bagley_line" ]] && extra+=(--command "$piped")
    command "${bin[@]}" why "${extra[@]}" "$@"
    return
  fi

  if (( ! ${+_bagley_fail[cmd]} )); then
    command "${bin[@]}" why --cwd "$PWD" "$@" </dev/null
    return
  fi

  local -a args=(--command "$_bagley_fail[cmd]" --status "$_bagley_fail[status]"
    --cwd "$_bagley_fail[cwd]")
  local out='' tip=''
  # Read what the failed command printed from the terminal. Never run it again.
  if [[ -n $TMUX ]] && (( $+commands[tmux] )); then
    out=$(tmux capture-pane -p -J -S -200 2>/dev/null)
  elif [[ -n $KITTY_WINDOW_ID ]]; then
    local -a kitty=()
    if (( $+commands[kitten] )); then
      kitty=(kitten @)
    elif (( $+commands[kitty] )); then
      kitty=(kitty @)
    fi
    if [[ -z $KITTY_LISTEN_ON ]] || (( ! ${#kitty} )); then
      tip="kitty capture is off. In kitty.conf: shell_integration enabled, allow_remote_control socket-only, listen_on unix:/tmp/kitty"
    elif [[ $_bagley_fail[seq] != "$_bagley_last[seq]" ]]; then
      tip="other commands ran since it failed, so its output is not captured. Pipe it in: cmd 2>&1 | bagley why"
    else
      (( $+commands[timeout] )) && kitty=(timeout 5 "${kitty[@]}")
      out=$("${kitty[@]}" --to "$KITTY_LISTEN_ON" get-text --match "id:$KITTY_WINDOW_ID" \
        --extent last_non_empty_output 2>/dev/null) ||
        tip="kitty did not answer. In kitty.conf: shell_integration enabled, allow_remote_control socket-only, listen_on unix:/tmp/kitty"
    fi
  else
    tip="no output captured. Use kitty (shell_integration, remote control) or tmux, or pipe it in: cmd 2>&1 | bagley why"
  fi
  [[ -n $tip ]] && _bagley_note "TIP  $tip"
  if [[ -n $out ]]; then
    print -r -- "$out" | command "${bin[@]}" why "${args[@]}" "$@"
  else
    command "${bin[@]}" why "${args[@]}" "$@" </dev/null
  fi
}
