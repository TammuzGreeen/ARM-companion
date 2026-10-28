# Deployment and rollback

Version **0.4.0** is a helper-only performance update; keep the compatible
detention-v7 ARM backend. Back up state/config, verify no ARM/capture/model work
is active, and deploy the verified immutable helper digest. Retain the previous
helper digest for rollback. No media cleanup or transfer is part of deployment.

For the **0.3.8** backend features, first qualify/build the companion detention-v7 overlay from the same
source commit and your existing compatible private ARM base. Its source-selection
and guarded deletion code runs inside ARM, not in the GHCR helper. Replace ARM and
helper only after checking all drives, capture and inference work are idle and
backing up state/configuration/database. Do not use the helper-only upgrade shortcut
below when moving from an older overlay. See `selection-and-deletion.md`.

## Prerequisites and boundaries

Only Linux/amd64 is validated. The published image runs recognition-helper/FastAPI,
its GUI and native OpenCV/FLAC dependencies. It does not run ARM or host AI models.
Keep your compatible ARM companion, optical-drive privileges, MakeMKV license,
database/configuration persistence and existing ripping service in place.

`backend_overlay/Dockerfile` requires a separately available, compatible patched ARM
base. Its default names the development base, not an image available from GHCR.
Do not substitute stock ARM blindly: `apply.py` validates the supported base source
hashes and fails closed. Build with `--build-arg ARM_BASE=your-compatible-base` only
after inspecting the overlay and qualifying that base. The existing overlay must
expose helper job/status/source completion APIs and honor detention roots. This
repository does not provide a publicly reproducible ARM base or a full-stack Compose.

Both services must see the **same media files**: helper `/media`, ARM
`/home/arm/media` (or set `ARM_MEDIA_PREFIX` privately to match). ARM's detention
root must be `/home/arm/media/detention`. Persist `/state` across upgrades; it holds
cycle records, calibration, decisions, confirmations and optional references.
Masterlists belong under `/state/masterlists`; `examples/masterlists` is synthetic.
Pass the same `TV_MIN_LENGTH_SECONDS=900` to helper and ARM. ARM must have optical
drive/SG device access; the helper requires read access to `/dev/sr0` and camera
access to `/dev/video0`. Do not expose these devices to CI tests.

## Start the helper

1. Copy `.env.example` to private `.env`; replace network, model host, media/state
   locations, UID/GID and device settings. No private configuration is tracked.
2. Create state/media directories and a strong password file, readable by the
   container user; restrict file and directory permissions. Do not put the password
   in the Compose file or command line. GUI username is `operator`.
3. Join the existing ARM Docker network. Ensure ARM is reachable at `ARM_URL`.
4. Configure the **two** independent local model names and existing Ollama endpoint.
   This project does not install or host them. External paid fallback defaults to
   disabled; no OpenAI credentials are required for builds or normal local testing.
5. `docker compose pull helper`, then `docker compose up -d helper`.
6. Check `docker compose ps` and authenticated `/health`: verify `running`, version
   and `backend_connected`. Inspect the GUI's backend/model/reference state privately.

Compose binds only loopback by default. For other clients use a protected TLS reverse
proxy or restricted trusted network; HTTP Basic authentication is not encryption.
The container health check checks helper liveness, not ARM health; explicitly verify
`backend_connected` before inserting a disc. No automatic initial disc insertion is
needed for reviewing retained detained jobs.

## Reproducible upgrade

- Record the currently running image **digest**, source revision and Compose/private
  settings. Read the Actions summary and use `HELPER_IMAGE=ghcr.io/tammuzgreeen/arm-companion@sha256:...`.
- Check ARM is idle, the tray/camera is not capturing and no local/fallback model
  request is active. Do not interrupt ripping or inference.
- Back up `/state`, private environment/password files, ARM configuration and its
  database consistently (SQLite backup API, not a live partial database copy).
  Do not put these backups in this repository.
- Pull the selected digest, then replace **helper only** with
  `docker compose up -d --no-deps helper`. Do not recreate ARM unnecessarily.
- Verify authenticated health, ARM connectivity, both model endpoints, references
  and read-only opening of retained jobs. Test mutations only in isolated fixtures.
- Masterlist confirmation does not publish. Review editable mappings and backend
  destinations, then explicitly submit a correction to release completed outputs.
  Unassigned sources stay detained; collisions and incomplete files remain protected.

## Rollback

When idle, restore the prior helper image digest and recreate helper only. Preserve
all media. Keep the state/config backup before any downgrade; restore it only after
assessing records written by the newer helper and never erase newer release history
or overwrite media as a blind rollback. Do not automatically release or delete
historical detained files. The ARM overlay is unchanged by the 0.3.7 helper upgrade.
