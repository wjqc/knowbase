# Durable local directory monitoring

The original one-shot command still works. To retain directory choices, exclusions,
and per-directory polling intervals across terminal sessions:

```sh
knowbase documents register ~/Documents/reference --scope work --exclude '*.draft.md' --interval 10
knowbase documents roots
knowbase documents sync --saved --retries 2
knowbase documents watch --saved
knowbase documents unregister ROOT_ID
```

Registration saves configuration in the local SQLite database only. It does not
start an OS service, alter Git configuration, or share absolute paths. Unregistering
retains imported snapshots and search records. Run `watch --saved` explicitly in a
terminal; Ctrl-C stops it. Changes to registrations are picked up between scans.
A registered directory whose path is replaced by a symlink is rejected.

For temporary monitoring without saving a directory:

```sh
knowbase documents watch ~/Documents/reference --scope work --interval 10 --progress
```

`--progress` on direct sync/watch emits JSON progress on stderr (scan, prepared,
apply, complete; counts only, no text). Saved-directory runs emit JSON events on
stdout, including attempts and per-file progress. `roots` shows the last result
and timing. `--retries 0..5` controls bounded retries with interruptible exponential
waits; the default is two retries. Persistent parse failures are not silently
ignored, but are retried on each future polling pass. Successful files avoid
reparsing during a retry.

Polling intentionally reads file hashes rather than relying on filesystem events
or mtimes. This catches same-size, same-mtime edits, but costs disk I/O on every
pass. Intervals are per directory, measured after the previous scan completes;
roots run sequentially. Unregistering skips queued scans and cooperatively cancels an active scan at the
next file/checkpoint without stopping other directories. A blocking parser must
return before that cancellation takes effect. SQLite serializes unregister with
an already-running write transaction; a committed batch is retained. Ctrl-C exits the
CLI with status 130. Progress does not estimate a total or completion time.
Cancellation is checked between files and before transaction commit, not inside a
blocking parser call. Ctrl-C interrupts Python execution where supported. A
cancelled transaction rolls back local index changes; content-addressed source
artifacts written before cancellation can remain safely for later reconciliation.
This is local single-user tooling, not a distributed job scheduler.
