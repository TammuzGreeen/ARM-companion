# 0.4 beta: bounded routine polling

- The authenticated `/api/status` endpoint returns live status and lightweight
  historical cards. `/api/state` remains backward compatible; authenticated
  `/api/cycles/{id}` returns complete retained detail.
- Summary projection happens under the existing store lock without copying raw
  probes, model responses, prescans or audit histories.
- Unchanged saves do not rewrite JSON/Markdown or advance `updated`. Actual
  transitions, nested evidence changes and audit events retain the existing
  atomic JSON replacement/fsync behavior. No evidence is truncated.
- Cards retain their DOM identity. Retained captures load when opened, and full
  findings load on demand with cycle/revision checks. Nested disclosures and
  review drafts survive polling.
- Polling is single-flight, scheduled after completion, with superseded-response
  rejection. Hidden tabs poll more slowly; camera preview remains active when
  the camera is connected. Capture and recognition image quality is unchanged.

Recognition agreement, source selection, publication, detention, collision,
deletion and drive-recovery policies are unchanged. No broad lock refactoring,
media transfer or automatic cleanup is included.

## Validation

Synthetic regression tests cover unchanged persistence, nested changes, API
authentication/compatibility and detail isolation. Chromium tests cover review,
masterlist confirmation, preview races, explicit deletion, stable historical
cards/images, nested disclosures, focus, unsaved drafts and single-flight polls.
Benchmark results must distinguish isolated API/browser replay from deployed
measurements; do not infer sustained CPU savings or a memory-leak fix.

Pre-deployment qualification: 303 tests passed with private fixtures; the clean
public image passed 294 tests with 9 private-fixture skips. Both Chromium flows
passed, including structural card changes, newly arriving captures, stale
success/error responses and delayed detail collapse/revision/removal cases.

An isolated API test using 25 retained cycles / 254 sources returned a 112,556-byte
status response with a 39.64 ms warm p95 over 30 requests. This is not a deployed
network latency claim. An ephemeral copy of the same records survived 875
unchanged save attempts over 30.11 seconds with zero writes and unchanged bytes
and mtimes. These measurements contain no published runtime records or captures.

Deploy only while ARM, capture and model work are idle, retaining a state/config
backup and the previous immutable helper image. The ARM backend need not be
replaced for this helper-only update.
