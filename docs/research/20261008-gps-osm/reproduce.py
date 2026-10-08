#!/usr/bin/env python3
"""Reproduce the nine cached geometry cases without any network access.

Metrics require only Python's standard library. --maps additionally imports
matplotlib. Research target annotations are expected data, not runtime hints.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
import time


PHOTO_IDS = tuple(range(104, 113))
HELPERS = ("geometry_research.py", "summarize_geometry.py", "plot_geometry.py")
RESULTS = ("target_geometry_matrix.csv", "gps_sensitivity.csv", "target_geometry_summary.json")


def reject_network(event, args):
    """Apply to this interpreter and all in-process research helpers."""
    if event.startswith("socket.") or event in ("urllib.Request", "http.client.connect"):
        raise RuntimeError("Network access is disabled by the offline research wrapper")


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def relative_file(root, relative):
    """Provenance contains portable paths confined to this bundle."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Non-portable provenance path: {relative}")
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f"Provenance path leaves bundle: {relative}")
    return result


def verify_bundle(root):
    provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    if tuple(provenance["photo_ids"]) != PHOTO_IDS:
        raise ValueError("Provenance must describe exactly photo IDs 104 through 112")
    seen = set()
    for entry in provenance["files"]:
        if entry["path"] in seen:
            raise ValueError("Duplicate provenance file entry")
        seen.add(entry["path"])
        path = relative_file(root, entry["path"])
        if not path.is_file():
            raise FileNotFoundError(f"Missing bundle file: {entry['path']}")
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"Bundle SHA-256/size mismatch: {entry['path']}")
    required = {"geo_originals/gps-manifest.json"}
    required.update(f"research/{name}" for name in HELPERS)
    required.update(f"expected/{name}" for name in RESULTS)
    for source in provenance["osm_snapshots"]:
        required.update((source["gzip_path"], source["fetch_path"]))
    if not required.issubset(seen):
        raise ValueError("Provenance does not cover every required input")
    snapshots = {source["photo_id"]: source for source in provenance["osm_snapshots"]}
    if tuple(sorted(snapshots)) != PHOTO_IDS or len(provenance["osm_snapshots"]) != 9:
        raise ValueError("Exactly nine distinct OSM snapshots are required")
    return provenance, snapshots


def unpack_snapshot(root, target, source):
    """Verify compressed bytes first, then verify the original XML while unpacking."""
    fetch_path = relative_file(root, source["fetch_path"])
    fetch = json.loads(fetch_path.read_text(encoding="utf-8"))
    if fetch["sha256"] != source["raw_sha256"] or fetch["size_bytes"] != source["raw_bytes"]:
        raise ValueError(f"Fetch provenance disagrees for photo {source['photo_id']}")
    if fetch["url"] != source["source_url"]:
        raise ValueError(f"Source URL provenance disagrees for photo {source['photo_id']}")
    xml_path = target / f"photo-{source['photo_id']}.osm"
    raw_hash = hashlib.sha256()
    size = 0
    with gzip.open(relative_file(root, source["gzip_path"]), "rb") as compressed:
        with xml_path.open("wb") as output:
            for block in iter(lambda: compressed.read(1024 * 1024), b""):
                size += len(block)
                if size > source["raw_bytes"]:
                    raise ValueError("Uncompressed OSM exceeds the recorded byte count")
                raw_hash.update(block)
                output.write(block)
    if size != source["raw_bytes"] or raw_hash.hexdigest() != source["raw_sha256"]:
        raise ValueError(f"Uncompressed OSM SHA-256/size mismatch: photo {source['photo_id']}")
    shutil.copyfile(fetch_path, target / f"photo-{source['photo_id']}.fetch.json")
    return fetch


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load verified helper {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("reproduced"),
                        help="Directory for compact regenerated results; default: ./reproduced")
    parser.add_argument("--maps", action="store_true", help="Also render maps; requires matplotlib")
    parser.add_argument("--verify-only", action="store_true", help="Verify all bundle input hashes only")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    started = time.perf_counter()
    sys.addaudithook(reject_network)
    # The wrapper executes helpers in-process, so the audit hook is inherited.
    sys.dont_write_bytecode = True
    provenance, snapshots = verify_bundle(root)
    input_verified_at = time.perf_counter()
    if args.verify_only:
        print(json.dumps({"status": "verified", "photo_ids": PHOTO_IDS,
                          "files": len(provenance["files"]), "network_calls": 0}))
        return

    manifest = json.loads((root / "geo_originals/gps-manifest.json").read_text(encoding="utf-8"))
    items = {item["message_id"]: item for item in manifest["items"]}
    if tuple(sorted(items)) != PHOTO_IDS or len(manifest["items"]) != 9:
        raise ValueError("The normalized metadata manifest must contain exactly nine unique cases")
    for item in items.values():
        lat, lon = item["latitude"], item["longitude"]
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Invalid research GPS coordinate")

    output = args.output.resolve()
    if output == root or output.is_relative_to(root / "research") or output.is_relative_to(root / "expected"):
        raise ValueError("Use a separate output directory, not the bundled inputs or expected results")
    counts = []
    with tempfile.TemporaryDirectory(prefix="street-story-geometry-") as directory:
        work = Path(directory)
        research = work / "research"
        osm = research / "osm"
        osm.mkdir(parents=True)
        (work / "geo_originals").mkdir()
        shutil.copyfile(root / "geo_originals/gps-manifest.json", work / "geo_originals/gps-manifest.json")
        for name in HELPERS:
            shutil.copyfile(root / "research" / name, research / name)
        for num in PHOTO_IDS:
            unpack_snapshot(root, osm, snapshots[num])
        unpacked_at = time.perf_counter()

        geometry = load_helper("geometry_research", research / "geometry_research.py")
        for num in PHOTO_IDS:
            item = items[num]
            data = geometry.fetch_osm(osm / f"photo-{num}", item["latitude"], item["longitude"],
                                      snapshots[num]["radius_m"], local_only=True)
            if data["raw_sha256"] != snapshots[num]["raw_sha256"] or not data["raw_cache_reused"]:
                raise ValueError("Geometry did not consume the verified local snapshot")
            counts.append({"photo_id": num, "complete_building_groups": len(data["building_groups"]),
                           "unresolved_buildings": len(data["unresolved_buildings"])})
            del data
        geometry_done_at = time.perf_counter()
        summary = load_helper("summarize_geometry", research / "summarize_geometry.py")
        with contextlib.redirect_stdout(io.StringIO()):
            summary.main()
        compared = {}
        for name in RESULTS:
            compared[name] = digest(research / name) == digest(root / "expected" / name)
        if not all(compared.values()):
            failed = [name for name, same in compared.items() if not same]
            raise ValueError("Regenerated metrics differ from checked-in expected research data: " + ", ".join(failed))
        summary_done_at = time.perf_counter()
        output.mkdir(parents=True, exist_ok=True)
        for name in RESULTS:
            shutil.copyfile(research / name, output / name)

        maps_written = 0
        if args.maps:
            try:
                plot = load_helper("plot_geometry", research / "plot_geometry.py")
            except ModuleNotFoundError as exc:
                raise SystemExit("Map rendering additionally requires matplotlib; metrics themselves use only stdlib") from exc
            for num in PHOTO_IDS:
                data = json.loads((osm / f"photo-{num}.geometry.json").read_text(encoding="utf-8"))
                plot.render(data, output / "maps" / f"photo-{num}.map.png", summary.TARGETS.get(num),
                            snapshots[num]["radius_m"], f"Фото {num} · Геометрия окружения")
                maps_written += 1
        finished_at = time.perf_counter()
        receipt = {
            "status": "offline_reproduction_pass",
            "meaning": "Cached arithmetic and expected research outputs match; not a product identity/vision/facts PASS",
            "python_version": ".".join(str(x) for x in sys.version_info[:3]),
            "photo_ids": list(PHOTO_IDS),
            "target_status": {str(num): ("research_expected_target" if num in summary.TARGETS else "pending") for num in PHOTO_IDS},
            "hash_verification": "All listed files verified before execution; gzip and decoded OSM verified before parsing",
            "expected_comparison": compared,
            "network_policy": "Python audit hook rejects socket and urllib network operations in the same interpreter",
            "network_calls": 0,
            "provider_inference_calls": 0,
            "maps_written": maps_written,
            "timings_seconds": {
                "verify_inputs": round(input_verified_at - started, 6),
                "unpack_and_verify_osm": round(unpacked_at - input_verified_at, 6),
                "geometry_nine_snapshots": round(geometry_done_at - unpacked_at, 6),
                "summary_and_compare": round(summary_done_at - geometry_done_at, 6),
                "maps_and_copy": round(finished_at - summary_done_at, 6),
                "total": round(finished_at - started, 6),
            },
            "candidate_counts": counts,
            "calculation_inputs": {
                "normalized_gps_sha256": digest(root / "geo_originals/gps-manifest.json"),
                "wrapper_sha256": digest(root / "reproduce.py"),
                "geometry_helper_sha256": digest(root / "research/geometry_research.py"),
                "summary_helper_sha256": digest(root / "research/summarize_geometry.py"),
                "raw_osm_sha256": {str(num): snapshots[num]["raw_sha256"] for num in PHOTO_IDS},
                "expected_output_sha256": {name: digest(root / "expected" / name) for name in RESULTS},
            },
            "temporary_files_retained": False,
        }
        (output / "verification.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "cases": 9, "maps": maps_written,
                      "metrics_equal_expected": True, "network_calls": 0,
                      "elapsed_seconds": receipt["timings_seconds"]["total"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
