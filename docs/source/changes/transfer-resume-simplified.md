# Change note: transfer resume is driven by the reconnect, not by a public call

> Per-change note for the simplification of the transfer resume path in
> `connect_tcp`. `resume_transfers` is removed. No PR number is assigned yet;
> rename the file when the PR is opened.

## Core logic of this change
- Problem: resuming an interrupted transfer had two drivers (`resume_transfers`
  and a `/resume_trans` receive leg), a sender-side `_run_resume_send_leg`
  wrapper, and a separate progress-table lookup. A stale port announcement could
  also be picked up on resume.
- Approach: on every connect each side calls `_request_resumes`. The receiver
  sets the offset from its partial file and sends `/resume_trans <fid> <offset>`.
  The sender answers with `/file <name> <fid>` on the control channel and sends
  the remainder through `file_transfer_mode(resume_offset=...)`. The receiver
  matches that `/file` line to its progress entry by fid, sender and file name,
  and appends only when the size equals the recorded total.
- Old port announcements are dropped (`_forget_transfer_port`) before the resume
  `/file` is sent, so the sender waits for the new listener, not a dead one.

## Modules and interactions
- `PyFlow/network_api/connect_tcp.py`:
  - `_resumable_entry`: module-level matcher for a received `/file` line.
  - `_request_resumes`: one per side (`TCP_Server_Base`, `TCP_Client_Base`).
    The server calls it from `_resume_after_connect`; the client from the
    connect thread.
  - `resume_trans`: one per side, the only sender-side resume entry.
  - `file_transfer_mode_recv`: refuses a continued leg whose size differs from
    the recorded total (`resume rejected: ...`); keeps the partial file and
    the progress entry.
- Tests: `test/unit/network_api/test_resume_matching.py` (matching rules);
  `test/integration/network_api/test_tcp_transfer_resume.py` (upload and
  download resume, encrypted resume, total mismatch refused).
- Docs: `File_Transfer/File_Transfer.rst`, `Network_APIs/TCP_Server_APIs.rst`,
  `Network_APIs/TCP_Client_APIs.rst`.

## Implicit assumptions and easy-to-forget details
- A `/file` line is a new transfer unless `_resumable_entry` matches. A new
  transfer truncates the target path, so any change to the matcher must keep the
  three-way check (fid, sender, basename).
- The progress file stores peers as JSON lists. Matching compares `tuple(...)`
  on both sides; a comparison without `tuple()` silently never matches after a
  reload.
- A continued leg is refused before `full_path` is assigned, so the error path
  never removes the partial file.
- `_forget_transfer_port` must run before the `/file` is sent, not after: the
  announcement can arrive before the sender starts waiting.

## Changing this in six months
- `resume_trans` is kept as the single sender entry and is still callable as a
  public API for an on-demand resume. Do not reintroduce a receiver-side leg.
- Gap: the auto-reconnect of a client downloading from the server is covered by
  an integration test that calls `_request_resumes` directly, not by a real
  disconnect followed by `connect()`. Add an end-to-end disconnect test before
  relying on the automatic path.
- Gap: the encrypted path is covered for download resume only.
- Translations: the `.po` catalogues under `docs/source/locale/` still describe
  `resume_transfers`. They are not regenerated here (`sphinx-intl update`).
- Duplication: the server and client copies of `file_transfer_mode_recv`,
  `resume_trans` and `_request_resumes` are still written twice. Merging them is
  a separate refactor.
