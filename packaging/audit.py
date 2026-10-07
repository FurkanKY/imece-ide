"""Bounded audit of an already-built ImeceIDE onedir artifact.

This checks package-manifest integrity and scans artifact bytes for a small set
of high-confidence credential indicators. It is not a general secret detector or
compliance certification. PyInstaller bytecode inspection is best-effort and
requires the installed PyInstaller archive readers.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import json
import os
import marshal
from pathlib import Path, PurePosixPath
import re
import sys

# check.py intentionally remains a script, not a packaging namespace package.
_spec = importlib.util.spec_from_file_location("imece_packaging_check", Path(__file__).with_name("check.py"))
_check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_check)

CHUNK = 64 * 1024
OVERLAP = 8192
PATTERNS = (
    ("aws_access_key_id", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("openai_api_key", re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}\b")),
    ("pem_private_key", re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----\r?\n(?:[A-Za-z0-9+/=]{16,}\r?\n){2}")),
)


def _safe_rel(text: str) -> bool:
    if not text or "\\" in text or "\x00" in text:
        return False
    path = PurePosixPath(text)
    return ":" not in text and not path.is_absolute() and all(part not in ("", ".", "..") for part in text.split("/")) and path.as_posix() == text


def _actual(bundle: Path, platform: str):
    files, links = {}, {}
    for path in _check._bundle_files(bundle, platform):
        rel = path.relative_to(bundle).as_posix()
        if rel == "package-manifest.json":
            continue
        if path.is_symlink():
            links[rel] = os.readlink(path).replace(os.sep, "/")
        else:
            files[rel] = path
    return files, links


def _manifest(bundle: Path, files: dict, links: dict, platform: str):
    path = bundle / "package-manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("manifest missing or unsafe")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("platform") != platform:
        raise ValueError("manifest platform invalid")
    listed = data.get("files")
    listed_links = data.get("links", {})
    if not isinstance(listed, dict) or not isinstance(listed_links, dict):
        raise ValueError("manifest mappings invalid")
    # Reject unsafe keys before any manifest-derived path is constructed/accessed.
    if any(not isinstance(k, str) or not _safe_rel(k) for k in (*listed.keys(), *listed_links.keys())):
        raise ValueError("manifest path invalid")
    if set(listed) != set(files) or set(listed_links) != set(links):
        raise ValueError("manifest entries do not match artifact")
    for name, expected in listed.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("manifest hash invalid")
        digest = hashlib.sha256()
        with files[name].open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("manifest hash mismatch")
    if listed_links != links:
        raise ValueError("manifest symlink mapping mismatch")


def _find_bytes(path: Path, rel: str):
    findings = []
    tail = b""
    base = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            data = tail + chunk
            start = base - len(tail)
            for rule, regex in PATTERNS:
                for match in regex.finditer(data):
                    offset = start + match.start()
                    if offset >= base - len(tail) and offset >= 0:
                        findings.append({"rule": rule, "file": rel, "offset": offset, "count": 1})
            base += len(chunk)
            tail = data[-OVERLAP:]
    # Deduplicate matches repeated in adjacent overlapping windows.
    unique = {(f["rule"], f["file"], f["offset"]): f for f in findings}
    return list(unique.values())


def _direct_url(path: Path, rel: str):
    if not rel.endswith("direct_url.json"):
        return []
    # This file has structured credential-bearing URL fields; report only byte offsets.
    try:
        raw = path.read_bytes()
        obj = json.loads(raw)
    except (OSError, ValueError, UnicodeError):
        return []
    findings = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and "://" in item:
                    from urllib.parse import urlsplit, parse_qsl
                    try:
                        parsed = urlsplit(item)
                        credentialed = parsed.username is not None or parsed.password is not None or any(
                            name.lower() in {'token','access_token','api_key','apikey','password','secret','auth','key'} and value
                            for name,value in parse_qsl(parsed.query))
                    except ValueError:
                        credentialed = False
                    if credentialed:
                        at = raw.find(item.encode("utf-8"))
                        findings.append({"rule": "credential_url", "file": rel, "offset": max(0, at), "count": 1})
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(obj)
    return findings


def _scan_code(bundle: Path, files: dict):
    """Read PyInstaller bytecode archive constants without importing/execing app code."""
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    except ImportError:
        return [], "unavailable"
    results = []
    failed = False
    for rel, path in sorted(files.items()):
        if rel in ("ImeceIDE", "ImeceIDE.exe") or rel.endswith(".pyz"):
            try:
                if rel.endswith(".pyz"):
                    archive = ZlibArchiveReader(str(path))
                    names = archive.toc.keys()
                    for name in names:
                        try:
                            code = archive.extract(name)
                        except Exception:
                            failed = True
                            continue
                        if hasattr(code, "co_consts"):
                            results.extend(_scan_constants(code, rel))
                elif rel in ("ImeceIDE", "ImeceIDE.exe"):
                    import tempfile
                    archive = CArchiveReader(str(path))
                    for name, entry in archive.toc.items():
                        if entry[-1] in ("s", "m", "M"):
                            try:
                                code = marshal.loads(archive.extract(name))
                                results.extend(_scan_constants(code, rel))
                            except Exception:
                                failed = True
                        if name.lower().endswith(".pyz"):
                            try:
                                data = archive.extract(name)
                                with tempfile.TemporaryDirectory(prefix="imece-audit-") as temp:
                                    embedded = Path(temp) / "embedded.pyz"
                                    embedded.write_bytes(data)
                                    pyz = ZlibArchiveReader(str(embedded))
                                    for module in pyz.toc:
                                        try:
                                            code = pyz.extract(module)
                                        except Exception:
                                            failed = True
                                            continue
                                        if hasattr(code, "co_consts"):
                                            results.extend(_scan_constants(code, rel))
                            except Exception:
                                failed = True
                                continue
            except Exception:
                failed = True
                continue
    return results, "partial" if failed else "available"


def _scan_constants(code, rel, depth=0):
    if depth > 30:
        return []
    out = []
    for value in getattr(code, "co_consts", ()):
        if isinstance(value, str):
            for rule, regex in PATTERNS:
                for match in regex.finditer(value.encode("utf-8", "ignore")):
                    out.append({"rule": rule, "file": rel, "offset": 0, "count": 1})
        elif isinstance(value, bytes):
            for rule, regex in PATTERNS:
                if regex.search(value):
                    out.append({"rule": rule, "file": rel, "offset": 0, "count": 1})
        elif hasattr(value, "co_consts"):
            out.extend(_scan_constants(value, rel, depth + 1))
        elif isinstance(value, (tuple, frozenset)):
            class Constants:
                co_consts = value
            out.extend(_scan_constants(Constants(), rel, depth + 1))
    return out



def _record_hash_indicator(finding: dict, path: Path, bundle: Path) -> bool:
    if finding["rule"] != "openai_api_key" or path.name != "RECORD" or path.stat().st_size > 8 * 1024 * 1024:
        return False
    cursor = 0
    for line in path.read_bytes().splitlines(keepends=True):
        offset = finding["offset"]
        if cursor <= offset < cursor + len(line):
            try:
                row = next(csv.reader([line.decode("utf-8")]))
                if len(row) != 3 or not _safe_rel(row[0]) or not re.fullmatch(r"sha256=[A-Za-z0-9_-]{43}", row[1]):
                    return False
                begin = line.index(row[1].encode())
                if not cursor + begin <= offset < cursor + begin + len(row[1]):
                    return False
                target = (path.parent.parent / row[0]).resolve(strict=True)
                target.relative_to(bundle)
                expected = base64.urlsafe_b64decode(row[1][7:] + "=").hex()
                return target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected
            except (ValueError, OSError, UnicodeError, csv.Error):
                return False
        cursor += len(line)
    return False


def _review(root: Path, bundle: Path, files: dict, findings: list):
    rules_path = root / "packaging/audit-exceptions.json"
    exceptions = json.loads(rules_path.read_text()).get("exceptions", []) if rules_path.is_file() else []
    blocked, reviewed, digests = [], [], {}
    for finding in findings:
        path = files.get(finding["file"])
        reason = None
        if path and _record_hash_indicator(finding, path, bundle):
            reason = "Verified RECORD file SHA-256 field, not a credential"
        if path and reason is None:
            for entry in exceptions:
                if entry.get("file") == finding["file"] and entry.get("rule") == finding["rule"]:
                    if finding["file"] not in digests:
                        digest = hashlib.sha256()
                        with path.open("rb") as stream:
                            for chunk in iter(lambda: stream.read(CHUNK), b""):
                                digest.update(chunk)
                        digests[finding["file"]] = digest.hexdigest()
                    digest = digests[finding["file"]]
                    if re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))) and entry["sha256"] == digest:
                        reason = "Public third-party fixture reviewed by exact file SHA-256; see audit-exceptions.json"
                        break
        if reason:
            reviewed.append({**finding, "reason": reason})
        else:
            blocked.append(finding)
    return blocked, reviewed


def audit(root: Path, bundle: Path, platform: str):
    findings = []
    try:
        _check.check_bundle(root, bundle, platform=platform)
        bundle = bundle.resolve()
        files, links = _actual(bundle, platform)
        _manifest(bundle, files, links, platform)
        if json.loads((bundle / "package-manifest.json").read_text())["version"] != _check._version(root):
            raise ValueError("manifest version mismatch")
    except Exception:
        return {"status": "blocked", "findings": [{"rule": "package_integrity", "file": "package-manifest.json", "offset": 0, "count": 1}], "bytecode": "not_scanned"}
    for rel, path in sorted(files.items()):
        findings.extend(_find_bytes(path, rel))
        findings.extend(_direct_url(path, rel))
    code_findings, bytecode = _scan_code(bundle, files)
    findings.extend(code_findings)
    findings.sort(key=lambda item: (item["file"], item["offset"], item["rule"]))
    findings, reviewed = _review(root, bundle, files, findings)
    return {"status": "blocked" if findings else "passed", "findings": findings, "reviewedIndicators": reviewed, "bytecode": bytecode,
            "scope": "Manifest integrity, all artifact file bytes, available frozen code constants, selected credential indicators; not a general secret-free/compliance certification",
            "fileCount": len(files), "linkCount": len(links)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--platform", choices=("linux", "win32"), required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = audit(args.root, args.bundle, args.platform)
    args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "finding_count": len(report["findings"]), "bytecode": report["bytecode"]}, sort_keys=True))
    return 1 if report["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
