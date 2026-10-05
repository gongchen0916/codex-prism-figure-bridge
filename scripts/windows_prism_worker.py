"""Guest-side file executor. Each unique job dispatches at most once.

Uses Prism's documented @PZC interface, not keyboard/mouse automation.
Timeout preserves the job and never kills Prism or replays a native operation.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid


def atomic(path, value):
    temp = path.with_name(path.name + '.new')
    temp.write_text(json.dumps(value, ensure_ascii=True), encoding='utf-8')
    os.replace(temp, path)


def inventory():
    import ctypes
    from ctypes import wintypes
    import win32api, win32process
    query=ctypes.WinDLL('kernel32',use_last_error=True).QueryFullProcessImageNameW
    query.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    query.restype=wintypes.BOOL
    rows = []
    for pid in win32process.EnumProcesses():
        if not pid:
            continue
        try:
            handle = win32api.OpenProcess(0x1000, False, pid)
            try:
                buffer=ctypes.create_unicode_buffer(32768);length=wintypes.DWORD(len(buffer))
                if not query(int(handle),0,buffer,ctypes.byref(length)):
                    continue
                exe=buffer.value
                if Path(exe).name.lower() not in ('prism.exe', 'prism_cn.exe', 'prism_en.exe', 'powerpnt.exe'):
                    continue
                started = win32process.GetProcessTimes(handle)['CreationTime'].isoformat()
                rows.append({'pid': pid, 'started': started, 'executable': exe})
            finally:
                handle.Close()
        except Exception:
            continue
    return {'available': True, 'instances': rows}


def ppt_status(job):
    """Read exact worker liveness; never create, attach, close or activate UI."""
    import win32api,win32process,pywintypes
    job=Path(job);request=json.loads((job/'ppt-request.json').read_text())
    if str(uuid.UUID(request['id']))!=job.name:raise ValueError('PPT status identity mismatch')
    state='unknown'
    try:
        pid=int((job/'ppt.claim').read_text())
        handle=win32api.OpenProcess(0x1000,False,pid)
        try:state='alive'if win32process.GetExitCodeProcess(handle)==259 else'exited'
        finally:handle.Close()
    except pywintypes.error as error:
        if error.winerror==87:state='exited'
    except (ValueError,OSError):pass
    return {'id':request['id'],'helper_state':state,**inventory()}


def read_log(path):
    if not path.exists():
        return ''
    try:raw = path.read_bytes()
    except (FileNotFoundError,PermissionError):
        # CreateLog can replace the file or temporarily deny reads while its
        # Windows writer holds an exclusive handle. Both are pending, never
        # completion: persistent denial reaches the existing deadline and
        # remains unknown unless a readable footer and fresh done file agree.
        return ''
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16', 'replace')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode('utf-8', 'replace')


def completed(text):
    if re.search(r"(?im)^\s*(?:Can't|Cannot)\s+(?:set limits|open|save|export)\b",text):return False
    return bool(re.search(r'(?m)^\s*(?:COMPLETE! No Errors\.|完成[!！]\s*没有错误[。.])\s*$', text))


def validate_job(job):
    job = Path(job)
    request = json.loads((job / 'request.json').read_text(encoding='utf-8'))
    ident = request['id']
    if str(uuid.UUID(ident)) != ident or job.name != ident:
        raise ValueError('Job identity mismatch')
    local = Path(os.environ['LOCALAPPDATA']) / 'PrismBridge' / 'jobs' / ident
    for name in request['inputs'] + request['outputs'] + [request['script'], request['done']]:
        if Path(name).name != name or name in ('', '.', '..') or ':' in name:
            raise ValueError('Only local filenames permitted in guest jobs')
    return job, request, local


def collect(job, request, local):
    claim=job/'dispatch.claim'
    if not local.exists() and claim.is_file():
        try:
            cancellation=json.loads(claim.read_text())
            if cancellation=={'id':request['id'],'state':'cancelled_before_dispatch'}:
                return {'id':request['id'],'state':'not_started','native_log':'Dispatch atomically cancelled before guest worker claim.','processes':[]}
        except ValueError:
            pass
    for name in sorted(set(request['outputs'] + request['inputs'])):
        source = local / name
        if source.is_file() and name != request['script']:
            dest = job / 'result' / name
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, dest)
    text = read_log(local / Path(request['script']).with_suffix('.log'))
    done = (local / request['done']).is_file()
    finished = done and completed(text) and 'PROBLEM!' not in text
    return {'id': request['id'], 'state': 'completed' if finished else 'unknown', 'native_log': text,
            'local_stage': str(local), 'processes': inventory()['instances']}


def execute(job):
    job, request, local = validate_job(job)
    receipt = job / 'receipt.json'
    # Exclusive creation is also the at-most-once delivery fence across processes.
    with (job / 'dispatch.claim').open('x') as f:
        f.write(str(os.getpid()))
    if local.exists():
        raise ValueError('Guest stage already exists; refusing replay')
    local.mkdir(parents=True)
    for name in request['inputs']:
        source = job / 'input' / name
        if hashlib.sha256(source.read_bytes()).hexdigest() != request['hashes'][name]:
            raise ValueError('Guest input fingerprint mismatch: ' + name)
        shutil.copyfile(source, local / name)
    exe = Path(request['prism_exe'])
    if hashlib.sha256(exe.read_bytes()).hexdigest() != request['prism_sha256']:
        raise ValueError('Installed Prism executable changed; repeat installation acceptance')
    original = (local / request['script']).read_text(encoding='utf-8')
    text, count = re.subn(r'(?m)^SetPath\s+"[^"\r\n]+"\s*$', lambda _: 'SetPath "' + str(local) + '"', original)
    if count != 1:
        raise ValueError('Exactly one staged SetPath required')
    # This Windows Prism reader uses the system ANSI code page, including for
    # PZC's first command. UTF-8 BOM is interpreted as command text. Encode
    # strictly to the actual ACP; never replace scientific labels with '?'.
    import ctypes
    codepage=ctypes.windll.kernel32.GetACP()
    (local / request['script']).write_bytes(text.encode('cp'+str(codepage),errors='strict'))
    atomic(receipt, {'id': request['id'], 'state': 'dispatching', 'local_stage': str(local)})
    proc = subprocess.Popen([str(exe), '@' + str(local / request['script'])], cwd=str(local),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    atomic(receipt, {'id': request['id'], 'state': 'active', 'launch_pid': proc.pid, 'local_stage': str(local)})
    deadline = time.monotonic() + request['timeout']
    while time.monotonic() < deadline:
        log = read_log(local / Path(request['script']).with_suffix('.log'))
        if completed(log) or 'PROBLEM! Not completed because of error.' in log:
            break
        time.sleep(.3)
    result = collect(job, request, local)
    atomic(receipt, result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('run', 'collect', 'inventory','ppt_status'))
    parser.add_argument('job', nargs='?')
    args = parser.parse_args()
    if args.job and args.job.startswith('b64.'):
        args.job=base64.urlsafe_b64decode(args.job[4:]).decode('utf8')
    if args.action == 'inventory':
        result = inventory()
    elif args.action == 'ppt_status':
        result=ppt_status(args.job)
    elif args.action == 'collect':
        job, request, local = validate_job(args.job)
        result = collect(job, request, local)
        atomic(job / 'receipt.json', result)
    else:
        try:
            result = execute(args.job)
        except FileExistsError:
            raise  # Existing claim can belong to an already running operation.
        except Exception as exc:
            job,request,local=validate_job(args.job)
            receipt=job/'receipt.json'
            prior=json.loads(receipt.read_text())if receipt.exists()else{}
            if prior.get('state')in('dispatching','active','completed'):
                raise
            # Preparation failed before the recorded launch boundary. Exact
            # encoding/installation/input errors do not require killing Prism.
            result={'id':request['id'],'state':'not_started','native_log':'Unable to launch Prism: preparation rejected: '+str(exc),'processes':[]}
            atomic(receipt,result)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
