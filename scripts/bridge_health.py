"""Layered non-mutating diagnostics; never start apps or hide unchecked layers."""
import json,subprocess,time
import windows_prism_executor as executor


def probe_windows(policy):
    result={'vm_state':'unknown','guest_reachable':False,'instances':[]};started=time.monotonic()
    try:
        cp=subprocess.run([executor.PRLCTL,'list','-a','-j'],capture_output=True,timeout=3,stdin=subprocess.DEVNULL,close_fds=True)
        if cp.returncode:raise RuntimeError('VM inventory unavailable')
        matches=[vm for vm in json.loads(cp.stdout)if vm.get('uuid','').strip('{}')==policy['vm_id'].strip('{}')]
        if len(matches)!=1:raise ValueError('Configured VM missing or ambiguous')
        result['vm_state']=matches[0].get('status','unknown')
        if result['vm_state']=='running':
            guest=executor.guest_call(policy,'inventory',timeout=5)
            result['guest_reachable']=guest.get('available')is True
            result['instances']=guest.get('instances',[])
    except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError)as error:result['error']=str(error)[-400:]
    result['seconds']=time.monotonic()-started
    return result


def status(runtime,policy,native,ppt,*,probe=False):
    stale=runtime.get('restart_required')is True
    layers={'runtime':{'state':'restart_required'if stale else'current'},
        'transport':{'state':'responding','kind':runtime.get('transport','stdio')},
        'configuration':{'state':'configured'if policy.get('ready')else'unavailable'},
        'native_queue':{'state':'blocked'if native.get('blocked')else'ready','operation_id':native.get('operation_id')},
        'ppt_queue':{'state':'blocked'if ppt.get('blocked')else'ready','operation_id':ppt.get('operation_id')},
        'vm':{'state':'not_probed'},'executor':{'state':'not_probed'},'prism':{'state':'not_probed'},
        'powerpoint':{'state':'not_probed'},'ole':{'state':'not_probed'},
        'ui_input':{'state':'not_used','mode':'native_script_and_com','accessibility':'not_required_for_this_backend'}}
    verified=None;observed=None
    if probe and not stale and policy.get('ready'):
        observed=probe_windows(policy);layers['vm']['state']=observed['vm_state']
        layers['executor']['state']='reachable'if observed['guest_reachable']else'unreachable'
        if observed['guest_reachable']:
            names=[p.get('executable','').replace('\\','/').rsplit('/',1)[-1].lower()for p in observed['instances']]
            layers['prism']['state']='running'if any(n in('prism.exe','prism_cn.exe','prism_en.exe')for n in names)else'not_running'
            layers['powerpoint']['state']='running'if'powerpnt.exe'in names else'not_running'
        verified=bool(observed['guest_reachable']and not native.get('blocked')and not ppt.get('blocked'))
    return {'layers':layers,'dispatch_preconditions_verified':verified,'probe':observed,
        'note':'Running processes or responding MCP do not prove OLE responsiveness, scientific correctness, or figure acceptance.'}
