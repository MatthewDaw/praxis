# Sourced by af-ticket-loop.sh. Local orchestration recovery only: never grades tickets.
af_checkpoint_verification(){
  local dest="$AF_STATE_DIR/af-pending-verification-$PROJECT" tmp
  tmp="$dest.tmp.$$"
  ( umask 077
    printf '%s\n' "$1" "$(git -C "$WT" rev-parse HEAD)" "$2" "${*:3}" > "$tmp"
  )
  mv -f "$tmp" "$dest"
}

af_clear_verification_checkpoint(){
  rm -f "$AF_STATE_DIR/af-pending-verification-$PROJECT"
}

af_resume_verification(){
  local pending="$AF_STATE_DIR/af-pending-verification-$PROJECT"
  [ -f "$pending" ] || return 0
  local baseline head rnd ids old_baseline="${AF_PREMERGE_SHA:-}"
  local -a ticket_ids
  { IFS= read -r baseline; IFS= read -r head; IFS= read -r rnd; IFS= read -r ids; } < "$pending"
  [ -n "$baseline" ] && [ -n "$head" ] && [ -n "$rnd" ] && [ -n "$ids" ] || {
    say "RECOVERY: incomplete verification checkpoint $pending; refusing to invent a verdict"; return 9;
  }
  [ "$(git -C "$WT" rev-parse HEAD)" = "$head" ] || {
    say "RECOVERY: integration HEAD changed since pending verification; checkpoint retained"; return 9;
  }
  read -r -a ticket_ids <<< "$ids"
  say "RECOVERY: resuming verification of $head before dispatching more work"
  AF_PREMERGE_SHA="$baseline"
  verify_round "$rnd" "${ticket_ids[@]}"
  local result=$?
  [ "$result" -eq 0 ] || return "$result"
  AF_PREMERGE_SHA="$old_baseline"
}

af_verification_can_extend(){
  # A quiet pane may have a real test child; a changing pane is progress too.
  [ "$1" -lt "$2" ] || [ "$3" = 1 ]
}

af_worker_turn_ended(){
  # Match the CLI's completed-turn UI, not ordinary prose saying a ticket is blocked.
  printf '%s\n' "$1" | grep -Eq '^[[:space:]]*Worked for [0-9]' || return 1
  ! printf '%s\n' "$1" | grep -Eq 'esc to interrupt|^[[:space:]]*• Working'
}

af_all_worker_turns_ended(){
  local sx pane found=0
  for sx in ${AF_WORKER_SESSIONS:-$SESSION}; do
    tmux has-session -t "$sx" 2>/dev/null || continue
    found=1
    pane=$(tmux capture-pane -t "$sx" -p 2>/dev/null) || return 1
    af_worker_turn_ended "$pane" || return 1
    verify_children_busy "$sx" && return 1
  done
  [ "$found" = 1 ]
}
