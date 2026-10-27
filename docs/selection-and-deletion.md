# Video selection and Delete run — 0.3.8 beta

Requires helper 0.3.8 **and the compatible ARM detention-v7 overlay**. Updating only
the helper cannot change extraction policy inside an older ARM container. GHCR
provides the helper only; the ARM overlay is still built locally from the qualified
private base. No local model hosting or experimental transfer code is included.

## Selection before extraction

ARM's existing prescan is reused; no second MakeMKV scan or ripper is introduced.
Numeric MakeMKV source IDs, durations, chapters, playlist filenames, segment maps
and stream signatures are retained. Four or more similarly sized 15–75-minute
titles form a strong provisional episode signal (four over 40 minutes is one common
case). Three titles can qualify when their known segment maps are disjoint. This
does not establish editorial TV identity, episode numbers or content certainty.

Combined candidates are compared with the episode-duration sum. A matching segment
union of independent episodes establishes a combined relationship; matching chapter
totals can corroborate a duration-based candidate. A duration-only candidate remains
ambiguous. Identical segment playlists require matching stream evidence or the same
playlist identifier before deduplication. Unexplained long features, overlapping
playlists and comparable movie cuts are not silently treated as episodes or movies.

Clear technical TV structure selects individual episode candidates while recognition
continues. Clear standalone movie structure uses ARM's native main-feature ranker.
Ambiguous structures wait for additional semantic/source evidence instead of choosing
the longest title. No fixed waiting timer or universal recognition-first gate exists.
The defaults remain movie minimum 600 seconds and TV minimum 900 seconds. Music/CD
TOC extraction and verified scoped short-episode/extra associations are unchanged.

TV source IDs still map in ascending order to established episode numbers unless an
applicable verified mapping overrides that assumption. Count conflicts remain detained.
An existing extraction reservation is immutable: late recognition cannot switch a
running MakeMKV title, rename incomplete files or create another ARM job.

**Analyze retained video scan** in the GUI is read-only. It shows current technical
analysis alongside the actual selected source IDs from the historical manifest.
It does not rewrite a historical decision or claim that missing episodes were ripped.

## Delete run

This is an explicit destructive action, never automatic cleanup. It is for completed
detained/failed test runs, not an active-rip cancellation mechanism. A running source,
model/fallback request, release worker, original ripper PID or optical process blocks
deletion. Runs containing any released output are conservatively refused, including
partial releases; the ordinary cleanup action never deletes released media.

Open **Delete run…** for the selected cycle. The authenticated preview shows the ARM
job ID, cycle/GUID, identity, status, exact verified files, actual sizes/count, supported
ARM operation and queue state or a concrete reason cleanup is unavailable. Opening it
does not delete anything. Type the displayed ARM job ID and explicitly confirm.
The server requires an unexpired, cycle-specific preview token and rechecks the
filesystem and backend identity. Browser-supplied filesystem paths are never accepted.

Files must be registered outputs/retained partials or the known manifest/progress
state under that exact job-ID/GUID detention root. Symlinks, special files, shared
hardlinks, other-cycle references and unregistered files fail closed. A changed
inventory requires a new preview. Only individually verified files and explicitly
inventoried empty directories are removed; there is no recursive directory delete.
Unrecognized abandoned scratch files may require manual investigation rather than
being swept away. Diagnostic cycle records, model observations and prescan evidence
remain private in persistent state.

The installed ARM `svc_jobs.delete_job` operation was inspected: it removes job,
track/config rows and clears matching current/previous drive references; it does
**not** remove media. The overlay wraps it with finalized-job/GUID/process checks
and the same job reservation lock. File cleanup and ARM deletion are tracked as
separate steps. The manifest remains until the guarded ARM operation is attempted.
Afterward the helper verifies job absence, no optical processes, and the configured
drive's ready/unowned state. Row disappearance alone is not reported as readiness.

Partial failures retain a deletion audit and quarantine the run from further release.
No destructive operation automatically retries. Inspect the reported errors, resolve
the issue and request a **fresh preview** before another explicit attempt. Uncertain
ARM replies are reconciled by identity/absence; a reused numeric ID is never deleted.
The audit records steps, deleted files/bytes, timestamps, ARM result, queue result
and errors; it is not included in sanitized reference exports.

Once cleanup reports ready, remove the old disc, close the tray, then manually reopen
it **empty** to initiate a new capture cycle. Deletion does not eject, start a rip,
kill processes or restart containers. Other historical jobs are never cleaned up as
a side effect. Harmless released advisory-lock files/diagnostic prescans may remain;
their presence is not an active extraction reservation for a new job GUID.

## Qualification

Synthetic selection, deletion/API/queue tests and rendered Chromium checks cover
both features without touching real media. Private retained-scan evaluation confirms
that the former combined-title regression now proposes the six independent sources
and identifies the combined segment union, without altering the old extraction.
A new physical rip and user-initiated deletion remain separate physical verification.
