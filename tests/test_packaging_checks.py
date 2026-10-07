import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

# Keep packaging/ a script directory: a package here shadows PyPA packaging.
_spec = importlib.util.spec_from_file_location("imece_packaging_checks", Path(__file__).parents[1] / "packaging/check.py")
_checks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checks)
PackagingError, check_bundle, check_sources = _checks.PackagingError, _checks.check_bundle, _checks.check_sources


VERSION = "1.2.3"


def source_tree(root: Path, html='<script src="/assets/app.js"></script>'):
    (root / "webhost/api").mkdir(parents=True)
    (root / "web/ui/dist/assets").mkdir(parents=True)
    (root / "webhost/api/app.py").write_text(f'APP_VERSION = "{VERSION}"\n', encoding="utf-8")
    (root / "web/ui/package.json").write_text(json.dumps({"version": VERSION}), encoding="utf-8")
    (root / "web/ui/dist/index.html").write_text(html, encoding="utf-8")
    (root / "web/ui/dist/assets/app.js").write_text("console.log('ok')", encoding="utf-8")
    (root / "LICENSE").write_text("license", encoding="utf-8")
    (root / "THIRD-PARTY-NOTICES.md").write_text("notices", encoding="utf-8")


def bundle_tree(root: Path, bundle: Path):
    source_tree(root)
    internal_dist = bundle / "_internal/web/ui/dist"
    (internal_dist / "assets").mkdir(parents=True)
    (bundle / "_internal/nodejs_wheel").mkdir(parents=True)
    (internal_dist / "index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
    (internal_dist / "assets/app.js").write_text("bundle asset", encoding="utf-8")
    (bundle / "_internal/nodejs_wheel/node.exe").write_bytes(b"node")
    (bundle / "_internal/OpenConsole.exe").write_bytes(b"conpty")
    (bundle / "_internal/winpty-agent.exe").write_bytes(b"winpty")
    (bundle / "ImeceIDE.exe").write_bytes(b"ImeceIDE.exe")
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        (bundle / "_internal" / name).write_bytes(name.encode())


def linux_bundle_tree(root: Path, bundle: Path):
    source_tree(root)
    internal_dist = bundle / "_internal/web/ui/dist"
    (internal_dist / "assets").mkdir(parents=True)
    node = bundle / "_internal/nodejs_wheel/bin/node"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    node.chmod(0o755)
    (internal_dist / "index.html").write_text('<script src="/assets/app.js"></script>', encoding="utf-8")
    (internal_dist / "assets/app.js").write_text("bundle asset", encoding="utf-8")
    exe = bundle / "ImeceIDE"
    exe.write_bytes(b"ImeceIDE")
    exe.chmod(0o755)
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
        (bundle / "_internal" / name).write_bytes(name.encode())


def test_source_preflight_and_rejects_asset_escape(tmp_path):
    source_tree(tmp_path)
    assert check_sources(tmp_path) == VERSION
    (tmp_path / "web/ui/dist/index.html").write_text('<img src="../../outside.png">', encoding="utf-8")
    with pytest.raises(PackagingError, match="escapes dist"):
        check_sources(tmp_path)


def test_source_preflight_rejects_version_mismatch(tmp_path):
    source_tree(tmp_path)
    (tmp_path / "web/ui/package.json").write_text('{"version":"wrong"}', encoding="utf-8")
    with pytest.raises(PackagingError, match="Version mismatch"):
        check_sources(tmp_path)


def test_bundle_manifest_has_deterministic_hashes(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    bundle_tree(root, bundle)
    manifest_path = check_bundle(root, bundle, write_manifest=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == VERSION
    assert manifest["platform"] == "win32"
    assert manifest["files"]["ImeceIDE.exe"] == hashlib.sha256(b"ImeceIDE.exe").hexdigest()
    assert "package-manifest.json" not in manifest["files"]
    assert str(tmp_path) not in manifest_path.read_text(encoding="utf-8")
    before = manifest_path.read_bytes()
    check_bundle(root, bundle, write_manifest=True)
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize("secret", [".env", ".env.local", "prefs.json", "providers.json", "runtime.sqlite3", "runtime.sqlite3-wal", "runtime.sqlite3-shm", "keys.dpapi", "secrets.dat", "SECRETS.DAT", ".ENV.LOCAL"])
def test_bundle_rejects_user_state_and_secrets(tmp_path, secret):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    bundle_tree(root, bundle)
    (bundle / secret).write_text("private", encoding="utf-8")
    with pytest.raises(PackagingError, match="User data/secret"):
        check_bundle(root, bundle)


def test_checks_do_not_shadow_pypa_packaging():
    import packaging.version
    assert packaging.version.Version("1.2.3").major == 1


@pytest.mark.parametrize("directory", [".git", ".PI", ".VENV"])
def test_bundle_rejects_private_directories(tmp_path, directory):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    bundle_tree(root, bundle)
    (bundle / directory).mkdir()
    with pytest.raises(PackagingError, match="Forbidden directory"):
        check_bundle(root, bundle)


def test_bundle_rejects_symlinks(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    bundle_tree(root, bundle)
    try:
        (bundle / "linked").symlink_to(bundle / "_internal/LICENSE")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PackagingError, match="Symlink/reparse"):
        check_bundle(root, bundle)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file-link contract')
def test_linux_bundle_manifest_records_safe_links(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    linux_bundle_tree(root, bundle)
    link = bundle / "_internal/shared-lib.so"
    link.symlink_to("nodejs_wheel/bin/node")
    manifest_path = check_bundle(root, bundle, write_manifest=True, platform="linux")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["platform"] == "linux"
    assert manifest["links"] == {"_internal/shared-lib.so": "nodejs_wheel/bin/node"}
    assert "_internal/shared-lib.so" not in manifest["files"]
    assert str(tmp_path) not in manifest_path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file-link contract')
@pytest.mark.parametrize("target", ["/etc/passwd", "missing.so", "../outside.so"])
def test_linux_bundle_rejects_unsafe_links(tmp_path, target):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    linux_bundle_tree(root, bundle)
    (bundle / "_internal/unsafe.so").symlink_to(target)
    with pytest.raises(PackagingError, match="Unsafe bundle symlink"):
        check_bundle(root, bundle, platform="linux")


def test_linux_bundle_rejects_directory_links_and_cycles(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    linux_bundle_tree(root, bundle)
    try:
        (bundle / "_internal/directory-link").symlink_to(bundle / "_internal", target_is_directory=True)
        (bundle / "_internal/cycle-a").symlink_to("cycle-b")
        (bundle / "_internal/cycle-b").symlink_to("cycle-a")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(PackagingError):
        check_bundle(root, bundle, platform="linux")


@pytest.mark.skipif(os.name != 'posix', reason='POSIX file-link contract')
def test_linux_manifest_output_link_cannot_overwrite_runtime(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    linux_bundle_tree(root, bundle)
    target = bundle / "ImeceIDE"
    before = target.read_bytes()
    (bundle / "package-manifest.json").symlink_to("ImeceIDE")
    with pytest.raises(PackagingError, match="Manifest output"):
        check_bundle(root, bundle, write_manifest=True, platform="linux")
    assert target.read_bytes() == before


def test_linux_bundle_requires_platform_specific_runtime(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    linux_bundle_tree(root, bundle)
    (bundle / "_internal/nodejs_wheel/bin/node").unlink()
    with pytest.raises(PackagingError, match="Required bundle file"):
        check_bundle(root, bundle, platform="linux")


def test_bundle_rejects_missing_referenced_asset(tmp_path):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    bundle_tree(root, bundle)
    (bundle / "_internal/web/ui/dist/index.html").write_text('<script src="/missing.js"></script>', encoding="utf-8")
    with pytest.raises(PackagingError, match="missing"):
        check_bundle(root, bundle)


@pytest.mark.parametrize("payload", ["_internal/LICENSE", "_internal/nodejs_wheel/node.exe", "_internal/OpenConsole.exe", "_internal/winpty-agent.exe"])
def test_bundle_missing_runtime_payload_fails(tmp_path, payload):
    root, bundle = tmp_path / "src", tmp_path / "bundle"
    bundle_tree(root, bundle)
    (bundle / payload).unlink()
    with pytest.raises(PackagingError, match="missing"):
        check_bundle(root, bundle)


def test_encoded_backslash_asset_escape_is_rejected(tmp_path):
    source_tree(tmp_path, '<img src="..%5coutside.png">')
    with pytest.raises(PackagingError, match="escapes dist"):
        check_sources(tmp_path)


def test_version_command_does_not_require_build_or_qt(tmp_path, capsys):
    source_tree(tmp_path)
    (tmp_path / "web/ui/dist/index.html").unlink()
    assert _checks.main(["version", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == VERSION
