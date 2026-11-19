# Mailbox behavior and checks

How the bus hands out mail and decides who owns a mailbox, and the tests that pin each rule.

## Preparing and acknowledging mail

`Bus.prepare_delivery()` returns context text and receipt IDs without editing the agent's inbox. It records the delivery event inside the caller's transaction. Direct mail is shown first; Stop receives only direct mail. Messages omitted by the output budget remain unread, while a displayed preview still counts as delivery.

The hook returns the receipt to the client, which acknowledges only after flushing output. A plain `GET /inbox` without a receipt marks mail read immediately. Obsolete entries, such as board notices for a project the agent left, need no acknowledgment when there is no mail output. When mixed with visible mail, they remain part of the receipt.

Regression coverage: `test_preparing_mail_preserves_the_inbox` and `test_obsolete_board_entries_keep_existing_receipt_behavior` in [test_mailbox.py](tests/test_mailbox.py), plus the bounded-output and lost-response tests in [test_server.py](tests/test_server.py).

## Ownership decisions

`Bus.replacement_reason()` decides whether a mailbox may be inherited and returns the reason. Its three callers distinguish automatic name selection, explicit rename, and replacement in the same terminal pane. `Bus.retire()` performs the transfer: merge unread mail, preserve the stronger delivery flags and aliases, and record the old session/process guards.

`Bus.hook_ownership()` separately decides whether an incoming hook is still allowed to act. It does not update ownership. These decisions and their updates happen under the bus lock. Waking repeats its ownership and foreground-process checks before terminal input.

| Existing state and incoming action | Expected owner and old-session behavior | Regression coverage |
| --- | --- | --- |
| Live agent; another terminal requests its name | Existing agent keeps the name and mail; newcomer receives another name | `test_a_live_agents_name_and_mail_are_never_taken` |
| Ended terminal; another ordinary terminal requests its name | New session inherits; superseded session cannot act | `test_a_dead_local_harness_goes_offline_and_its_name_and_mail_pass_on` |
| Same host, tmux server, and pane; newer session registers, even under a different requested name | New session inherits the pane's previous mailbox; old hooks and receipts cannot consume it | `test_direct_flag_survives_rename_and_takeover`, `test_old_hook_receipt_cannot_acknowledge_inherited_mail[seat]` |
| Pane number reused by a different tmux server | Pane number alone does not transfer ownership | `test_a_restarted_tmux_server_reuses_pane_ids_but_not_seats` |
| New session in the same non-daemon process | New session inherits the requested name and its mail | `test_a_new_session_in_the_same_process_replaces_the_old_one` |
| New boot on the same host; ordinary terminal claims an older name | New session inherits; old-process hooks and receipts are rejected | `test_a_rebooted_machine_takes_its_earlier_agents_offline`, `test_old_hook_receipt_cannot_acknowledge_inherited_mail[reboot]` |
| Nested session requests an ended agent's name | Automatic name claim does not inherit | `test_a_one_shot_doesnt_take_an_offline_name_or_its_mail` |
| Either participant is a daemon conversation; automatic name claim | Existing conversation retains its name and mail, including while ended | `test_automatic_name_takeover_requires_neither_session_to_be_a_daemon_thread` |
| Explicit rename claims an eligible ended agent, including a daemon conversation | Renaming session inherits; previous receipts and hooks cannot affect it | Same daemon test; `test_old_hook_receipt_cannot_acknowledge_inherited_mail[rename]` |
| Same session resumes in a new process | Session retains its mailbox; old-process hooks are rejected | `test_delayed_hook_from_the_older_process_of_a_resumed_session_is_ignored`, `test_daemon_restart_rejects_late_hooks_and_keeps_pending_mail_wakeable` |
| Terminal process switches A to B to A | Explicit SessionStart may return ownership to A | `test_same_process_switching_back_to_a_session_is_allowed` |
| Different daemon threads share a process token | Shared process does not authorize inheritance or an old thread's return | `test_shared_daemon_token_does_not_transfer_names_mail_or_resume_rights` |
| Superseded session explicitly resumes in a different process | SessionStart can register it again; ordinary stale hooks cannot | `test_explicit_resume_in_new_harness_is_allowed`, `test_retired_hooks_cannot_replace_successor_after_restart` |
| Both mailboxes contain the same unread message | Transfer produces one entry, preserving notify over board and direct over indirect | `test_a_takeover_merges_inboxes_keeping_the_stronger_delivery` |
| Old receipt arrives after transfer and server restart | Old session's acknowledgment fails; successor still has the unread message | `test_inbox_receipts_survive_restart_and_takeover` |

The tests are in [test_server.py](tests/test_server.py), [test_lifecycle.py](tests/test_lifecycle.py), [test_storage.py](tests/test_storage.py), and [test_mailbox.py](tests/test_mailbox.py). Registration's seat rule is separate from automatic name selection; do not accidentally apply the nested/daemon name restriction to every form of explicit handoff. Daemon hook input clears inherited tmux identity before these decisions run.

## History lookup benchmark

From the clone root:

```bash
uv run --locked python benchmarks/hook_history.py
uv run --locked python benchmarks/hook_history.py --profile
```

It times a hook and a receipt inbox read for one agent with one old unread message and growing retained history, using temporary state and real commits (one warm-up, 30 samples each). It ignores installed settings and never contacts a running bus. Pending mail is found by binary search over the ordered message list; most of a hook's time is the SQLite commit.
