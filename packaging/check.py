"""Dependency-free source and Windows/Linux onedir packaging checks."""
from __future__ import annotations

import argparse
import ast
import hashlib
import html.parser
import json
import os
import re
from pathlib import Path
import stat
import sys
from urllib.parse import unquote, urlsplit


class PackagingError(ValueError):
    pass


def _version(root: Path) -> str:
    app_path = root / "webhost/api/app.py"
    try:
        tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    except (OSError, SyntaxError) as exc:
        raise PackagingError(f"Cannot read APP_VERSION in {app_path}: {exc}") from exc
    version = None
    for item in tree.body:
        if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "APP_VERSION" for t in item.targets):
            try:
                version = ast.literal_eval(item.value)
            except (ValueError, TypeError):
                pass
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "APP_VERSION":
            try:
                version = ast.literal_eval(item.value)
            except (ValueError, TypeError):
                pass
    if not isinstance(version, str) or not version:
        raise PackagingError("APP_VERSION must be a string literal in webhost/api/app.py")
    try:
        package_version = json.loads((root / "web/ui/package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PackagingError(f"Cannot read web/ui/package.json version: {exc}") from exc
    if package_version != version:
        raise PackagingError(f"Version mismatch: APP_VERSION={version!r}, package.json={package_version!r}")
    return version


class _References(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.values = []

    def handle_starttag(self, tag, attrs):
        self.values.extend(value for key, value in attrs if key.lower() in ("src", "href") and value)


def _check_references(index: Path, dist: Path) -> None:
    parser = _References()
    try:
        parser.feed(index.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise PackagingError(f"Cannot parse UI index {index}: {exc}") from exc
    base = dist.resolve()
    for value in parser.values:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() in ("http", "https", "data") or parsed.netloc or not parsed.path:
            continue
        path_text = unquote(parsed.path).replace("\\", "/")
        rooted = path_text.startswith("/")
        path = Path(path_text.lstrip("/")) if rooted else Path(path_text)
        candidate = ((base / path) if rooted else (index.parent / path)).resolve()
        try:
            candidate.relative_to(base)
        except ValueError as exc:
            raise PackagingError(f"UI asset reference escapes dist: {value}") from exc
        if not candidate.is_file():
            raise PackagingError(f"UI asset referenced by index is missing: {value}")


def check_sources(root: Path) -> str:
    root = root.resolve()
    dist = root / "web/ui/dist"
    index = dist / "index.html"
    if not index.is_file():
        raise PackagingError(f"Built UI index is missing: {index}")
    _check_references(index, dist)
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        if not (root / name).is_file():
            raise PackagingError(f"Required packaging file is missing: {name}")
    return _version(root)


_FORBIDDEN_DIRS = {".git", ".pi", ".venv"}
_FORBIDDEN_FILES = {"prefs.json", "providers.json", "runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm", "keys.dpapi", "secrets.dat", "secrets.tmp"}


def _bundle_files(bundle: Path, platform: str = "win32"):
    # Walk without following directory links, checking every entry including directories.
    def unreadable(error):
        raise PackagingError("Cannot enumerate all bundle entries") from error
    for current, dirs, files in os.walk(bundle, topdown=True, followlinks=False, onerror=unreadable):
        parent = Path(current)
        kept = []
        for name in dirs:
            path = parent / name
            _reject_special(path)
            if name.casefold() in _FORBIDDEN_DIRS:
                raise PackagingError(f"Forbidden directory bundled: {path.relative_to(bundle)}")
            kept.append(name)
        dirs[:] = kept
        for name in files:
            path = parent / name
            folded = name.casefold()
            if folded in _FORBIDDEN_FILES or folded == ".env" or folded.startswith(".env."):
                raise PackagingError(f"User data/secret bundled: {path.relative_to(bundle)}")
            if path.is_symlink() and platform == "linux":
                target_text = os.readlink(path)
                if Path(target_text).is_absolute():
                    raise PackagingError(f"Unsafe bundle symlink: {path.relative_to(bundle)}")
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(bundle)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise PackagingError(f"Unsafe bundle symlink: {path.relative_to(bundle)}") from exc
                if not resolved.is_file():
                    raise PackagingError(f"Unsafe bundle symlink: {path.relative_to(bundle)}")
                yield path
                continue
            _reject_special(path)
            if not path.is_file():
                raise PackagingError(f"Unexpected non-file in bundle: {path.relative_to(bundle)}")
            yield path


def _reject_special(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
        attrs = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError as exc:
        raise PackagingError(f"Cannot inspect bundle entry {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise PackagingError(f"Symlink/reparse point forbidden in bundle: {path}")


def check_bundle(root: Path, bundle: Path, write_manifest: bool = False, platform: str = "win32") -> Path | None:
    if platform not in ("win32", "linux"):
        raise PackagingError(f"Unsupported bundle platform: {platform}")
    _reject_special(bundle)
    root, bundle = root.resolve(), bundle.resolve()
    version = check_sources(root)
    if not bundle.is_dir():
        raise PackagingError(f"Bundle directory is missing: {bundle}")
    files = list(_bundle_files(bundle, platform))
    for path in files:
        rel = path.relative_to(bundle).as_posix().casefold()
        if rel.startswith(('_internal/pyside6/qt/qml/', '_internal/pyside6/qml/')) or re.match(
                r'(?:lib)?qt6(?:charts|graphs|datavisualization|pdf|virtualkeyboard|quick3d)', path.name, re.I):
            raise PackagingError('Unreviewed unused Qt QML/add-on payload bundled')
    executable = "ImeceIDE.exe" if platform == "win32" else "ImeceIDE"
    node = "_internal/nodejs_wheel/node.exe" if platform == "win32" else "_internal/nodejs_wheel/bin/node"
    required = (executable, "_internal/web/ui/dist/index.html", node, "_internal/LICENSE", "_internal/THIRD-PARTY-NOTICES.md")
    for name in required:
        if not (bundle / name).is_file():
            raise PackagingError(f"Required bundle file is missing: {name}")
    if platform == "linux":
        for name in (executable, node):
            if not os.access(bundle / name, os.X_OK):
                raise PackagingError(f"Required bundle executable is not executable: {name}")
    internal = bundle / "_internal"
    if platform == "win32":
        for conpty in ("OpenConsole.exe", "winpty-agent.exe"):
            if not any(p.name.lower() == conpty.lower() and p.is_file() for p in internal.rglob("*")):
                raise PackagingError(f"Required ConPTY payload is missing under _internal: {conpty}")
    _check_references(bundle / "_internal/web/ui/dist/index.html", bundle / "_internal/web/ui/dist")
    if not write_manifest:
        return None
    hashes = {}
    links = {}
    for path in files:
        rel = path.relative_to(bundle).as_posix()
        if rel == "package-manifest.json":
            continue
        if path.is_symlink():
            links[rel] = os.readlink(path).replace(os.sep, "/")
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        hashes[rel] = digest.hexdigest()
    manifest_path = bundle / "package-manifest.json"
    if manifest_path.is_symlink():
        raise PackagingError("Manifest output cannot be a symlink")
    manifest = {"version": version, "platform": platform, "files": dict(sorted(hashes.items()))}
    if platform == "linux" and links:
        manifest["links"] = dict(sorted(links.items()))
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version")
    version.add_argument("--root", required=True, type=Path)
    sources = commands.add_parser("sources")
    sources.add_argument("--root", required=True, type=Path)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("--root", required=True, type=Path)
    bundle.add_argument("--bundle", required=True, type=Path)
    bundle.add_argument("--write-manifest", action="store_true")
    bundle.add_argument("--platform", choices=("win32", "linux"), default="win32")
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(_version(args.root))
        elif args.command == "sources":
            check_sources(args.root)
        else:
            check_bundle(args.root, args.bundle, args.write_manifest, platform=args.platform)
    except PackagingError as exc:
        print(f"packaging check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
