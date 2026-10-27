# Experimental local media transfer (not deployed)

This branch adds a background **copy** worker inside the existing helper process.
It is disabled by default. It does not change recognition, FLAC tagging, detention,
naming, publication decisions or ARM. It adds no network protocol, service or model.
Do not use the stable 0.3.7 image to enable this experimental feature: it does not
contain the worker. Build this branch locally for isolated qualification first.

Software qualification: **278 Python tests passed, nine optional private-fixture
tests skipped**, including 39 new transfer/configuration/API cases. The existing
Compose template resolves with transfers disabled and four read-only placeholder
mounts. Tests use synthetic files, temporary directories and simulated dataset
identity checks; no real media dataset or production service was used. Actual ZFS
bind-mount/ACL qualification and production deployment remain deliberately undone.

## One existing `.env`, no new configuration system

Docker Compose reads the existing helper `.env` for **interpolation**. That alone
does not pass variables into Python. `compose.yaml` explicitly forwards every
runtime `TRANSFER_*` setting under `helper.environment`; `config.Settings` reads
those values using the existing `os.getenv` mechanism. Host paths, supplemental
groups and mount read-only flags are Compose settings, not application settings.

Append the new transfer block from the **existing** `.env.example` to your **existing
private helper `.env`**, once. Do not replace that file or create another env file.
No private `.env` is distributed or changed by this implementation. If using a
site-specific Compose, add these environment/mount/group entries to that existing
file and have them resolve from that same `.env`; `.env` is not an `env_file` list
automatically injected into containers. Never commit private values.

Runtime keys:

| Key | Default / meaning |
| --- | --- |
| `TRANSFER_ENABLED` | `false` — no thread or ledger writes while disabled |
| `TRANSFER_DRY_RUN` | `true` — plan/validate only; no destination changes |
| `TRANSFER_INTERVAL_SECONDS` | `60`, integer 1–86400; reconciliation/retry interval |
| `TRANSFER_DELETE_SOURCE` | `false`; `true` is deliberately rejected |
| `TRANSFER_VERIFY` | `sha256`; no weaker verification mode |
| `TRANSFER_SOURCE_ROOT` | `/media/released`; must equal the helper's `RELEASED_PATH` |
| `TRANSFER_STATE_DIR` | `/state/transfers`; must be within the existing `STATE_PATH` |
| `TRANSFER_MOVIES_DIR` | `/transfer/movies` in Compose |
| `TRANSFER_TV_DIR` | `/transfer/tv` in Compose |
| `TRANSFER_MUSIC_DIR` | `/transfer/music` in Compose |
| `TRANSFER_AUDIOBOOKS_DIR` | `/transfer/audiobooks` in Compose |
| `TRANSFER_{MOVIES,TV,MUSIC,AUDIOBOOKS}_DATASET` | Exact expected ZFS dataset source; required for each configured destination when enabled |

Empty `TRANSFER_<CATEGORY>_DIR` disables routing for that category; leave its
dataset setting empty as well. Other categories, including `extras/`, are skipped.
Paths must be absolute, non-overlapping and free of traversal/symlink components.

Compose-only keys in the **same `.env`**:

- `HOST_MOVIES_PATH`, `HOST_TV_PATH`, `HOST_MUSIC_PATH`, `HOST_AUDIOBOOKS_PATH`: existing
  local dataset directories. Long bind syntax uses `create_host_path: false` so
  Compose will not create a missing host directory as a substitute for a dataset.
  Defaults are read-only `/dev/null` placeholders, not writable media directories;
  they keep disabled installations usable without requiring TrueNAS mounts.
- `TRANSFER_DESTINATIONS_READ_ONLY=true`: retain for initial dry-run; set `false`
  only when deliberately testing real copies. Omitted/unconfigured category mounts
  stay on `/dev/null`; do not point unused mounts at real datasets unnecessarily.
- `TRANSFER_GID`: supplemental destination-access group ID, in addition to existing
  primary UID/GID and optical/video groups. Set actual deployment values privately;
  application code never assumes a media owner/group. Existing TrueNAS ACLs must
  grant traversal, directory creation, file write, hard-link publication and removal
  of worker staging files. The worker never runs `chown`, `chmod` or repairs ACLs.

Choose the music library **or archive** by changing `HOST_MUSIC_PATH` and
`TRANSFER_MUSIC_DATASET`; the container target can remain `/transfer/music`.
No rebuild is needed for routing or permissions changes after this code is installed.

## Missing/unmounted dataset guard

Directory existence is not proof of a mounted ZFS dataset. Before copying and again
before publication, the worker checks Linux `/proc/self/mountinfo`: the exact
destination path must be a mount whose filesystem is `zfs` and whose dataset source
matches the configured `TRANSFER_<CATEGORY>_DATASET`. It also checks the opened root
has not changed. Inspect this inside the intended helper container and verify it
against the TrueNAS pool/dataset configuration. A generic illustrative dataset name
is `pool/library/movies`; replace it privately, not in source control.

If the intended child dataset is unmounted and its underlying parent directory is
bound instead, its ZFS source differs and copying is refused. On a platform where
container mount information does not expose the expected ZFS source, **do not weaken
the guard**: this experimental implementation cannot safely activate there. Bind
the dataset itself (or a directory inside it), not an unnecessary entire pool.
Actual TrueNAS permissions/mount behavior have not been physically qualified here.

## What is transferred

The worker projects only `state == released` sources from the existing loaded cycle
records (`/state/cycles/<id>/record.json`). It does not scan files for readiness or
use ARM completion notifications, file timestamps or directory creation as proof.
A valid cycle ID, job GUID, source ID, release timestamp and final path are retained.
Recorded size, when present, must match; older records without size are checked via
an unchanged open-file fingerprint and full SHA-256 verification.

`movies/`, `tv/`, `music/` and `audiobooks/` route to their configured roots, preserving
all category-relative directories and Unicode filenames. Partially released cycles
are supported file by file. Historical successful releases are discovered on the
next scan without re-ripping. Detained/unassigned/incomplete outputs are never copied.
The worker reads the Store already owned by this process; external record edits are
not live-imported. Restarting the helper reloads its normal authoritative records.

## Publication, recovery and collisions

The worker opens path components without following symlinks; only regular released
files under the release root qualify. It copies to a unique hidden `.partial` file
**on the destination filesystem**, fsyncs it, independently hashes destination and
source, and checks source identity/size/mtime/ctime and path stability. A hard link
atomically publishes the final name **without replacing anything**. No cross-dataset
rename is assumed. Destination filesystem support for hard links and directory
fsync is required; otherwise the entry fails closed and is retried.

SQLite under the existing state volume records pending, active, success, failed,
skipped, conflict and dry-run statuses, attempts, timestamps, errors, source/destination
identity, checksum and staging proof. A file/process lock serializes workers sharing
that state directory. Do not run multiple helper replicas with separate ledgers on
the same release/destination roots. A successfully transferred identity is terminal;
the ledger is not a library backup or continuous destination-integrity monitor.

Interrupted copies retry automatically. If final publication preceded the ledger
success commit, restart recovery requires a durable verified checksum **and the
worker's retained staging/final inode association** before adopting the final file.
An unrelated pre-existing final file is a collision even if its bytes match. Nothing
is overwritten. Disk-full, permissions, mount changes and verification failures are
logged and retried at the configured interval. Failures never delete the source.

Only the worker's own known staging file is cleaned up. A crash before its inode was
recorded, or after success was committed but before staging unlink, can leave a hidden
orphan. It does not block retries or expose a partial final file. Inspect such files
against the ledger manually; no broad or recursive cleanup is performed.

**Source deletion is not implemented.** Existing correction previews, audits and
recovery still reference released source paths. This branch cannot prove deletion
safe for all those consumers. `TRANSFER_DELETE_SOURCE=true` is rejected rather than
silently ignored. Copies retain the original immutable releases and their records.

## Monitoring and safe enablement (operator procedure; not executed here)

1. Keep production transfers disabled. Build this branch and run synthetic tests:
   `docker run --rm --entrypoint python <experimental-image> -m pytest -q`.
2. Back up existing state/private configuration. When idle, add settings/mounts/groups
   to the existing private `.env`/Compose, using separate **test** ZFS datasets and
   synthetic released records in isolated state. Never point tests at real libraries.
3. Check `docker compose config --quiet`; inspect resolved mount/runtime values
   privately. It can reveal private configuration, so do not post its full output.
4. Explicitly select the experimental image. Keep `TRANSFER_DRY_RUN=true`, source
   deletion `false` and destination mounts read-only. Set `TRANSFER_ENABLED=true`
   only after verifying expected dataset identities and all path/ACL settings.
5. Inspect authenticated `GET /api/transfers` (for example
   `curl --user operator http://localhost:8080/api/transfers`, which prompts for the
   existing GUI password). The API lists bounded recent entries, aggregate status
   counts and scan errors; normal helper logs include per-entry start/result/retry
   errors. There is no new web service or unauthenticated control endpoint.
6. In isolation, allow writable destination mounts and turn dry-run off. Verify copied
   hashes, nested routes, collisions, restart recovery and permission failures.
7. Any future production enablement is a separate operator decision after TrueNAS
   qualification. To stop, set `TRANSFER_ENABLED=false` in the **same `.env`** and
   recreate helper only when idle; preserve the ledger. Do not change ARM or records.
