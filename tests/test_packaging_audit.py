import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("audit", Path(__file__).parents[1] / "packaging/audit.py")
audit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_mod)


def setup_bundle(tmp_path):
    root, bundle = tmp_path / "source", tmp_path / "bundle"
    (root / "webhost/api").mkdir(parents=True)
    (root / "web/ui/dist/assets").mkdir(parents=True)
    (root / "webhost/api/app.py").write_text('APP_VERSION = "1.2.3"\n')
    (root / "web/ui/package.json").write_text('{"version":"1.2.3"}')
    (root / "web/ui/dist/index.html").write_text('<script src="/assets/app.js"></script>')
    (root / "web/ui/dist/assets/app.js").write_text("ok")
    (root / "LICENSE").write_text("license")
    (root / "THIRD-PARTY-NOTICES.md").write_text("notices")
    d = bundle / "_internal/web/ui/dist"
    (d / "assets").mkdir(parents=True)
    (bundle / "_internal/nodejs_wheel/bin").mkdir(parents=True)
    (d / "index.html").write_text('<script src="/assets/app.js"></script>')
    (d / "assets/app.js").write_bytes(b"safe")
    node = bundle / "_internal/nodejs_wheel/bin/node"
    node.write_bytes(b"node")
    node.chmod(0o755)
    exe = bundle / "ImeceIDE"
    exe.write_bytes(b"exe")
    exe.chmod(0o755)
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        (bundle / "_internal" / name).write_text("payload")
    audit_mod._check.check_bundle(root, bundle, write_manifest=True, platform="linux")
    return root, bundle


def test_chunk_boundary_detection_and_nonmatch(tmp_path):
    payload = b"x" * (audit_mod.CHUNK - 7) + b" AKIA1234567890ABCDEF" + b" ghp_placeholder"
    path = tmp_path / "data.bin"
    path.write_bytes(payload)
    found = audit_mod._find_bytes(path, "data.bin")
    assert [(f["rule"], f["offset"]) for f in found] == [("aws_access_key_id", audit_mod.CHUNK - 6)]


def test_complete_pem_detected_but_marker_alone_not(tmp_path):
    path = tmp_path / "blob"
    path.write_bytes(b"-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----")
    assert audit_mod._find_bytes(path, "blob") == []
    path.write_bytes(b"-----BEGIN PRIVATE KEY-----\n" + b"A" * 32 + b"\n" + b"B" * 32 + b"\n-----END PRIVATE KEY-----")
    assert audit_mod._find_bytes(path, "blob")[0]["rule"] == "pem_private_key"


def test_audit_redacts_matches_and_detects_integrity_tampering(tmp_path):
    root, bundle = setup_bundle(tmp_path)
    target = bundle / "_internal/web/ui/dist/assets/app.js"
    target.write_bytes(b"ghp_" + b"A" * 40 + b" AKIA1234567890ABCDEF")
    report = audit_mod.audit(root, bundle, "linux")
    text = json.dumps(report)
    assert report["status"] == "blocked"
    assert "ghp_" not in text and "AKIA" not in text
    assert {f["rule"] for f in report["findings"]} == {"package_integrity"}


def test_manifest_forged_path_and_hash_tamper_are_safe_failures(tmp_path):
    root, bundle = setup_bundle(tmp_path)
    manifest_path = bundle / "package-manifest.json"
    data = json.loads(manifest_path.read_text())
    data["files"]["../../outside"] = "0" * 64
    manifest_path.write_text(json.dumps(data))
    result = audit_mod.audit(root, bundle, "linux")
    assert result["findings"][0]["rule"] == "package_integrity"
    assert "outside" not in json.dumps(result)
    root, bundle = setup_bundle(tmp_path / "second")
    (bundle / "_internal/LICENSE").write_text("changed")
    assert audit_mod.audit(root, bundle, "linux")["findings"][0]["rule"] == "package_integrity"


def test_credentials_in_direct_url_report_only_offset(tmp_path):
    path = tmp_path / "direct_url.json"
    secret_url = "https://user:password@example.invalid/repo"
    path.write_text(json.dumps({"url": secret_url}))
    findings = audit_mod._direct_url(path, "direct_url.json")
    assert findings == [{"rule": "credential_url", "file": "direct_url.json", "offset": path.read_bytes().find(secret_url.encode()), "count": 1}]
    assert secret_url not in json.dumps(findings)


def test_nested_code_constants_are_scanned_without_execution():
    # A simple reader-like code object avoids importing or executing application code.
    class Code:
        co_consts = ("sk-" + "A" * 40,)
    found = audit_mod._scan_constants(Code(), "ImeceIDE")
    assert found == [{"rule": "openai_api_key", "file": "ImeceIDE", "offset": 0, "count": 1}]


def test_tuple_constants_are_not_ignored():
    class Code:
        co_consts = (("sk-" + "A" * 40,),)
    assert audit_mod._scan_constants(Code(), 'ImeceIDE.exe')[0]['rule'] == 'openai_api_key'


def test_public_fixture_exception_is_exact_hash_only(tmp_path):
    root, bundle = setup_bundle(tmp_path)
    target = bundle/'_internal/public-fixture.bin'
    target.write_bytes(b'-----BEGIN PRIVATE KEY-----\n' + b'A'*32 + b'\n' + b'B'*32 + b'\n-----END PRIVATE KEY-----')
    import hashlib
    (root/'packaging').mkdir()
    (root/'packaging/audit-exceptions.json').write_text(json.dumps({'exceptions':[{'file':'_internal/public-fixture.bin','rule':'pem_private_key','sha256':hashlib.sha256(target.read_bytes()).hexdigest()}]}))
    findings = audit_mod._find_bytes(target,'_internal/public-fixture.bin')
    blocked, reviewed = audit_mod._review(root,bundle,{'_internal/public-fixture.bin':target},findings)
    assert not blocked and reviewed
    target.write_bytes(target.read_bytes()+b'changed')
    blocked, reviewed = audit_mod._review(root,bundle,{'_internal/public-fixture.bin':target},findings)
    assert blocked and not reviewed
