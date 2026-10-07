#!/usr/bin/env python3
"""Bounded, isolated frozen Node/debugpy/LSP smoke with supervisor receipts."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time

LIMIT = 256 * 1024
DEADLINE = 25


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def extract_frames(buffer):
    messages = []
    while b'\r\n\r\n' in buffer:
        header, body = buffer.split(b'\r\n\r\n', 1)
        lengths = [line.split(b':', 1)[1].strip() for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')]
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise ValueError('invalid frame length')
        length = int(lengths[0])
        if length > LIMIT:
            raise ValueError('invalid frame length')
        if len(body) < length:
            break
        message = json.loads(body[:length])
        if not isinstance(message, dict):
            raise ValueError('invalid JSON-RPC message')
        messages.append(message)
        buffer = body[length:]
    if len(buffer) > LIMIT:
        raise ValueError('frame buffer exceeded limit')
    return messages, buffer


def frame(message):
    body = json.dumps(message, separators=(',', ':')).encode()
    return b'Content-Length: %d\r\n\r\n' % len(body) + body


def terminate(proc):
    if proc.poll() is None:
        if getattr(proc, '_cancel_path', None) is not None:
            proc._cancel_path.write_text('cancel', encoding='ascii')
        else:
            proc.terminate()  # Linux supervisor drains its subreaper tree.
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()  # Windows supervisor exit closes the kill-on-close Job.
            proc.wait(timeout=3)


def launch(argv, env, cwd, executable):
    """Contain helpers with the actual frozen Linux subreaper/Windows Job."""
    nonce = secrets.token_hex(32)
    config_read, config_write = os.pipe()
    receipt_read = receipt_write = None
    options = {}
    windows = sys.platform == 'win32'
    if windows:
        import msvcrt
        handle = msvcrt.get_osfhandle(config_read)
        os.set_handle_inheritable(handle, True)
        startup = subprocess.STARTUPINFO()
        startup.lpAttributeList = {'handle_list': [handle]}
        options.update(startupinfo=startup, close_fds=True)
        receipt = Path(cwd) / (nonce + '.receipt.json')
        cancel = Path(cwd) / (nonce + '.cancel')
        args = ['--windows-acp', str(handle), str(receipt), str(cancel), nonce]
    else:
        receipt_read, receipt_write = os.pipe()
        options['pass_fds'] = (config_read, receipt_write)
        args = [str(config_read), str(receipt_write), nonce]
    proc = None
    try:
        proc = subprocess.Popen([str(executable), '--imece-process-supervisor', *args], cwd=cwd, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        proc._nonce = nonce
        proc._receipt_path = receipt if windows else None
        proc._receipt_fd = receipt_read
        proc._cancel_path = cancel if windows else None
        payload = json.dumps({'argv':list(map(str, argv)), 'cwd':str(cwd), 'env':env, 'stdio':'acp'}).encode() + b'\n'
        with os.fdopen(config_write, 'wb') as config:
            config_write = None
            config.write(payload)
        return proc
    except BaseException:
        if proc is not None:
            terminate(proc)
        if receipt_read is not None:
            os.close(receipt_read)
        raise
    finally:
        os.close(config_read)
        if config_write is not None:
            os.close(config_write)
        if receipt_write is not None:
            os.close(receipt_write)


def wait_target(proc, timeout):
    if proc.wait(timeout=timeout) != 0:
        raise ValueError('helper supervisor failed')
    if proc._receipt_path is not None:
        if proc._receipt_path.stat().st_size > 1024:
            raise ValueError('helper receipt exceeded bound')
        raw = proc._receipt_path.read_bytes()
    else:
        os.set_blocking(proc._receipt_fd, False)
        raw = os.read(proc._receipt_fd, 1025)
    receipt = json.loads(raw)
    if len(raw) > 1024 or receipt.get('nonce') != proc._nonce or receipt.get('quiescent') is not True or receipt.get('cancelled', False) is not False:
        raise ValueError('helper producer did not authenticate quiescence')
    if type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0:
        raise ValueError('helper command failed')
    return 0


def close_process(proc):
    terminate(proc)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None and not stream.closed:
            stream.close()
    if getattr(proc, '_receipt_fd', None) is not None:
        os.close(proc._receipt_fd)
        proc._receipt_fd = None


def invoke(argv, env, cwd, executable, timeout=DEADLINE):
    proc = launch(argv, env, cwd, executable)
    proc.stdin.close()
    chunks = {'stdout': bytearray(), 'stderr': bytearray()}
    def drain(name, stream):
        while True:
            data = os.read(stream.fileno(), 4096)
            if not data:
                break
            room = LIMIT - len(chunks[name])
            if room > 0:
                chunks[name].extend(data[:room])
    threads = [threading.Thread(target=drain, args=(name, getattr(proc, name)), daemon=True) for name in chunks]
    for thread in threads: thread.start()
    try:
        wait_target(proc, timeout)
        for thread in threads: thread.join(timeout=2)
        if any(thread.is_alive() for thread in threads) or any(len(x) >= LIMIT for x in chunks.values()):
            raise ValueError('helper output limit exceeded')
        return bytes(chunks['stdout']).decode('utf-8', 'replace').strip()
    finally:
        terminate(proc)
        for thread in threads: thread.join(timeout=2)
        close_process(proc)


def lsp(exe, env, cwd):
    proc = launch([str(exe), '--imece-lsp', '--stdio'], env, cwd, exe)
    events = queue.Queue(maxsize=128)
    overflow = threading.Event()
    totals = {'stdout': 0, 'stderr': 0}
    def read(name, stream):
        read_bytes = 0
        while True:
            data = os.read(stream.fileno(), 4096)
            read_bytes += len(data)
            if read_bytes > LIMIT:
                overflow.set()
            elif not overflow.is_set():
                try:
                    events.put((name, data), timeout=.2)
                except queue.Full:
                    overflow.set()
            if not data: break
    readers = [threading.Thread(target=read, args=(n, getattr(proc, n)), daemon=True) for n in totals]
    for thread in readers: thread.start()
    buf = bytearray(); initialized = False; shutdown = False
    def send(msg): proc.stdin.write(frame(msg)); proc.stdin.flush()
    try:
        send({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'processId':os.getpid(),'rootUri':Path(cwd).as_uri(),'capabilities':{}}})
        deadline = time.monotonic() + DEADLINE
        while time.monotonic() < deadline and not shutdown:
            if overflow.is_set(): raise ValueError('LSP output limit exceeded')
            if proc.poll() is not None and events.empty(): break
            try: name, data = events.get(timeout=min(.2, max(0, deadline-time.monotonic())))
            except queue.Empty: continue
            totals[name] += len(data)
            if totals[name] > LIMIT: raise ValueError('LSP output limit exceeded')
            if name == 'stderr' or not data: continue
            buf.extend(data)
            messages, rest = extract_frames(bytes(buf)); buf = bytearray(rest)
            for msg in messages:
                if msg.get('id') == 1:
                    if initialized or not isinstance(msg.get('result'),dict) or 'capabilities' not in msg['result']:
                        raise ValueError('LSP initialize failed')
                    initialized = True
                    send({'jsonrpc':'2.0','method':'initialized','params':{}})
                    send({'jsonrpc':'2.0','id':2,'method':'shutdown','params':None})
                elif msg.get('id') == 2:
                    if not initialized or msg.get('error') is not None:
                        raise ValueError('LSP shutdown failed')
                    send({'jsonrpc':'2.0','method':'exit','params':None})
                    proc.stdin.close(); shutdown = True
        if not initialized or not shutdown: raise ValueError('LSP handshake timed out')
        wait_target(proc, 5)
        for thread in readers: thread.join(timeout=2)
        if overflow.is_set() or any(thread.is_alive() for thread in readers):
            raise ValueError('LSP output did not drain')
    finally:
        terminate(proc)
        for thread in readers: thread.join(timeout=2)
        close_process(proc)


def main():
    bundle = Path(os.environ.get('IMECE_PACKAGE_BUNDLE', Path(__file__).resolve().parents[1] / 'dist' / 'ImeceIDE')).resolve()
    windows = sys.platform == 'win32'
    exe = bundle / ('ImeceIDE.exe' if windows else 'ImeceIDE')
    node = bundle / '_internal' / 'nodejs_wheel' / ('node.exe' if windows else 'bin/node')
    checks = {}
    try:
        with tempfile.TemporaryDirectory(prefix='imece-helper-smoke-') as temp:
            scratch = Path(temp).resolve()
            for name in ('home','data','config','cache','temp'): (scratch/name).mkdir()
            env = {'HOME':str(scratch/'home'),'XDG_DATA_HOME':str(scratch/'data'),'XDG_CONFIG_HOME':str(scratch/'config'),
                   'XDG_CACHE_HOME':str(scratch/'cache'),'TMPDIR':str(scratch/'temp'),'TEMP':str(scratch/'temp'),'TMP':str(scratch/'temp'),
                   'PATH':'', 'LANG':'C.UTF-8'}
            if windows:
                root = os.environ.get('SystemRoot', r'C:\Windows')
                env.update({'SystemRoot':root,'WINDIR':root,'USERPROFILE':str(scratch/'home'),
                            'LOCALAPPDATA':str(scratch/'data'),'APPDATA':str(scratch/'config'),
                            'PATH':root+';'+str(Path(root)/'System32')})
            node_version = invoke([str(node),'--version'],env,str(scratch),exe)
            debug_version = invoke([str(exe),'--imece-debugpy','--version'],env,str(scratch),exe)
            lsp(exe,env,str(scratch))
        checks = {'nodeVersionOk':bool(re.fullmatch(r'v\d+\.\d+\.\d+', node_version)),
                  'debugpyVersionOk':bool(re.fullmatch(r'\d+\.\d+\.\d+', debug_version)),
                  'lspInitializeShutdownOk':True, 'producerQuiescent':True}
        ok = all(checks.values())
    except (OSError, ValueError, subprocess.SubprocessError):
        ok = False
    report = {'ok':ok, 'checks':checks,
              'manifestSha256':digest(bundle/'package-manifest.json') if (bundle/'package-manifest.json').is_file() else None,
              'executableSha256':digest(exe) if exe.is_file() else None}
    dest = os.environ.get('IMECE_HELPER_SMOKE_REPORT')
    text = json.dumps(report, sort_keys=True, indent=2)+'\n'
    if dest: Path(dest).write_text(text, encoding='utf-8')
    print(text, end='')
    return 0 if ok and report['manifestSha256'] and report['executableSha256'] else 1


if __name__ == '__main__': raise SystemExit(main())
