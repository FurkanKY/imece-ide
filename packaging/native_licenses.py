"""Read-only native-library provenance/license staging from the build TOC.

Debian/Ubuntu system copyrights are copied offline. Wheel-native components
remain covered by the Python/Qt/Node inventories, with release review explicit.
This is not a legal compliance certification.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def _query_owner(source: Path):
    for path in dict.fromkeys([str(source), str(source.resolve()), '/usr' + str(source) if str(source).startswith('/lib/') else str(source)]):
        proc = subprocess.run(['dpkg-query', '--search', path], capture_output=True, text=True, timeout=5)
        if proc.returncode:
            continue
        for line in proc.stdout.splitlines():
            if ': ' not in line:
                continue
            name = line.rsplit(': ', 1)[0].split(', ')[0]
            if re.fullmatch(r'[a-z0-9+.-]+(?::[a-z0-9-]+)?', name):
                version = subprocess.run(['dpkg-query', '-W', '-f=${Version}', name], capture_output=True, text=True, timeout=5)
                return name.split(':')[0], version.stdout.strip() if version.returncode == 0 else None
    return None, None


def collect_native(root: Path, bundle: Path, platform: str, doc_root=Path('/usr/share/doc'), common_root=Path('/usr/share/common-licenses')) -> dict:
    destination = bundle / '_internal/licenses/native'
    if platform != 'linux':
        report = {'platform': platform, 'libraries': [], 'reviewRequired': ['Native Windows DLL provenance must be reviewed on the actual Windows build'], 'stagedFiles': []}
        destination.mkdir(parents=True, exist_ok=True)
        (destination/'inventory.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n')
        return report
    toc = root / 'build/ImeceIDE/COLLECT-00.toc'
    entries = ast.literal_eval(toc.read_text(encoding='utf-8'))[0]
    libraries, missing, staged = [], [], set()
    owners = {}
    for name, raw_source, kind in entries:
        if kind not in ('BINARY', 'EXTENSION'):
            continue
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe native build TOC entry')
        target = bundle / '_internal' / relative
        if not target.is_file():
            continue
        source = Path(raw_source)
        system = str(source).startswith(('/lib/', '/usr/lib/'))
        item = {'file': target.relative_to(bundle).as_posix(), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'sourceKind': 'system' if system else 'wheel-or-build', 'licenseFiles': []}
        if system:
            package, version = _query_owner(source)
            item.update({'sourcePackage': package, 'sourcePackageVersion': version})
            if package not in owners:
                copied = []
                if package:
                    copyright_path = (doc_root / package / 'copyright').resolve()
                    if copyright_path.is_file() and copyright_path.is_relative_to(doc_root.resolve()):
                        data = copyright_path.read_bytes()
                        output = destination / package / 'copyright'
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(data)
                        copied.append(output.relative_to(bundle).as_posix())
                        for common in sorted(set(re.findall(rb'/usr/share/common-licenses/([A-Za-z0-9.+_-]+)', data))):
                            label = common.decode('ascii')
                            full = (common_root / label).resolve()
                            if full.is_file() and full.is_relative_to(common_root.resolve()):
                                out = destination / 'common' / label
                                out.parent.mkdir(parents=True, exist_ok=True)
                                shutil.copyfile(full, out)
                                copied.append(out.relative_to(bundle).as_posix())
                owners[package] = copied
            item['licenseFiles'] = owners[package]
            staged.update(item['licenseFiles'])
            if not item['licenseFiles']:
                missing.append(item['file'])
        libraries.append(item)
    report = {'platform': platform, 'libraries': sorted(libraries, key=lambda item: item['file']), 'reviewRequired': sorted(missing), 'stagedFiles': sorted(staged), 'scope': 'Offline native provenance/copyright texts; Qt/Chromium and legal source obligations remain release review, not certified'}
    destination.mkdir(parents=True, exist_ok=True)
    (destination/'inventory.json').write_text(json.dumps(report, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    return report
