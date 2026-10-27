# Changelog

## Unreleased — experimental/media-transfer

- Opt-in background transfer of authoritative released files to local ZFS bind
  mounts, preserving media hierarchy with SHA-256 and atomic no-clobber publication.
- SQLite retry/recovery ledger and authenticated `/api/transfers` status; no release
  record edits or source deletion. Disabled and dry-run by default.
- Transfer settings extend the existing `.env.example`/Compose mechanism only.

## 0.3.7 beta

- Manual review: actual extracted durations before labeled scan estimates, honest
  unknowns, stable source IDs, candidate-first ordering, collapsed editable short
  clips and expandable actual paths.
- Relevant masterlist suggestions with matching/missing/contradictory evidence;
  persistent per-cycle confirm/reject/reconsider and retained reference provenance.
- Confirmed identity prepares editable, explicitly assumed source-order mappings
  and backend filename/destination previews without inference or publication.
- Existing correction submission retains partial release, completion/collision
  checks and proposal/final assignment audit history. Stale preview responses cannot
  authorize a different edited payload.
- Sanitized helper-only GHCR build; external compatible ARM overlay remains required.

## 0.3.6 beta

- TV metadata reconciliation, duration-filtered source selection and ascending
  source-to-episode mapping with scoped short-episode exceptions.

## 0.3.5 beta

- Audio work/track reconciliation, lossless-tag preparation and detained review.
