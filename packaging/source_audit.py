"""Read-only redacted Gitleaks history + current Git-visible source snapshot.

Ignored local data, .pi and PI_HANDOFF.md are never copied/read. Reviewed test
fixtures are pinned to full file SHA-256; changing them requires renewed review.
Requires an explicitly installed Gitleaks binary; never downloads/installs it.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def scan(root: Path, executable: str, output: Path):
    root=root.resolve()
    pins=json.loads((root/'packaging/source-audit-fixtures.json').read_text())['fixtures']
    reviewed,blocked=[],[]
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='imece-source-audit-') as temp:
        temp=Path(temp)
        snapshot=temp/'source'
        snapshot.mkdir()
        paths=subprocess.check_output(['git','-C',str(root),'ls-files','--cached','--others','--exclude-standard','-z']).split(b'\0')
        count=0
        for name in sorted(set(paths)):
            if not name: continue
            rel=Path(name.decode())
            if rel.is_absolute() or '..' in rel.parts:
                raise ValueError('Unsafe source snapshot entry')
            if rel.parts[0]=='.pi' or rel.as_posix()=='PI_HANDOFF.md': continue
            source=root/rel
            if source.is_symlink():
                raise ValueError('Source snapshot symlink refused')
            if not source.is_file(): continue
            target=snapshot/rel
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
            count+=1
        for scope,path in [('history',root),('current',snapshot)]:
            report=temp/(scope+'.json')
            command=[executable,'git' if scope=='history' else 'dir',str(path),'--redact=100','--no-banner','--report-format','json','--report-path',str(report)]
            if scope=='history': command.append('--log-opts=--all')
            process=subprocess.run(command,capture_output=True,timeout=120)
            if process.returncode not in (0,1) or not report.is_file():
                raise ValueError('Gitleaks scanner failed; no clean result granted')
            parsed=json.loads(report.read_text())
            if process.returncode==1 and not parsed:
                raise ValueError('Scanner failed without a valid findings report')
            for finding in parsed:
                filename=Path(finding['File'])
                if filename.is_absolute(): filename=filename.relative_to(path)
                item={'scope':scope,'file':filename.as_posix(),'rule':finding['RuleID'],'line':finding['StartLine']}
                pin=next((p for p in pins if p['file']==item['file'] and item['rule'] in p['rules']),None)
                accepted=(scope=='current' and pin is not None and hashlib.sha256((snapshot/filename).read_bytes()).hexdigest()==pin['sha256'])
                if accepted:
                    reviewed.append({**item,'reason':'Reviewed synthetic redaction/security-test fixture; exact full-file hash'})
                else:
                    blocked.append(item)
    result={'status':'blocked' if blocked else 'passed','findings':blocked,'reviewedIndicators':reviewed,'sourceFileCount':count,'scope':'all available Git commits plus current Git-visible sources; ignored local/PI data excluded; not a general secret-free certificate'}
    output.write_text(json.dumps(result,sort_keys=True,indent=2)+'\n',encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--gitleaks',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=scan(args.root,args.gitleaks,args.output)
    except (OSError,ValueError,subprocess.SubprocessError):
        print('Source audit failed; no clean result granted')
        return 1
    print(json.dumps({'status':result['status'],'blocking':len(result['findings']),'reviewed':len(result['reviewedIndicators'])}))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':
    raise SystemExit(main())
