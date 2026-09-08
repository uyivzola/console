"""Offline regression coverage for the official Contour repository migration."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import requests
import yaml
from packaging.version import Version

COMPATIBILITY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(COMPATIBILITY))
spec = importlib.util.spec_from_file_location("contour_scraper", COMPATIBILITY / "scrapers/contour.py")
scraper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scraper)
from utils import reduce_versions

FIXTURES = Path(__file__).parent / "fixtures/contour"


class ContourTests(unittest.TestCase):
    def setUp(self):
        self.html = (FIXTURES / "matrix.html").read_bytes()
        self.index = yaml.safe_load((FIXTURES / "index.yaml").read_bytes())
        self.sources = {scraper.compatibility_url: self.html}
        for version in ("1.33.0", "1.33.7"):
            self.sources[scraper.release_manifest_url.format(version=version)] = (FIXTURES / f"release-{version}.yaml").read_bytes()
        self.existing = {"helm_repository_url": "https://charts.bitnami.com/bitnami", "versions": [
            {"version": version, "kube": ["1.33", "1.32", "1.31"], "chart_version": chart,
             "images": [f"docker.io/bitnami/contour:{version}"], "requirements": ["keep"],
             "incompatibilities": [], "summary": {"features": ["Stored summary"]}, "eolAt": "2027-01-01"}
            for version, chart in (("1.32.1", "21.1.4"), ("1.32.0", "21.1.2"))
        ]}

    def parse(self, html=None):
        return scraper.extract_table_data(scraper.find_target_tables(scraper.parse_page(html or self.html)))

    def run_scrape(self, render_failure=None):
        sources = dict(self.sources, **{scraper.chart_index_url: yaml.safe_dump(self.index)})
        with patch.object(scraper, "fetch_page", side_effect=sources.get), \
                patch.object(scraper, "read_yaml", return_value=self.existing), \
                patch.object(scraper, "get_chart_images", side_effect=lambda url, name, version:
                             None if version == render_failure else [f"example.org/chart:{version}"]) as render, \
                patch.object(scraper, "print_error"), patch.object(scraper, "write_yaml") as write:
            scraper.scrape()
        return write, render

    def save(self, write):
        self.existing = deepcopy(write.call_args.args[1])

    def restrict_charts(self, versions):
        self.index["entries"]["contour"] = [e for e in self.index["entries"]["contour"] if e["appVersion"] in versions]

    def test_current_matrix_uses_explicit_supported_versions_only(self):
        rows = self.parse()
        self.assertNotIn("main", [row["version"] for row in rows])
        self.assertEqual(rows[0]["version"], "1.33.7")
        self.assertEqual(rows[0]["kube"], ["1.34", "1.33", "1.32"])
        self.assertNotIn("1.37", rows[0]["kube"])

    def test_columns_are_selected_by_label(self):
        html = '<h2>Compatibility Matrix</h2><table><tr><th>Kubernetes Versions</th><th>Contour Version</th></tr>'
        html += '<tr><td>1.32,1.34, 1.33,1.34</td><td>1.33.0</td></tr></table>'
        self.assertEqual(self.parse(html)[0]["kube"], ["1.34", "1.33", "1.32"])

    def test_prereleases_and_partial_versions_are_excluded(self):
        for version in (b"1.33.7-rc1", b"1.33.7+build", b"next 1.33.7"):
            self.assertNotIn("1.33.7", [row["version"] for row in self.parse(
                self.html.replace(b">1.33.7<", b">" + version + b"<"))])

    def test_malformed_values_and_missing_headers_are_rejected(self):
        for value in (b"1.32+", b"1.32,unknown", b"1.32 - 1.34", b""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.parse(self.html.replace(b"1.34, 1.33, 1.32", value))
        with self.assertRaises(ValueError):
            self.parse(self.html.replace(b"Kubernetes Versions", b"Dependency Version"))

    def test_official_index_maps_exact_apps_and_latest_stable_chart(self):
        charts = scraper.chart_versions(yaml.safe_dump(self.index))
        self.assertEqual({v: charts[v] for v in ("1.32.0", "1.33.0", "1.33.7")},
                         {"1.32.0": "0.1.0", "1.33.0": "0.2.0", "1.33.7": "0.8.0"})
        self.assertNotIn("1.32.1", charts)
        self.index["entries"]["contour"] += [
            {"name": "contour", "appVersion": "v1.33.7", "version": "0.8.1"},
            {"name": "contour", "appVersion": "1.33.7", "version": "0.9.0-rc1"},
            {"name": "contour", "appVersion": "1.34.0-rc1", "version": "0.9.0"},
        ]
        charts = scraper.chart_versions(yaml.safe_dump(self.index))
        self.assertEqual(charts["1.33.7"], "0.8.1")
        self.assertNotIn("1.34.0-rc1", charts)

    def test_deprecated_charts_are_excluded_before_version_selection(self):
        self.index["entries"]["contour"].append({"name": "contour", "appVersion": "1.33.7",
                                                "version": "0.8.1", "deprecated": True})
        self.assertEqual(scraper.chart_versions(yaml.safe_dump(self.index))["1.33.7"], "0.8.0")

    def test_wrong_chart_name_and_conflicting_application_mappings_abort(self):
        for entry in ({"name": "other", "appVersion": "1.33.7", "version": "0.8.1"},
                      {"appVersion": "1.33.7", "version": "0.8.1"},
                      {"name": "contour", "appVersion": "1.34.0", "version": "0.8.0"}):
            with self.subTest(entry=entry):
                self.index["entries"]["contour"].append(entry)
                self.run_scrape()[0].assert_not_called()
                self.index["entries"]["contour"].pop()

    def test_empty_or_malformed_index_never_strips_old_charts(self):
        for index in (None, {}, {"entries": []}, {"entries": {"contour": []}},
                      {"entries": {"contour": [None]}}, {"entries": {"contour": [{"version": "0.8.0"}]}}):
            with self.subTest(index=index):
                self.index = index
                self.run_scrape()[0].assert_not_called()
        for content in (None, b"", b"invalid: [yaml"):
            with self.subTest(content=content), self.assertRaises((ValueError, yaml.YAMLError)):
                scraper.chart_versions(content)

    def test_repository_migration_resets_chart_numbering_for_consumer(self):
        original = deepcopy(self.existing)
        write, render = self.run_scrape()
        data = write.call_args.args[1]
        self.assertEqual(data["helm_repository_url"], scraper.helm_repository_url)
        mapped = {r["version"]: r["chart_version"] for r in data["versions"] if r.get("chart_version")}
        self.assertEqual(mapped, {"1.33.7": "0.8.0", "1.33.0": "0.2.0", "1.32.0": "0.1.0"})
        # observer/poller.ex selects the highest compatible chart semver.
        selected = max(Version(r["chart_version"]) for r in data["versions"]
                       if "1.33" in r["kube"] and r.get("chart_version"))
        self.assertEqual(selected, Version("0.8.0"))
        self.assertEqual(self.existing, original)
        self.assertEqual({call.args for call in render.call_args_list}, {
            (scraper.helm_repository_url, "contour", chart) for chart in ("0.1.0", "0.2.0", "0.8.0")})

    def test_unmatched_history_preserves_all_fields_except_obsolete_chart(self):
        original = deepcopy(self.existing["versions"][0])
        data = self.run_scrape()[0].call_args.args[1]
        row = next(r for r in data["versions"] if r["version"] == "1.32.1")
        original.pop("chart_version")
        self.assertEqual(row, original)
        matched = next(r for r in data["versions"] if r["version"] == "1.32.0")
        old = self.existing["versions"][1]
        self.assertEqual({k: v for k, v in matched.items() if k not in {"images", "chart_version"}},
                         {k: v for k, v in old.items() if k not in {"images", "chart_version"}})

    def test_failed_final_chart_render_prevents_all_writes(self):
        original = deepcopy(self.existing)
        write, render = self.run_scrape(render_failure="0.8.0")
        write.assert_not_called()
        self.assertEqual(render.call_count, 3)
        self.assertEqual(self.existing, original)

    def test_complete_migration_noops_without_rendering_old_charts(self):
        self.save(self.run_scrape()[0])
        self.sources = {scraper.compatibility_url: self.html}
        write, render = self.run_scrape()
        write.assert_not_called()
        render.assert_not_called()

    def test_delayed_exact_chart_backfill_needs_no_manifest_reads(self):
        full_index = deepcopy(self.index)
        self.restrict_charts({"1.32.0"})
        self.save(self.run_scrape()[0])
        before = deepcopy(self.existing)
        self.index = full_index
        self.sources = {scraper.compatibility_url: self.html}
        write, render = self.run_scrape()
        after = {r["version"]: r for r in write.call_args.args[1]["versions"]}
        for row in before["versions"]:
            self.assertEqual({k:v for k,v in after[row["version"]].items() if k not in {"chart_version", "images"}},
                             {k:v for k,v in row.items() if k not in {"chart_version", "images"}})
        self.assertEqual(render.call_count, 2)
        self.save(write)
        self.run_scrape()[0].assert_not_called()

    def test_legacy_backfill_uses_stored_data_outside_current_matrix(self):
        self.save(self.run_scrape()[0])
        row = {"version": "1.24.0", "kube": ["1.25"], "summary": {"features": ["Keep"]},
               "images": ["old:1.24.0"], "eolAt": "2025-01-01"}
        self.existing["versions"].append(row)
        self.index["entries"]["contour"].append({"name": "contour", "appVersion": "1.24.0", "version": "0.0.1"})
        self.sources = {scraper.compatibility_url: self.html}
        write, render = self.run_scrape()
        saved = next(r for r in write.call_args.args[1]["versions"] if r["version"] == "1.24.0")
        self.assertEqual(saved, dict(row, chart_version="0.0.1", images=["example.org/chart:0.0.1"]))
        render.assert_called_once_with(scraper.helm_repository_url, "contour", "0.0.1")

    def test_prerelease_chart_does_not_remove_saved_stable_mapping(self):
        self.save(self.run_scrape()[0])
        self.index["entries"]["contour"][0]["version"] = "0.9.0-rc1"
        self.run_scrape()[0].assert_not_called()

    def test_updated_chart_for_same_application_is_rendered(self):
        self.save(self.run_scrape()[0])
        self.index["entries"]["contour"][0]["version"] = "0.8.1"
        write, render = self.run_scrape()
        self.assertEqual(write.call_args.args[1]["versions"][0]["chart_version"], "0.8.1")
        render.assert_called_once_with(scraper.helm_repository_url, "contour", "0.8.1")

    def test_index_rollback_cannot_downgrade_a_saved_official_chart(self):
        self.save(self.run_scrape()[0])
        original = deepcopy(self.existing)
        self.index["entries"]["contour"][0]["version"] = "0.7.1"
        write, render = self.run_scrape()
        write.assert_not_called()
        render.assert_not_called()
        self.assertEqual(self.existing, original)

    def test_missing_older_boundary_can_be_added_after_latest_patch(self):
        self.save(self.run_scrape()[0])
        self.existing["versions"] = [r for r in self.existing["versions"] if r["version"] != "1.33.0"]
        write, render = self.run_scrape()
        self.assertIn("1.33.0", [r["version"] for r in write.call_args.args[1]["versions"]])
        render.assert_called_once_with(scraper.helm_repository_url, "contour", "0.2.0")

    def test_chartless_new_versions_keep_verified_release_images(self):
        self.restrict_charts({"1.32.0"})
        rows = self.run_scrape()[0].call_args.args[1]["versions"][:2]
        self.assertEqual([r["version"] for r in rows], ["1.33.7", "1.33.0"])
        for row in rows:
            self.assertNotIn("chart_version", row)
            self.assertIn(f'ghcr.io/projectcontour/contour:v{row["version"]}', row["images"])

    def test_charted_intermediate_patch_survives_candidate_reduction(self):
        self.restrict_charts({"1.32.0", "1.33.0", "1.33.6"})
        rows = self.run_scrape()[0].call_args.args[1]["versions"]
        self.assertEqual([r["version"] for r in rows[:3]], ["1.33.7", "1.33.6", "1.33.0"])
        self.assertEqual(rows[1]["chart_version"], "0.7.0")

    def test_manifest_images_ignore_crd_schema_keys(self):
        source = (FIXTURES / "release-1.33.0.yaml").read_bytes()
        source += b'\n---\nkind: CustomResourceDefinition\nspec:\n  properties:\n    image: {type: string}\n'
        self.assertEqual(scraper.release_images(source, "1.33.0"), [
            "docker.io/envoyproxy/envoy:distroless-v1.35.2", "ghcr.io/projectcontour/contour:v1.33.0"])

    def test_mismatched_release_manifest_is_rejected(self):
        with self.assertRaises(ValueError):
            scraper.release_images((FIXTURES / "release-1.33.0.yaml").read_bytes(), "1.33.7")

    def test_missing_or_malformed_manifest_aborts_entire_batch(self):
        self.restrict_charts({"1.32.0"})
        for source in (None, b'not: [valid'):
            self.sources[scraper.release_manifest_url.format(version="1.33.0")] = source
            self.run_scrape()[0].assert_not_called()

    def test_missing_matrix_or_duplicate_existing_data_never_writes(self):
        self.sources[scraper.compatibility_url] = b'<html>No matrix</html>'
        self.run_scrape()[0].assert_not_called()
        self.sources[scraper.compatibility_url] = self.html
        self.existing["versions"].append(deepcopy(self.existing["versions"][0]))
        self.run_scrape()[0].assert_not_called()
        self.existing = None
        self.run_scrape()[0].assert_not_called()

    def test_http_reads_have_timeout_and_raise_for_status(self):
        with patch.object(scraper.requests, "get") as get:
            scraper.fetch_page("https://example.com")
            get.assert_called_once_with("https://example.com", timeout=30)
            get.return_value.raise_for_status.assert_called_once_with()
        with patch.object(scraper, "fetch_page", side_effect=requests.HTTPError("404")), \
                patch.object(scraper, "write_yaml") as write, patch.object(scraper, "print_error"):
            scraper.scrape()
            write.assert_not_called()

    def test_shared_reducer_keeps_chartless_images_and_latest_chart(self):
        rows = [{"version": "1.32.0", "kube": ["1.33"], "chart_version": "0.1.0"},
                {"version": "1.32.1", "kube": ["1.33"], "chart_version": "0.1.1", "eolAt": "2027-01-01"},
                {"version": "1.33.0", "kube": ["1.33"], "images": ["verified:1.33.0"]}]
        reduced = reduce_versions(rows)
        self.assertEqual([r["version"] for r in reduced], ["1.33.0", "1.32.1", "1.32.0"])
        self.assertEqual(reduced[0]["images"], ["verified:1.33.0"])
        self.assertEqual(reduced[1]["eolAt"], "2027-01-01")


if __name__ == "__main__":
    unittest.main()
