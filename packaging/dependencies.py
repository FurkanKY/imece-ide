"""Offline runtime dependency and license-metadata inventory."""
from __future__ import annotations

import argparse
import json
from importlib import metadata
from pathlib import Path, PurePosixPath
import re
import sys

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


class InventoryError(ValueError):
    """The requested inventory cannot be built from installed metadata."""


def _metadata_values(dist, key: str) -> list[str]:
    meta = dist.metadata
    values = meta.get_all(key, []) if hasattr(meta, "get_all") else []
    return [str(value) for value in (values or []) if value is not None]


def _license_expression(dist) -> str | None:
    # Legacy License values are prose in many distributions; accept only a
    # bounded expression-like value so arbitrary metadata is never copied out.
    values = _metadata_values(dist, "License-Expression") or _metadata_values(dist, "License")
    if not values:
        return None
    value = " ".join(values).strip()
    if value.casefold() in {"unknown", "unspecified", "none", "n/a"}:
        return None
    if len(value) > 256 or not re.fullmatch(r"[A-Za-z0-9.+() _:-]+", value):
        return None
    return value


def _license_files(dist) -> list[str]:
    safe = set()
    candidates = list(_metadata_values(dist, "License-File"))
    for file in getattr(dist, "files", None) or []:
        parts = PurePosixPath(str(file).replace("\\", "/")).parts
        if parts and parts[0].endswith(".dist-info"):
            leaf = parts[-1].casefold()
            if (any(part.casefold() in {"licenses", "license"} for part in parts)
                    or (len(parts) == 2 and ("license" in leaf or "notice" in leaf))):
                candidates.append(str(file))
    for value in candidates:
        candidate = PurePosixPath(value.replace("\\", "/"))
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            continue
        normalized = candidate.as_posix()
        if len(normalized) <= 256 and re.fullmatch(r"[A-Za-z0-9._/-]+", normalized):
            safe.add(normalized)
    return sorted(safe, key=str.casefold)


def _active(requirement: Requirement, extras: set[str]) -> bool:
    if requirement.marker is None:
        return True
    return any(requirement.marker.evaluate({"extra": extra}) for extra in ({""} | extras))


def build_inventory(requirements_path: str | Path, distributions=None, optional_seeds=("typesafe-sdk",)) -> dict:
    """Build inventory using installed distributions only; never imports packages."""
    path = Path(requirements_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InventoryError(f"Cannot read requirements file: {exc}") from exc

    installed = {}
    source = metadata.distributions() if distributions is None else distributions
    entries = source.values() if isinstance(source, dict) else source
    for dist in entries:
        name = dist.metadata.get("Name")
        if name:
            installed[canonicalize_name(name)] = dist

    parsed = []
    for number, line in enumerate(lines, 1):
        text = line.split("#", 1)[0].strip()
        if not text or text.startswith(("-", "\\")):
            if text:
                raise InventoryError(f"Unsupported requirements syntax on line {number}")
            continue
        try:
            parsed.append(Requirement(text))
        except Exception as exc:
            raise InventoryError(f"Invalid requirement on line {number}") from exc

    requested: dict[str, set[str]] = {}
    pending = []
    optional_seed_names = {canonicalize_name(name) for name in optional_seeds}
    for name in sorted(optional_seed_names):
        if name in installed:
            requested.setdefault(name, set())
            pending.append(name)
    for req in parsed:
        if _active(req, set()):
            name = canonicalize_name(req.name)
            requested.setdefault(name, set()).update(req.extras)
            pending.append(name)

    while pending:
        name = pending.pop()
        dist = installed.get(name)
        if dist is None:
            if name in optional_seed_names:
                continue
            raise InventoryError(f"Missing installed distribution: {name}")
        extras = requested[name]
        try:
            for requirement in parsed:
                if canonicalize_name(requirement.name) == name and _active(requirement, set()) and not requirement.specifier.contains(dist.version, prereleases=True):
                    raise InventoryError(f"Installed version does not satisfy requirement: {name}")
        except InventoryError:
            raise
        except Exception as exc:
            raise InventoryError(f"Invalid installed version for {name}") from exc
        for raw in (dist.requires or []):
            try:
                req = Requirement(raw)
            except Exception as exc:
                raise InventoryError(f"Invalid dependency metadata for {name}") from exc
            if not _active(req, extras):
                continue
            dep_name = canonicalize_name(req.name)
            dep_dist = installed.get(dep_name)
            if dep_dist is not None and req.specifier and not req.specifier.contains(dep_dist.version, prereleases=True):
                raise InventoryError(f"Installed version does not satisfy dependency requirement: {dep_name}")
            additions = set(req.extras) - requested.get(dep_name, set())
            if dep_name not in requested or additions:
                requested.setdefault(dep_name, set()).update(req.extras)
                pending.append(dep_name)

    packages = []
    review = []
    for name in sorted(requested):
        dist = installed[name]
        expression = _license_expression(dist)
        classifiers = sorted({value for value in _metadata_values(dist, "Classifier") if value.startswith("License ::")})
        license_files = _license_files(dist)
        packages.append({
            "name": canonicalize_name(dist.metadata.get("Name")),
            "version": str(dist.version),
            "licenseExpression": expression,
            "licenseClassifiers": classifiers,
            "licenseFiles": license_files,
        })
        if expression is None and not classifiers and not license_files:
            review.append(name)
    return {"packages": packages, "reviewRequired": review}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create an offline runtime dependency/license metadata inventory")
    parser.add_argument("--requirements", required=True, help="Runtime requirements file")
    parser.add_argument("--output", required=True, help="Output JSON path")
    args = parser.parse_args(argv)
    try:
        report = build_inventory(args.requirements)
        Path(args.output).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (InventoryError, OSError) as exc:
        print(f"dependency inventory: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
