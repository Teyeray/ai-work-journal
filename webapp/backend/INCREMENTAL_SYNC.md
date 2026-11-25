# Incremental dashboard sync (bounded native-source reads)

Manual `/sync` and the 300-second job share the same adapters. Reporting collectors
remain stateless and retain their existing interfaces/default bounds. AW/browser
history, screenshot capture, Vision OCR and report routes are unchanged. Git's HEAD
checkpoint and Pi/Claude's byte cursors/targeted projections remain in place.

## Codex

The dashboard calls the **same `_codex_file` line processor as reporting**, supplying
bounded complete records and a small parser state. It does not maintain a second
implementation of Codex turn/tool/dedup semantics.

* Enumerate JSONL metadata; select at most configured `max_files` (default 400), by
  recent mtime in the existing two-day window. In-progress bootstrap files remain
  eligible. No epoch-wide file content bootstrap.
* Persist device/inode, signature, safe byte offset, original bootstrap boundary,
  whitelisted session context (cwd, parent/kind/fork), active native session ID and
  the last two parser turns. No arbitrary native metadata/config/auth is copied.
* Unchanged completed files and unchanged trailing fragments cause **zero content
  reads**. Append processing starts at the safe offset. Incomplete bytes are never
  committed; failed reads/parses leave that file's checkpoint unchanged. Restart
  resumes the committed state, including cross-offset duplicate-turn detection.
* Total content-read budget per poll is `max_bytes` (default 24 MiB). Per-file
  chunks default to 8 MiB (`agent_sources.sync_chunk_bytes` may override this).
  A pending large file resumes over subsequent polls instead of failing all files.
* Initial files within the cap start at zero. Oversized files start at a bounded
  tail, reserving up to 64 KiB (and at most one-quarter of the cap) for the initial
  header. Only complete `session_meta` records from that header seed the shared
  parser; the initial partial tail record is discarded. Header plus initial tail
  are bounded by the configured cap. The tail itself can require several chunks.
* Bootstrap records retain the two-day timestamp filter. New records starting at
  or beyond the original EOF accept old timestamps too, even in a chunk that
  straddles bootstrap EOF. Appends are not repeatedly filtered out just because
  their event timestamps are old.
* File failures are reported individually and do not roll back other successful
  file checkpoints. Rotation/shrink/same-size rewrite starts a fresh bounded read;
  prior journal events and session IDs are never deleted.

## OpenCode

No native message/part table-wide metadata query or retained-ID inventory remains.
The algorithm does not need a `time_updated` index:

1. Compare source **main + WAL** identity/size/nanosecond mtime/ctime signatures.
   A persistently unchanged source returns before opening SQLite. This also works
   across journal restarts. There is no unsafe cross-connection `data_version`
   comparison. Moving the time window forward is safe on a quiet source: the
   checkpoint tracks the next future message timestamp, if any, and invalidates
   the quiet path when it enters scope.
2. On change, enumerate the small **session table**, using a correlated indexed
   `EXISTS` seek for each session's in-window messages. Select at most configured
   `max_sessions` (default 400) **in-scope** sessions ordered by session update time,
   matching the reporting reader's selection bounds. Newer empty sessions cannot
   crowd out an active session with an unchanged old session timestamp. No native
   message/part history scan is involved. For selected sessions, query messages by
   `(session_id, time_created >= two_days_ago, time_created < now)` using the native
   `(session_id,time_created,id)` index, capped at `max_messages` (default 5000).
   Future-message detection likewise uses per-session covering-index seeks.
3. Query part metadata only for those message IDs, using the native message_id
   index and the same part cap/order as the body reader (default 500 per message).
   Hash the bounded metadata to one digest per in-scope session. Persist digests,
   not retained message/part ID lists. A session timestamp bump alone does not
   invalidate its unchanged bodies. Current message/part updates still invalidate
   the digest even when the session timestamp does not change.
4. Only changed in-scope sessions query bodies, by primary session ID followed by
   the indexed time-range/message queries. Metadata, bodies and checkpoint digests
   are read inside **one read-only SQLite snapshot**, including WAL data. A source
   signature change during the snapshot prevents installing a quiet-path token.

No empty two-day scope query scans native message/part history. No bootstrap starts
at 1970. Scope/caps apply to both metadata and body readers. Falling out of scope,
source retention, missing rows, caps, deleted parts or unavailable mounts **never
constitute authorization to delete historic journal evidence**.

## Legacy compatibility and projections

* Schema v6 adds source/file and reverse-link indexes; v7 adds a source/native-part
  expression index. These are one-time local index builds over dashboard events,
  not a data rebuild. Neither migration deletes/replaces events or session IDs.
* Existing event key formats remain valid. An unchanged bounded legacy turn is
  found by its original content key, preserving its ID; new native-part metadata
  is added in place. Subsequent streamed edits update that same event ID by native
  part identity, preserving links. Both old and new day keys are invalidated if a
  timestamp changes. Unseen/out-of-scope earlier evidence is retained.
* Legacy Pi/Claude projections are authoritative if source sessions already exist:
  the first UI sync does not force a historical projection rebuild. A source with
  no projections at all can bootstrap its existing stored events once. Ordinary
  refreshes query only changed/status-expired source/file pairs through the index.
* Optional checkpoints use `incremental-v2`, ignoring the obsolete broad inventory
  format. Initial synchronization reads the bounded scope described above.
  Existing events and sessions are retained. Identical native turns match
  existing keys.
* Checkpoint discovery, event ingestion, changed projections and attribution join
  `BEGIN IMMEDIATE`. Source failures cannot advance unsafe checkpoints; optional
  sources commit independently. Adapter failures have named results. Cursor JSON
  is no longer selected by `ingest.status()` when only status metadata is needed.
* Truly quiet session polls do not read historical event payloads, relink history
  or retry classification of historical unassigned sessions. AW checkpoint/audit
  writes still occur: this is not a claim of zero total SQLite writes.

## Explicit limitations

* Changed session projections still reread that session's stored events. OpenCode
  changed sessions still reread their bounded bodies, rather than individual part
  bodies. Metadata work scales with the **bounded in-scope** messages and parts.
  Session-table sorting remains a metadata cost.
* OpenCode intentionally retains the two-day scope and configured session/row
  caps. Sessions outside the in-scope session cap and messages outside the time/row caps
  are not newly imported. Existing journal evidence is retained. OpenCode relies
  on native message/part `time_updated` semantics; edits preserving all update
  metadata cannot be detected without content reads. No deletion reconciliation
  is attempted from bounded reads.
* Before the first native-part mapping warmup, an already-edited legacy part whose
  old content key no longer matches cannot be unambiguously identified. Its old
  evidence is preserved and the current turn may be added separately; unchanged
  legacy turns are not duplicated. Native rows lacking update columns fail safely.
* Oversized Codex bootstrap intentionally omits earlier file content beyond the
  bounded tail. It requires a complete session_meta record in the bounded header;
  otherwise it reports an error and does not advance, rather than guessing native
  identity. A record larger than a normal chunk is read to its next newline
  within the remaining round budget. A record larger than the entire budget
  reports an error without advancing; increase `max_bytes` for that source if
  such a record needs to be ingested. `max_bytes` caps the total even when the
  configured chunk size is larger. Many active files remain subject to the
  configured file count and byte limits.
* Growing in-place rewrites on the same inode remain indistinguishable from append
  for Codex/Pi/Claude. Stat-preserving content edits evade stat change detection.
  Missing/rotated source evidence remains archival. Existing explicit project
  attribution is not overwritten. Git history remains archival after resets.

## Validation

The tests use synthetic fixtures in an isolated environment. With the project
dependencies installed, run these commands from the repository root:

```sh
cd webapp/backend
python -m pytest -q
```

`tests/test_incremental_sync.py` covers quiet skips, targeted append projections,
partial restart recovery, rollback/retry, rewrite/expiry, failure isolation and Git.
`tests/test_bounded_agents.py` adds shared-parser dedup across offsets, independent
unreadable files, oversized-tail chunk progress, old timestamp append, native
indexed empty-scope work bounds (5,000 synthetic old messages), no quiet SQLite
opens, compact cursors, WAL edits without session timestamp changes, legacy event/
session ID retention, old/new day invalidation, out-of-scope evidence retention,
and unchanged legacy UI warmup/status reads.
