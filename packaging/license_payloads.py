"""Offline staging of license texts for packaged Python and frontend runtimes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import sys


class PayloadError(ValueError):
    pass


def _safe_rel(value: str) -> PurePosixPath | None:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or not re.fullmatch(r"[A-Za-z0-9._@+/-]+", path.as_posix()):
        return None
    return path


def _is_license_name(name: str) -> bool:
    return bool(re.search(r"(?:^|[._-])(license|licence|notice)(?:$|[._-])|^(license|licence|notice)(?:$|[._-])", name, re.I))


def _copy_regular(source: Path, destination: Path, max_bytes=4 * 1024 * 1024) -> bool:
    try:
        if source.is_symlink() or not source.is_file():
            return False
        # Reject symlinks in any source path component, including dist-info records.
        current = source
        while current != current.parent:
            if current.is_symlink():
                return False
            current = current.parent
        size = source.stat().st_size
        if size <= 0 or size > max_bytes:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return True
    except OSError:
        return False


def _python_payload(requirements: Path, destination: Path) -> dict:
    deps = runpy.run_path(str(Path(__file__).with_name("dependencies.py")))
    inventory = deps["build_inventory"](requirements)
    from importlib import metadata
    from packaging.utils import canonicalize_name
    distributions = {canonicalize_name(str(d.metadata.get("Name", ""))): d for d in metadata.distributions()}
    staged, missing = [], []
    for package in inventory["packages"]:
        key = package["name"]
        dist = distributions.get(key)
        copied = []
        if dist:
            for raw in package["licenseFiles"]:
                rel = _safe_rel(raw)
                if rel is None:
                    continue
                try:
                    source = Path(dist.locate_file(str(rel)))
                except Exception:
                    continue
                target_rel = PurePosixPath(key) / rel
                if _copy_regular(source, destination.joinpath(*target_rel.parts)):
                    copied.append(target_rel.as_posix())
        package["stagedLicenseFiles"] = sorted(copied)
        if not copied:
            missing.append(key)
        staged.extend(copied)
    inventory["reviewRequired"] = sorted(set(inventory.get("reviewRequired", [])) | set(missing))
    return {"packages": inventory["packages"], "reviewRequired": inventory["reviewRequired"], "stagedFiles": sorted(staged)}


def _resolve_lock_path(parent: str, name: str, entries: dict) -> str | None:
    if _safe_rel(name) is None:
        raise PayloadError("Unsafe frontend dependency name")
    current = parent
    while True:
        candidate = (current + "/node_modules/" if current else "node_modules/") + name
        if candidate in entries:
            return candidate
        if not current:
            return None
        current = current.rsplit("/node_modules/", 1)[0] if "/node_modules/" in current else ""


def _frontend_payload(ui: Path, destination: Path) -> dict:
    lock_path = ui / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    entries = lock.get("packages", {})
    root = entries.get("")
    if not isinstance(root, dict):
        raise PayloadError("Frontend lockfile has no root package")
    requested: set[str] = set()
    pending = [("", name) for field in ("dependencies", "optionalDependencies") for name in (root.get(field) or {})]
    # Tailwind emits CSS redistributed in the runtime build despite being a
    # build-time dependency; include its text, not unrelated dev tools.
    if "tailwindcss" in (root.get("devDependencies") or {}):
        pending.append(("", "tailwindcss"))
    while pending:
        parent, name = pending.pop()
        key = _resolve_lock_path(parent, name, entries)
        if key in requested:
            continue
        entry = entries.get(key)
        if not isinstance(entry, dict):
            raise PayloadError("Declared frontend runtime package is absent from lockfile")
        requested.add(key)
        for field in ("dependencies", "optionalDependencies"):
            for child in (entry.get(field) or {}):
                pending.append((key, child))
        for peer, peer_range in (entry.get("peerDependencies") or {}).items():
            if (entry.get("peerDependenciesMeta") or {}).get(peer, {}).get("optional"):
                continue
            peer_key = _resolve_lock_path(key, peer, entries)
            if peer_key is None:
                raise PayloadError("Required frontend peer dependency is absent from lockfile")
            pending.append((key, peer))
    packages, review, all_staged = [], [], []
    for key in sorted(requested):
        entry = entries[key]
        rel = _safe_rel(key)
        if rel is None:
            raise PayloadError("Unsafe frontend package path in lockfile")
        package_dir = ui / rel
        if not package_dir.is_dir() or package_dir.is_symlink():
            raise PayloadError("Declared frontend runtime package is not installed")
        actual = json.loads((package_dir / "package.json").read_text(encoding="utf-8"))
        expected_name = entry.get("name", key.rsplit("node_modules/", 1)[-1])
        if actual.get("name") != expected_name or actual.get("version") != entry.get("version"):
            raise PayloadError("Installed frontend runtime metadata differs from lockfile")
        licenses = []
        for root_dir, dirs, files in os.walk(package_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root_dir) / d).is_symlink() and d != "node_modules"]
            for filename in files:
                if not _is_license_name(filename):
                    continue
                source = Path(root_dir) / filename
                relative = source.relative_to(package_dir)
                safe = _safe_rel(relative.as_posix())
                if safe is None:
                    continue
                out_rel = rel / safe
                if _copy_regular(source, destination.joinpath(*out_rel.parts)):
                    licenses.append(out_rel.as_posix())
        package_name = entry.get("name", key.rsplit("node_modules/", 1)[-1])
        if not isinstance(package_name, str) or not re.fullmatch(r"@?[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?", package_name):
            package_name = key.rsplit("node_modules/", 1)[-1]
        if not licenses:
            review.append(package_name)
        package_version = entry.get("version")
        if not isinstance(package_version, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", package_version):
            package_version = None
        license_value = entry.get("license")
        if not isinstance(license_value, str) or len(license_value) > 128 or not re.fullmatch(r"[A-Za-z0-9.+() _:-]+", license_value):
            license_value = None
        if not licenses and package_name == "react-remove-scroll-bar" and package_version == "2.3.8":
            vendor = ui.parent.parent / "packaging/licenses/react-remove-scroll-bar-2.3.8-LICENSE.txt"
            vendor_rel = rel / "LICENSE"
            if _copy_regular(vendor, destination.joinpath(*vendor_rel.parts)):
                licenses.append(vendor_rel.as_posix())
                review.remove(package_name)
        package = {"name": package_name, "version": package_version, "license": license_value, "licenseFiles": sorted(licenses)}
        packages.append(package)
        all_staged.extend(licenses)
    return {"packages": packages, "reviewRequired": sorted(review), "stagedFiles": sorted(all_staged)}


def collect(root: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "python": _python_payload(root / "requirements.txt", output / "python"),
        "frontend": _frontend_payload(root / "web/ui", output / "frontend"),
        "fonts": {"stagedFiles": [], "reviewRequired": []},
        "reviewRequired": ["Qt/Chromium and other native-library source/third-party obligations require release review; license texts alone are not compliance certification"],
    }
    qt_texts = []
    for name in ("LGPL-3.0.txt", "GPL-3.0.txt"):
        if not _copy_regular(root / "packaging/licenses" / name, output / "qt" / name):
            raise PayloadError("Required Qt LGPL/GPL license text is missing")
        qt_texts.append("qt/" + name)
    for package in report["python"]["packages"]:
        if package["name"] in {"pyside6", "pyside6-addons", "pyside6-essentials", "shiboken6"}:
            package["stagedLicenseFiles"] = qt_texts
            package["guiLicenseOption"] = "LGPL-3.0-only; not a blanket classification of every collected Qt add-on"
            if package["name"] in report["python"]["reviewRequired"]:
                report["python"]["reviewRequired"].remove(package["name"])
    report["qt"] = {"stagedFiles": qt_texts,
                    "runtimeScope": "Widget/WebEngine shell; native QtQuick/Qml bindings kept, unused declarative app plugins excluded",
                    "excludedUnusedAddons": ["Charts", "Graphs", "DataVisualization", "PDF image plugin", "VirtualKeyboard", "Quick3D profiler", "all application qmldir plugins"],
                    "reviewRequired": "Qt/Chromium third-party/source redistribution obligations and exact Windows DLL inventory remain publication review"}
    fonts = root / "web/ui/public/fonts"
    for file in sorted(fonts.iterdir()) if fonts.is_dir() else []:
        if file.is_file() and not file.is_symlink() and (_is_license_name(file.name) or file.suffix.casefold() in {".txt", ".md"}):
            target = output / "frontend" / "fonts" / file.name
            if _copy_regular(file, target):
                report["fonts"]["stagedFiles"].append("fonts/" + file.name)
    # The executable embeds PyInstaller's bootloader, whose GPL exception and
    # license must accompany redistribution even though PyInstaller is a tool.
    from importlib import metadata
    bootloader = metadata.distribution("PyInstaller")
    boot_files = []
    for record in bootloader.files or []:
        if Path(str(record)).name.casefold() == "copying.txt":
            source = Path(bootloader.locate_file(record))
            if _copy_regular(source, output / "bootloader/PyInstaller-COPYING.txt"):
                boot_files.append("bootloader/PyInstaller-COPYING.txt")
    if not boot_files:
        raise PayloadError("PyInstaller bootloader license/exception text is missing")
    report["bootloader"] = {"stagedFiles": boot_files}
    report_path = output / "inventory.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        collect(Path(args.root), Path(args.output))
    except (OSError, PayloadError, ValueError) as exc:
        print(f"license payloads: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
