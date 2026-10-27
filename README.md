# ARM companion / recognition-helper

> **Experimental branch:** local, opt-in media transfer is implemented here but
> not deployed or part of the stable 0.3.7 image. Defaults remain disabled/dry-run;
> source deletion is unsupported. See [transfer configuration and safety](docs/transfers.md).

Beta **0.3.7**: a camera-assisted recognition and manual-review helper for an
existing compatible Automatic Ripping Machine (ARM) installation. Linux/amd64.

Two local vision models independently inspect disc captures. Optional masterlists
provide reference facts and scoped source associations. Unresolved outputs remain
detained. Manual review displays actual extracted durations before scan estimates,
groups episode candidates and short clips, and offers persistent per-cycle
masterlist confirmation/rejection. Confirmation prepares editable source mappings
and exact backend filename previews; **only Submit correction & release authorizes
publication**. Unassigned or unfinished sources remain detained.

## Image and architecture boundary

`ghcr.io/tammuzgreeen/arm-companion:0.3.7-beta` contains **the helper only**, not ARM,
MakeMKV licensing, drive-control services or local AI hosting. Use the immutable
digest reported by [Actions](../../actions) for reproducible deployments.
The source includes an ARM overlay, but its compatible locally patched base image
is **not public or built by this workflow**. This is not a standalone ripper stack.
See [deployment](docs/deployment.md), [verification](docs/verification.md) and
[changelog](CHANGELOG.md).

## Development

```sh
docker build -t recognition-helper:test .
docker run --rm --entrypoint python recognition-helper:test -m pytest -q
```

Dependencies are pinned in `requirements.txt`; rendered Chromium tests use
Playwright 1.56.1 and `tests/browser/package-lock.json`. Synthetic tests require no
disc, model endpoint or paid key. Optional retained-evidence tests skip when private
fixtures are absent; no real captures, media, runtime records or personal masterlists
are distributed. `examples/masterlists/` contains synthetic data only.

Preserve the existing MIT license. ARM and other external components retain their
own licenses; this repository does not redistribute the private ARM base image.
