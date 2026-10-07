"""Repeatable engineering handoff: prepare receipts/docs, audit, archive.

No acceptance/publication is granted. Run smoke explicitly between prepare and
archive. Runtime tools and user/provider/LAN work are never activated here.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import tarfile
import zipfile


HERE = Path(__file__).resolve().parent


def _tool(name):
    return runpy.run_path(str(HERE / name))


def _git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 else None


def prepare(root: Path, bundle: Path, target: str):
    checks = _tool('check.py')
    checks['check_bundle'](root, bundle, platform=target)
    inventory = _tool('dependencies.py')['build_inventory'](root/'requirements.txt')
    (bundle/'DEPENDENCIES.json').write_text(json.dumps(inventory, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    docs = bundle/'docs'
    docs.mkdir(exist_ok=True)
    for name in ['MANUAL-ACCEPTANCE.md', 'M5-PACKAGING.md', 'M5-AUDIT.md', 'M3-ACCEPTANCE.md', 'M4-DELIVERY.md', 'M4-OWNER-LAN.md', 'SETUP.md', 'RELEASE.md']:
        shutil.copyfile(root/'docs'/name, docs/name)
    (bundle/'MANUAL-TEST.md').write_text('# Imece IDE engineering test package\n\nKeep the entire folder, including `_internal`.\nRun `ImeceIDE` on Linux or `ImeceIDE.exe` on Windows.\n\nTurkish test guide: [docs/MANUAL-ACCEPTANCE.md](docs/MANUAL-ACCEPTANCE.md).\n\nThis is not a public/accepted release. Git, selected provider CLI and project\ntoolchains remain external. Inspect BUILD-INFO.json, DEPENDENCIES.json and\n_internal/licenses/inventory.json. Smoke/audit reports are archive sidecars.\n', encoding='utf-8')
    # Old one-off smoke copies are removed: fresh sidecars attest to this exact
    # immutable manifest, avoiding a report-in-manifest circular dependency.
    for name in ['package-smoke.json', 'helper-smoke.json', 'artifact-audit.json']:
        (bundle/name).unlink(missing_ok=True)
    native = _tool('native_licenses.py')['collect_native'](root, bundle, target)
    info = {
        'appVersion':checks['_version'](root), 'builtAtUtc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'purpose':'engineering/manual testing, not a public or accepted release',
        'sourceHead':_git(root,'rev-parse','HEAD'), 'sourceBranch':_git(root,'branch','--show-current'),
        'sourceState':'working-tree; inspect artifact manifest, not HEAD alone',
        'sourceHeadIsCompleteArtifactIdentity':False, 'targetPlatform':target,
        'buildPlatform':platform.platform(), 'architecture':platform.machine(),
        'pythonVersion':platform.python_version(),
        'buildTools':{name:metadata.version(name) for name in ['PyInstaller','PySide6','nodejs-wheel-binaries','debugpy']},
        'rendering':'Chromium software default' if target=='linux' else 'platform default',
        'licenseInventory':'_internal/licenses/inventory.json', 'nativeLicenseInventory':'_internal/licenses/native/inventory.json',
        'openAcceptance':['clean machine','real providers','Windows frozen/native','real two-machine LAN','hardware/X11 rendering','legal redistribution/source review','signing/publication'],
        'nativeLicenseTextReviewCount':len(native['reviewRequired']),
    }
    (bundle/'BUILD-INFO.json').write_text(json.dumps(info, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    checks['check_bundle'](root,bundle,write_manifest=True,platform=target)
    return info


def archive(root: Path, bundle: Path, target: str, smoke: Path, helper_smoke: Path, output: Path):
    report = json.loads(smoke.read_text(encoding='utf-8'))
    supervisor = report.get('supervisor', {})
    if (report.get('ok') is not True or report.get('normalCloseOk') is not True
            or supervisor.get('producerQuiescent') is not True
            or supervisor.get('commandExitCode') != 7 or supervisor.get('invalidDispatchExitCode') != 125):
        raise ValueError('Successful frozen GUI/normal-close/supervisor smoke report required')
    manifest_hash = hashlib.sha256((bundle/'package-manifest.json').read_bytes()).hexdigest()
    executable = bundle/('ImeceIDE.exe' if target == 'win32' else 'ImeceIDE')
    executable_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    expected = {'manifestSha256': manifest_hash, 'executableSha256': executable_hash}
    if any(report.get(key) != value for key, value in expected.items()):
        raise ValueError('GUI smoke receipt missing or stale for current bundle')
    if not helper_smoke.is_file():
        raise ValueError('Successful helper smoke receipt required')
    helper = json.loads(helper_smoke.read_text(encoding='utf-8'))
    required_checks = {'nodeVersionOk', 'debugpyVersionOk', 'lspInitializeShutdownOk', 'producerQuiescent'}
    if helper.get('ok') is not True or not isinstance(helper.get('checks'), dict) or any(helper['checks'].get(name) is not True for name in required_checks):
        raise ValueError('Successful helper smoke receipt required')
    if any(helper.get(key) != value for key, value in expected.items()):
        raise ValueError('Helper smoke receipt missing or stale for current bundle')
    if output.resolve().is_relative_to(bundle.resolve()) or output.is_symlink():
        raise ValueError('Archive output cannot be within the bundle or a symlink')
    output.parent.mkdir(parents=True,exist_ok=True)
    audit = _tool('audit.py')['audit'](root,bundle,target)
    audit_path = output.parent/'artifact-audit.json'
    audit_path.write_text(json.dumps(audit, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    if audit['status'] != 'passed' or audit['bytecode'] != 'available':
        raise ValueError('Artifact integrity/content or frozen-bytecode audit requires correction/review')
    output.parent.mkdir(parents=True,exist_ok=True)
    if target == 'linux':
        def owner(entry):
            entry.uid=entry.gid=0
            entry.uname=entry.gname=''
            return entry
        with tarfile.open(output,'w:gz',dereference=False) as archive_:
            archive_.add(bundle,arcname='ImeceIDE',filter=owner)
    else:
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive_:
            for path in sorted(bundle.rglob('*')):
                if path.is_file():
                    archive_.write(path,path.relative_to(bundle).as_posix())
    digest=hashlib.sha256()
    with output.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    (output.parent/'SHA256SUMS.txt').write_text(digest.hexdigest()+'  '+output.name+'\n',encoding='ascii')
    receipt = {'archive':output.name,'sha256':digest.hexdigest(),'appVersion':_tool('check.py')['_version'](root),
               'manifestSha256':hashlib.sha256((bundle/'package-manifest.json').read_bytes()).hexdigest(),
               'engineeringSmokePassed':True,'helperSmokePassed':True,'executableSha256':executable_hash,
               'sourceHeadIsCompleteArtifactIdentity':False,'releaseAcceptance':False}
    (output.parent/'delivery-receipt.json').write_text(json.dumps(receipt,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    return receipt


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','archive'])
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--platform',choices=['linux','win32'],required=True)
    parser.add_argument('--smoke-report',type=Path)
    parser.add_argument('--helper-smoke-report',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=='prepare':
            prepare(args.root.resolve(),args.bundle,args.platform)
        else:
            if args.smoke_report is None or args.helper_smoke_report is None or args.output is None:
                raise ValueError('archive requires GUI/helper smoke reports and --output')
            archive(args.root.resolve(),args.bundle,args.platform,args.smoke_report,args.helper_smoke_report,args.output)
    except (OSError,ValueError,KeyError,subprocess.SubprocessError) as exc:
        print('delivery failed: '+type(exc).__name__)
        return 1
    print('Engineering delivery '+args.command+' passed; release acceptance remains open')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
