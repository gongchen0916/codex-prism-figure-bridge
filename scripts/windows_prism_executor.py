"""Host transport for the Windows-only Prism executor; no inherited sockets."""
import hashlib
import base64
import json
import math
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
EXPORT_COMMANDS=('exportsvg','exportpdf','exportpng','exportemf','exportwmf','exportbmp','exportjpg','exportpcx','exportpict','exporttif','exporteps','export')
import time
import uuid

PRLCTL = '/Applications/Parallels Desktop.app/Contents/MacOS/prlctl'
WORKER = Path(__file__).with_name('windows_prism_worker.py')
_UUID = re.compile(r'\{[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\}')


def load_policy(root):
    path = Path(root) / 'assets/prism_execution.json'
    if path.is_symlink() or path.stat().st_size > 8192:
        raise ValueError('Unsafe Windows execution policy')
    value = json.loads(path.read_text())
    base = {'schema', 'backend', 'vm_id', 'vm_name', 'phase', 'macos_execution_allowed'}
    ready = {'shared_root', 'windows_share', 'windows_python', 'prism_exe', 'prism_sha256'}
    if not isinstance(value, dict) or value.get('backend') != 'windows_vm' or value.get('macos_execution_allowed') is not False:
        raise ValueError('Windows-only execution policy required')
    if not _UUID.fullmatch(value.get('vm_id', '')) or value.get('vm_name') != 'Windows 11':
        raise ValueError('Unexpected virtual machine identity')
    if value.get('schema') == 1 and set(value) == base and value['phase'] == 'awaiting_windows_installation_and_acceptance':
        return {**value, 'ready': False, 'reason': 'Windows Prism execution pending configuration and native acceptance.'}
    if type(value.get('schema')) is not int or value['schema'] != 2 or set(value) != base | ready or value['phase'] != 'native_accepted':
        raise ValueError('Windows executor is not accepted')
    if not Path(value['shared_root']).is_absolute() or not re.fullmatch(r'\\\\Mac\\[^\\/:\r\n]+', value['windows_share']):
        raise ValueError('Explicit Parallels shared folder required')
    for key in ('windows_python', 'prism_exe'):
        p = PureWindowsPath(value[key])
        if not p.is_absolute() or p.drive.startswith('\\') or p.suffix.lower() != '.exe' or '..' in p.parts or any(c in value[key] for c in '\r\n\x00"'):
            raise ValueError('Local Windows executable required: ' + key)
    if not re.fullmatch('[0-9a-f]{64}', value['prism_sha256']):
        raise ValueError('Prism executable fingerprint required')
    return {**value, 'ready': True, 'reason': 'Windows Prism executor configured; each result requires native verification.'}


def guest_call(policy, action, job=None, timeout=15, worker=WORKER):
    # The helper is staged separately and runs with a short argv. Parallels
    # cannot reliably pass long encoded PowerShell payloads to the guest.
    # Keep RPC argv ASCII. Non-ASCII UNC arguments can intermittently fail in
    # Parallels' session API even though Windows itself can read the same path.
    helper_dir = Path.home() / '.codex' / 'prism-guest-helpers'
    helper_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(Path(worker).read_bytes()).hexdigest()
    helper = helper_dir / (digest + '.py')
    if not helper.exists():
        shutil.copyfile(worker, helper)
    if hashlib.sha256(helper.read_bytes()).hexdigest() != digest:
        raise ValueError('Guest helper identity mismatch')
    guest_helper = str(PureWindowsPath(r'\\Mac\Home\.codex\prism-guest-helpers') / helper.name)
    args = [PRLCTL, 'exec', policy['vm_id'], '--current-user', policy['windows_python'], guest_helper, action]
    if job is not None:
        guest_job=str(PureWindowsPath(policy['windows_share']) / 'PrismBridge' / 'jobs' / job.name)
        args.append('b64.'+base64.urlsafe_b64encode(guest_job.encode('utf8')).decode('ascii'))
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, stdin=subprocess.DEVNULL, timeout=timeout)
    if result.returncode:
        diagnostic=(result.stderr+b'\n'+result.stdout).decode('utf-8','replace').strip()
        raise RuntimeError('Windows guest command failed (exit '+str(result.returncode)+'): ' + diagnostic[-1500:])
    for line in reversed(result.stdout.decode('utf-8', 'replace').splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise RuntimeError('No Windows executor JSON response')


def script_files(script, *, require_set_path=True):
    text = Path(script).read_text(encoding='utf-8')
    if require_set_path and len(re.findall(r'(?m)^SetPath\s+"[^"\r\n]+"\s*$', text)) != 1:
        raise ValueError('Exactly one staged SetPath required')
    outputs = {Path(script).with_suffix('.log').name}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('//'):
            continue
        command = line.split()[0].lower()
        if command in ('quit', 'exit', 'shell', 'exec', 'system', 'setpath') and command != 'setpath':
            raise ValueError('Unscoped Prism command rejected: ' + command)
        if command in ('open', 'save', 'openoutput', 'import', 'importlink', 'insertdata', 'insertdatalink',*EXPORT_COMMANDS):
            names = re.findall(r'"([^"\r\n]+)"', line)
            for name in names:
                if Path(name).name != name or any(c in name for c in '\\:/%') or name in ('.', '..'):
                    raise ValueError('Prism file references must be local filenames: ' + name)
            if command in ('save','openoutput',*EXPORT_COMMANDS):
                outputs.update(names)
    return outputs


def rejected_local_file_proof(record):
    """Recover only two provably impossible pre-job syntax failures.

    The executor has always required local file references before writing job
    metadata or invoking a guest. An unchanged offending script plus absent
    metadata proves this narrow preflight failure, not an application outcome.
    The mandatory quoted SetPath is checked at the same pre-job boundary.
    Other validation, worker and transport failures are deliberately excluded.
    """
    script=Path(record['script'])
    if script.is_symlink() or not script.is_file() or script.stat().st_size>4*1024*1024:
        return False
    metadata=script.with_suffix('.windows-job.json')
    if metadata.exists() or metadata.is_symlink():return False
    raw=script.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['script_sha256']:return False
    try:script_files(script)
    except ValueError as error:
        return (str(error).startswith('Prism file references must be local filenames: ')
                or (str(error)=='Exactly one staged SetPath required'
                    and not record.get('prism_processes')
                    and re.search(r'(?m)^SetPath\s',raw.decode('utf8'))is not None))
    except (OSError,UnicodeError):return False
    return False


def sync_outputs(script, job, request):
    ledger=job/'host-sync.json'
    copied=json.loads(ledger.read_text()) if ledger.exists() else {}
    for name in sorted(set(request['outputs'] + request['inputs'])):
        source = job / 'result' / name
        if source.is_file() and name != request['script']:
            destination = Path(script).parent / name
            if destination.is_symlink():
                raise ValueError('Output destination is a symlink')
            # Avoid changing freshness signatures when polling unchanged files.
            digest=hashlib.sha256(source.read_bytes()).hexdigest()
            first_output=name in request['outputs'] and copied.get(name)!=digest
            if first_output or not destination.is_file() or source.read_bytes() != destination.read_bytes():
                shutil.copyfile(source, destination)
            copied[name]=digest
    ledger.write_text(json.dumps(copied),encoding='utf-8')


def initial_inputs(script):
    """Local files read before any script write, including Open+Save overlaps."""
    required,written=set(),set()
    for line in Path(script).read_text(encoding='utf-8').splitlines():
        fields=line.strip().split()
        if not fields:continue
        command=fields[0].lower();names=re.findall(r'"([^"\r\n]+)"',line)
        if command in ('open','import','importlink','insertdata','insertdatalink'):
            required.update(name for name in names if name not in written)
        elif command in ('save','openoutput',*EXPORT_COMMANDS):
            written.update(names)
    return required


def validate_inputs(script):
    script=Path(script)
    for name in sorted(initial_inputs(script)):
        path=script.parent/name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Missing required input or unsafe symlink: '+name)


def run(script, timeout, done_name, policy):
    if not math.isfinite(timeout) or timeout < 0:
        raise ValueError('Finite nonnegative timeout required')
    script = Path(script).resolve()
    outputs = script_files(script) | {done_name}
    required_inputs=initial_inputs(script)
    validate_inputs(script)
    ident = str(uuid.uuid4())
    job = Path(policy['shared_root']) / 'PrismBridge' / 'jobs' / ident
    (job / 'input').mkdir(parents=True, exist_ok=False)
    inputs, hashes, size = [], {}, 0
    for path in sorted(script.parent.iterdir()):
        if path.name.startswith('.') or (path.name in outputs and path.name not in required_inputs) or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('Symlinked guest input rejected')
        if path.suffix.lower() not in ('.pzc', '.prism', '.pzfx', '.pzf', '.pzt', '.csv', '.tsv', '.txt'):
            continue
        size += path.stat().st_size
        if size > 256 * 1024 * 1024:
            raise ValueError('Guest input stage exceeds 256 MiB')
        shutil.copyfile(path, job / 'input' / path.name)
        inputs.append(path.name)
        hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    request = {'id': ident, 'script': script.name, 'done': done_name, 'inputs': inputs, 'hashes': hashes,
               'outputs': sorted(outputs), 'prism_exe': policy['prism_exe'], 'prism_sha256': policy['prism_sha256'], 'timeout': timeout}
    (job / 'request.json').write_text(json.dumps(request), encoding='utf-8')
    metadata = script.with_suffix('.windows-job.json')
    metadata.write_text(json.dumps({'id': ident, 'job': str(job)}), encoding='utf-8')
    for delivery in range(3):
        try:
            result = guest_call(policy, 'run', job, timeout=max(10, timeout + 15))
            break
        except RuntimeError as exc:
            receipt=job/'receipt.json'
            if receipt.is_file():
                observed=json.loads(receipt.read_text())
                if observed.get('id')==ident and observed.get('state')=='completed':
                    result=observed;break
            # Bounded transport re-delivery, not a Prism script retry. Every
            # message carries the SAME exclusive job identity. No new job,
            # no replay after a worker claim, no retries of native errors.
            if delivery==2 or not re.search(r'PrlJob_Get(?:RetCode|Result): Invalid argument',str(exc)) or (job/'dispatch.claim').exists():
                raise
            time.sleep(.5*(delivery+1))
    if result.get('id') != ident:
        raise ValueError('Windows response operation identity mismatch')
    sync_outputs(script, job, request)
    return result['state'] == 'completed', result.get('native_log') or 'Windows native outcome unknown; guest job retained: ' + ident


def refresh(script, policy):
    metadata = Path(script).with_suffix('.windows-job.json')
    if not metadata.is_file():
        return
    value = json.loads(metadata.read_text())
    if str(uuid.UUID(value['id'])) != value['id']:
        raise ValueError('Invalid guest job identity')
    job = Path(policy['shared_root']) / 'PrismBridge' / 'jobs' / value['id']
    if str(job) != value['job']:
        raise ValueError('Guest job outside configured share')
    result = guest_call(policy, 'collect', job)
    if result['id'] != value['id']:
        raise ValueError('Collected result identity mismatch')
    sync_outputs(script, job, json.loads((job / 'request.json').read_text()))


def cancel_not_started(script,policy):
    """Win the same exclusive claim used by the worker, or refuse cancellation.

    This cancels delivery, not a running Prism process. Even a late Parallels
    RPC cannot execute after this claim has been installed.
    """
    metadata=Path(script).with_suffix('.windows-job.json')
    if not metadata.is_file():return False
    value=json.loads(metadata.read_text())
    if str(uuid.UUID(value['id']))!=value['id']:raise ValueError('Invalid guest job identity')
    job=Path(policy['shared_root'])/'PrismBridge'/'jobs'/value['id']
    if str(job)!=value['job']:raise ValueError('Unexpected guest job root')
    try:
        with (job/'dispatch.claim').open('x')as stream:
            json.dump({'id':value['id'],'state':'cancelled_before_dispatch'},stream)
    except FileExistsError:
        try:
            if json.loads((job/'dispatch.claim').read_text())!={'id':value['id'],'state':'cancelled_before_dispatch'}:
                return False
        except (OSError,ValueError):return False
    result=guest_call(policy,'collect',job)
    return result.get('id')==value['id'] and result.get('state')=='not_started'


def process_inventory(policy):
    try:
        result = guest_call(policy, 'inventory')
        result['instances'] = [p for p in result['instances'] if Path(p['executable'].replace('\\', '/')).name.lower() != 'powerpnt.exe']
        for p in result['instances']:
            p['matches_target'] = p['executable'].lower() == policy['prism_exe'].lower()
        return result
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {'available': False, 'instances': []}
