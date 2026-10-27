# Qualification boundary

The 0.3.7 application passed 248 Python tests in the private development environment
(240 pre-milestone baseline plus 8 review tests), including retained-evidence tests.
The public snapshot deliberately excludes private fixtures. Those optional tests
skip; synthetic coverage remains executable without media, hardware, models or keys.

Rendered Chromium/Playwright 1.56.1 verified durations, source ordering, collapsed
editable clips, persistent confirmation/rejection, metadata population, editable
source mappings, backend previews and partial release. Regression checks cover
stale prior assignments and edits made while a preview response is outstanding.

The helper was deployed as 0.3.7 after idle checks and a private state/config/database
backup; ARM remained at its qualified 0.3.6 overlay revision. This milestone does not
claim a new physical-disc run. Earlier TV/movie/audio tests and physical runs are
not substitutes for qualification of arbitrary external ARM bases or hardware.

`source-manifest.json` records SHA-256 hashes of the deployed application and ARM
overlay source. Git commit, image revision label, SHA tag and Actions digest bind the
public source to the published helper. Build timestamps/OS packages can change:
deploy immutable digests rather than expecting byte-identical future rebuilds.
