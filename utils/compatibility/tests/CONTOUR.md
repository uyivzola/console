# Contour compatibility and chart repository migration

The [official installation guide](https://projectcontour.io/getting-started/#option-2-helm) now uses https://projectcontour.github.io/helm-charts/. Its [chart README](https://github.com/projectcontour/helm-charts/blob/main/charts/contour/README.md) identifies the chart as a fork of the discontinued Bitnami chart.

The [official index](https://projectcontour.github.io/helm-charts/index.yaml) supplies exact stable appVersion/chart-version mappings. The recorded applications map to 1.32.0/0.1.0, 1.33.0/0.2.0, and 1.33.7/0.8.0. Chart numbering restarts at 0.x, so keeping obsolete Bitnami 21.x values would select the wrong maximum chart and request unavailable packages from the new repository.

The scraper stages the complete migration before one write. It removes unmatched old chart versions while preserving every historical compatibility record, image, summary, requirement, incompatibility, and EOL value. Images change only for the three mapped applications, using actual successor chart rendering. Empty or malformed sources and failed chart renders abort before writing. After migration, missing exact chart mappings can be filled for stored releases, even when the compatibility matrix no longer contains those releases; a newer stable chart for the same application also refreshes its chart metadata and images.

New application compatibility comes from the [official matrix](https://projectcontour.io/resources/compatibility-matrix/), with explicit finite Kubernetes lists. Candidate reduction keeps changed support boundaries, the latest application, and the latest chart-backed candidate; existing historical records are never dropped during migration. New releases without a chart use version-matched images from their immutable release manifests.

This metadata correction is not a claim that a production Bitnami Helm release can be upgraded in place to the fork without reviewing its values and resources.

## Verification

From the repository root, with the compatibility Python dependencies installed:

```sh
env -u EXA_API_KEY -u OPENAI_API_KEY python -m unittest discover -s utils/compatibility/tests -v
```

There are 26 Contour tests and 2 shared image tests. Coverage includes exact application mapping, deprecated/prerelease rejection, wrong names and conflicting metadata, chart numbering reset, maximum-chart consumer selection, no obsolete chart requests, historical preservation, late chart backfills, same-application chart updates, protection against catalog rollback, no-op repeats, malformed sources, failed renders, and release image verification.

The fixture index contains the nine official entries observed September 8, 2026. The compatibility HTML fixture preserves the relevant official table. The release YAML fixtures provide the two recorded 1.33 boundary/latest manifest references.

Live validation used Helm4.2.4 with isolated Helm/Docker configuration and optional paid summaries disabled. All three mapped charts rendered successfully. The per-app and aggregate schemas pass, all unrelated add-ons are unchanged, and a live rerun makes no write or chart-render call. No cluster installation or workload execution was performed.

The downloaded chart0.1.0,0.2.0,0.8.0 archives were independently checked against the index SHA256 values and internal Chart.yaml application versions. Source checks and local rendering do not constitute a deployed-cluster integration test.
