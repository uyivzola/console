import re
import subprocess
from copy import deepcopy

import requests
import yaml
from bs4 import BeautifulSoup
from collections import OrderedDict
from utils import (
    print_error,
    get_chart_images,
    read_yaml,
    reduce_versions,
    sort_versions,
    validate_semver,
    write_yaml,
)

app_name = "contour"
compatibility_url = "https://projectcontour.io/resources/compatibility-matrix/"
target_file = f"../../static/compatibilities/{app_name}.yaml"
release_manifest_url = "https://raw.githubusercontent.com/projectcontour/contour/v{version}/examples/render/contour.yaml"
helm_repository_url = "https://projectcontour.github.io/helm-charts/"
chart_index_url = helm_repository_url + "index.yaml"
# Preserve the already-recorded history when removing the stale chart-only gate.
legacy_version_cutoff = validate_semver("1.32.1")


def fetch_page(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.content


def chart_versions(content):
    """Resolve exact stable app/chart tuples from the successor publisher."""
    if not isinstance(content, (str, bytes)) or not content:
        raise ValueError("Official Contour chart index is missing or empty")
    index = yaml.safe_load(content)
    catalog = index.get("entries") if isinstance(index, dict) else None
    entries = catalog.get(app_name) if isinstance(catalog, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("Official Contour chart index is missing or empty")
    charts = {}
    chart_apps = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Malformed Contour chart entry")
        if entry.get("deprecated") is True:
            continue
        if entry.get("name") != app_name:
            raise ValueError("Official index contains a different chart name")
        version, chart = entry.get("appVersion"), entry.get("version")
        if not isinstance(version, str) or not isinstance(chart, str):
            raise ValueError("Contour chart entry lacks version metadata")
        version = version.removeprefix("v")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"\d+\.\d+\.\d+", chart):
            continue
        if chart in chart_apps and chart_apps[chart] != version:
            raise ValueError("One Contour chart version maps to conflicting applications")
        chart_apps[chart] = version
        if version not in charts or validate_semver(chart) > validate_semver(charts[version]):
            charts[version] = chart
    if not charts:
        raise ValueError("Official Contour index has no stable app/chart mappings")
    return charts


def parse_page(content):
    soup = BeautifulSoup(content, "html.parser")
    sections = soup.find_all("h2")
    return sections


def find_target_tables(sections):
    target_tables = []
    for section in sections:
        if section.get_text(strip=True) in [
            "Compatibility Matrix",
        ]:
            table = section.find_next("table")
            if table:
                target_tables.append(table)
    return target_tables


def extract_table_data(target_tables):
    rows = []
    headers = [cell.get_text(" ", strip=True) for cell in target_tables[0].find_all("th")]
    try:
        version_index = headers.index("Contour Version")
        kube_index = headers.index("Kubernetes Versions")
    except ValueError:
        raise ValueError("Contour matrix is missing required column headers")
    for row in target_tables[0].find_all("tr")[1:]:  # Skip the header row
        columns = row.find_all("td")
        if not columns:
            continue
        if len(columns) <= max(version_index, kube_index):
            raise ValueError("Incomplete Contour compatibility row")
        version_text = columns[version_index].get_text(" ", strip=True)
        if not re.fullmatch(r"v?\d+\.\d+\.\d+", version_text):
            continue  # main and prereleases cannot establish a stable boundary.
        app_version = validate_semver(version_text.lstrip("v"))
        if not app_version:
            continue
        kube_versions = [value.strip() for value in columns[kube_index].get_text(" ", strip=True).split(",")]
        if not all(re.fullmatch(r"\d+\.\d+", value) for value in kube_versions):
            raise ValueError(f"Invalid supported Kubernetes list for Contour {app_version}")
        kube_versions = sorted(set(kube_versions), key=lambda value: tuple(map(int, value.split("."))), reverse=True)
        rows.append(OrderedDict([
            ("version", str(app_version)),
            ("kube", kube_versions),
            ("requirements", []),
            ("incompatibilities", []),
        ]))
    return rows


def release_images(content, version):
    """Verify the immutable release artifact and read only actual pod images."""
    images = set()
    for document in yaml.safe_load_all(content):
        if not isinstance(document, dict) or document.get("kind") not in {"Deployment", "DaemonSet", "Job"}:
            continue
        pod = document.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod.get("containers", []) + pod.get("initContainers", []):
            image = container.get("image")
            if isinstance(image, str) and image:
                images.add(image)
    if f"ghcr.io/projectcontour/contour:v{version}" not in images:
        raise ValueError(f"Release manifest does not contain Contour v{version}")
    return sorted(images)


def scrape():
    try:
        page_content = fetch_page(compatibility_url)
        if not page_content:
            raise ValueError("Could not fetch the Contour compatibility matrix")
        target_tables = find_target_tables(parse_page(page_content))
        if not target_tables:
            raise ValueError("No compatibility information found")
        rows = extract_table_data(target_tables)
        if not rows or len({row["version"] for row in rows}) != len(rows):
            raise ValueError("Empty or duplicate Contour compatibility versions")
        existing = read_yaml(target_file)
        if not existing or not existing.get("versions"):
            raise ValueError("Could not read existing Contour compatibility versions")
        recorded = {row["version"]: row for row in existing["versions"]}
        if len(recorded) != len(existing["versions"]):
            raise ValueError("Duplicate recorded Contour versions")
        # Never use an empty/failed catalog to strip historical chart metadata.
        charts = chart_versions(fetch_page(chart_index_url))
        migrating = existing.get("helm_repository_url", "").rstrip("/") != helm_repository_url.rstrip("/")
        rows = [row for row in rows if validate_semver(row["version"]) > legacy_version_cutoff and row["version"] not in recorded]
        combined = deepcopy(recorded)
        render = set()
        for version, row in combined.items():
            chart = charts.get(version)
            if chart and (migrating or not row.get("chart_version")
                          or validate_semver(chart) > validate_semver(row["chart_version"])):
                row["chart_version"] = chart
                render.add(version)
            elif migrating:
                # Bitnami's 21.x chart numbers are absent from the official fork
                # and must not outrank its 0.x charts in chart-update consumers.
                row.pop("chart_version", None)
        for row in rows:
            chart = charts.get(row["version"])
            if chart:
                row["chart_version"] = chart
        # Resolve charts before reduction so a chart-backed intermediate patch is
        # retained. Merge by version first: duplicate chartless/chart-backed rows
        # for one version cannot safely be sorted by the shared reducer.
        combined.update({row["version"]: row for row in rows})
        retained = {row["version"] for row in reduce_versions(list(combined.values()))}
        rows = [row for row in rows if row["version"] in retained]
        # Reduce only new candidates. A repository migration must preserve every
        # historical compatibility record, even a now-chartless redundant patch.
        combined = {version: row for version, row in combined.items()
                    if version in recorded or version in retained}
        for row in rows:
            version = row["version"]
            if row.get("chart_version"):
                render.add(version)
            else:
                content = fetch_page(release_manifest_url.format(version=version))
                if not content:
                    raise ValueError(f"Could not fetch the Contour v{version} release manifest")
                row["images"] = release_images(content, version)
        for version in sorted(render):
            row = combined[version]
            images = get_chart_images(helm_repository_url, app_name, row["chart_version"])
            if not images:
                raise ValueError(f"Could not render official Contour chart {row['chart_version']}")
            row["images"] = images
        data = deepcopy(existing)
        data["helm_repository_url"] = helm_repository_url
        data["versions"] = sort_versions(list(combined.values()))
    except (requests.RequestException, subprocess.SubprocessError, OSError, ValueError,
            TypeError, KeyError, yaml.YAMLError) as error:
        print_error(str(error))
        return
    # All sources and changed charts are verified before the one file write;
    # shared update_compatibility_info would re-render old charts from this URL.
    if data != existing:
        write_yaml(target_file, data)
