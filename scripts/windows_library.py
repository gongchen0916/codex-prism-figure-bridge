"""Compact Windows source-contract discovery, request preparation and execution.

Historical fixture evidence is not arbitrary-data/style approval. New runs use
the existing full source workflow and physical-size native PowerPoint checker.
Producer code is never executed; source seeds and previous outputs stay intact.
"""
from __future__ import annotations
import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]

def _object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON key: '+key)
        result[key]=value
    return result

def read_json(path):
    path=Path(path)
    if path.is_symlink()or not path.is_file()or path.stat().st_size>32*1024*1024:raise ValueError('Ordinary bounded JSON file required')
    return json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=_object,
                      parse_constant=lambda v:(_ for _ in ()).throw(ValueError('Nonfinite JSON value: '+v)))

def _dev(root,name):
    allowed={'qualified_catalog','library_scope','recipe_job','recipe_workflow','windows_qualification','windows_ppt_qualification'}
    if name not in allowed:raise ValueError('Unknown owned library module')
    directory=Path(root)/'development/library_v1';path=directory/(name+'.py')
    if path.is_symlink()or not path.is_file():raise ValueError('Source library module is unavailable: '+name)
    # This module is used by the isolated runtime child, never loaded into the
    # long-lived MCP parent. Owned sibling imports use the same source root.
    if str(directory)not in sys.path:sys.path.insert(0,str(directory))
    spec=importlib.util.spec_from_file_location('_windows_library_'+name,path)
    module=importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(),str(path),'exec',dont_inherit=True),module.__dict__)
    return module

def _catalog(root):
    entries=read_json(Path(root)/'development/library_v1/clean_raw_templates/catalog.json')
    if not isinstance(entries,list)or len({e['id']for e in entries})!=len(entries):raise ValueError('Unique registered source IDs required')
    return entries

def _clinical_family(root):
    path=Path(root)/'scripts/windows_clinical49_family.py'
    if path.is_symlink()or not path.is_file():raise ValueError('Owned clinical49 adapter unavailable')
    spec=importlib.util.spec_from_file_location('_windows_library_clinical49_family',path)
    module=importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(),str(path),'exec',dont_inherit=True),module.__dict__)
    return module

def _bounded_clinical(entry):
    return (entry['id']=='raw-clinical49'and entry.get('bounded_windows_request_supported')is True
            and entry.get('request_schema')=='prism-clinical49/v1')

def _campaign_path(root):
    current=Path(root)/'runs/windows_full_library_350_20261003/state.json'
    return current if current.is_file()else Path(root)/'runs/windows_to200_campaign_20261003/state.json'

def _campaign(root):
    path=_campaign_path(root)
    if not path.is_file():return {}
    value=read_json(path);entries=value['entries']
    if len({e['id']for e in entries})!=len(entries):raise ValueError('Ambiguous Windows evidence IDs')
    return {e['id']:e for e in entries}

def catalog(query='',*,template_id=None,groups=None,data_profile=None,limit=5,offset=0,windows_recorded_only=True,root=ROOT):
    if type(limit)is not int or not 1<=limit<=20 or type(offset)is not int or offset<0:raise ValueError('Bounded limit1..20 and nonnegative offset required')
    if type(windows_recorded_only)is not bool:raise ValueError('Windows filter must be boolean')
    entries=_catalog(root);recorded=_campaign(root)
    if template_id is not None and sum(e['id']==template_id for e in entries)!=1:raise ValueError('Unknown exact template ID; no fallback')
    chosen=[e for e in entries if e['id']in recorded]if windows_recorded_only else entries
    search=_dev(root,'qualified_catalog').search
    # Filtering precedes pagination; existing exact profile/shape semantics
    # are retained, and a historical Mac-only entry is never called Windows.
    matches=[]
    for entry in chosen:
        if template_id is not None and entry['id']!=template_id:continue
        result=search([entry],query,1,data_profile=data_profile,groups=groups)
        if result['matched']:matches.append(result['entries'][0])
    rows=matches[offset:offset+limit]
    for row in rows:
        row.update(windows_status='recorded_fixture_native_and_ppt'if row['id']in recorded else'windows_pending',
                   fresh_run_required=True,arbitrary_colors_supported=False)
        owner=next(e for e in entries if e['id']==row['id'])
        if _bounded_clinical(owner):
            row.update(routine_new_data_supported=True,request_schema='prism-clinical49/v1',
                       execution_scope='Exactly31 patient columns/12 original clinical rows; new native/PPT proof on every call')
        elif owner.get('windows_receipt_only')is True:
            row.update(routine_new_data_supported=False,request_schema=owner['request_schema'],
                       execution_scope='Recorded exact fixture only; no generic new-data fallback')
    return {'catalog_sources':len(entries),'recorded_windows_native_ppt_sources':len(recorded),
            'matched':len(matches),'returned':len(rows),'entries':rows,
            'proof_scope':'Recorded source-contract fixtures only; fresh native/PPT verification required for new data. Color policy and actual contract are authoritative.',
            'execution_backend':'windows_vm','workflow':'prism_windows_prepare → prism_windows_run',
            'current_runtime_verified':False}

def _entry(root,template_id):
    entries=[e for e in _catalog(root)if e['id']==template_id and e.get('status')=='data_and_titles_verified']
    if len(entries)!=1:raise ValueError('Unique data-contract template ID required; no fallback')
    entry=entries[0];seed=Path(entry['seed'])
    if seed.is_symlink()or not seed.is_file()or hashlib.sha256(seed.read_bytes()).hexdigest()!=entry['seed_sha256']:raise ValueError('Registered seed changed')
    return entry

def _new_output(path,label):
    path=Path(path)
    if not path.is_absolute()or path.exists()or path.is_symlink():raise ValueError(label+' must be a new absolute path')
    return path.resolve()

def _protect_templates(root,path):
    for base in(Path(root)/'assets/templates',Path(root)/'development/library_v1/clean_raw_templates'):
        base=base.resolve()
        if path==base or base in path.parents:raise ValueError('New data outputs must not be written into the original template library')

IMMUTABLE_REQUEST_FIELDS={'source','source_sha256','replacements','graph_settings','statistics',
    'graph_indices','layout_indices','policies','native_format','native_preparation','native_alignment','windows_local_phases'}

def _source_options(example):return {k:v for k,v in example.items()if k not in IMMUTABLE_REQUEST_FIELDS}

def blueprint(template_id,*,root=ROOT):
    entry=_entry(root,template_id);example=read_json(entry['request_example'])
    if _bounded_clinical(entry):
        return {'template_id':template_id,'request_schema':'prism-clinical49/v1',
            'routine_new_data_supported':True,'source_sha256':entry['seed_sha256'],
            'required_package_fields':['schema','data','clinical_codebook_acknowledgement','provenance'],
            'table_slots':{'Table0':{'y_columns':31,'rows':12,'subcolumns':1}},
            'fixed_row_titles':example['replacements']['Table0']['row_titles'],
            'row_allowed_codes':[[1,2,3],[1,2,3],[],[4,5],[6,7,8],[],[9,10,11],[12,13],[],[14,15],[14,15],[14,15]],
            'clinical_codebook_acknowledgement':'clinical49_original_row_codes_and_legend',
            'patient_names_visible':False,'column_order_explicit_in_package':True,
            'fresh_run_required':True,'safe_for_publication':False}
    if entry.get('windows_receipt_only')is True:
        request_path=entry.get('windows_request_example',entry['request_example'])
        example=read_json(request_path)
        return {'template_id':template_id,'request_schema':entry.get('windows_request_schema',entry.get('request_schema')),
                'request_example':request_path,'original_source_sha256':entry['original_sha256'],
                'source_sha256':example.get('source_sha256',entry['seed_sha256']),'required_external_fields':sorted(example),
                'routine_new_data_supported':False,'historical_fixture_only':True,
                'notice':'Only the complete recorded fixture is qualified. New external values, labels or provenance require fresh source-specific native and PPT proof; generic replacement is disabled.'}
    if example['source_sha256']!=entry['seed_sha256']or Path(example['source']).resolve()!=Path(entry['seed']).resolve():raise ValueError('Request example changed source identity')
    tables={}
    for key,value in example['replacements'].items():
        y=value['y']
        tables[key]={'required_fields':sorted(value),'y_columns':len(y),'subcolumns_per_column':[len(c)for c in y],
            'rows_per_subcolumn':[[len(r)for r in c]for c in y],
            'example_null_counts_per_subcolumn':[[sum(v is None for v in r)for r in c]for c in y],
            'x_rows':None if value.get('x')is None else len(value['x']),
            'row_title_count':None if value.get('row_titles')is None else len(value['row_titles']),
            'column_title_count':len(value.get('column_titles',[]))}
    options=_source_options(example)
    result={'template_id':template_id,'source_sha256':entry['seed_sha256'],'request_example':entry['request_example'],
            'table_slots':tables,'graph_fields':{k:sorted(v)for k,v in example['graph_settings'].items()},
            'source_options_required':{k:sorted(v)if isinstance(v,dict)else type(v).__name__ for k,v in options.items()},
            'statistics_required':'statistics'in example,
            'statistics_fields':sorted(example.get('statistics',{})),
            'graph_indices':example['graph_indices'],'layout_indices':example['layout_indices'],
            'color_policy':entry.get('color_policy','source contract required'),
            'capacity_scope':'Example dimensions only. Exact missing masks, capacity, labels, units and method still need source preflight.',
            'preflight_verified':False,'native_executed':False,'safe_for_publication':False}
    if len(json.dumps(result,allow_nan=False).encode('utf-8'))>128*1024:raise ValueError('Input blueprint too large; inspect the exact local request instead')
    return result

def prepare(template_id,replacements,output_request,*,graph_settings=None,statistics=None,source_options=None,root=ROOT):
    entry=_entry(root,template_id);example=read_json(entry['request_example']);request=copy.deepcopy(example)
    if _bounded_clinical(entry):
        if(not isinstance(replacements,dict)or set(replacements)!={'Table0'}or statistics is not None
           or graph_settings not in(None,{})or not isinstance(source_options,dict)
           or set(source_options)!={'clinical_codebook_acknowledgement','provenance'}):
            raise ValueError('Complete categoricalTable0, fixed source labels and explicit clinical/provenance options required')
        package={'schema':'prism-clinical49/v1','data':copy.deepcopy(replacements['Table0']),**copy.deepcopy(source_options)}
        destination=_new_output(output_request,'Request output');_protect_templates(root,destination)
        with tempfile.TemporaryDirectory(prefix='clinical49_preflight_')as temporary:
            _clinical_family(root).prepare_request(package,Path(temporary)/'stage')
        encoded=json.dumps(package,ensure_ascii=False,indent=2,allow_nan=False)
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('x',encoding='utf8')as stream:stream.write(encoded)
        return {'status':'request_preflight_verified','template_id':template_id,'request':str(destination),
            'request_sha256':hashlib.sha256(encoded.encode('utf8')).hexdigest(),
            'native_executed':False,'ppt_created':False,'fresh_run_required':True,'safe_for_publication':False}
    if entry.get('windows_receipt_only')is True:
        raise ValueError('Recorded exact fixture only: new-data preparation requires its extended source-specific adapter; generic fallback is disabled')
    if not isinstance(replacements,dict)or set(replacements)!=set(example['replacements']):raise ValueError('Complete exact source table replacements required')
    request['replacements']=copy.deepcopy(replacements)
    if not isinstance(graph_settings,dict)or set(graph_settings)!=set(example['graph_settings']):raise ValueError('Fresh settings for every original graph required; do not reuse fixture titles/units')
    request['graph_settings']=copy.deepcopy(graph_settings)
    if 'statistics'in example:
        if not isinstance(statistics,dict)or not isinstance(statistics.get('provenance'),str)or not statistics['provenance'].strip():raise ValueError('Fresh explicit statistical method/comparison/provenance acknowledgement required')
        request['statistics']=copy.deepcopy(statistics)
    elif statistics is not None:raise ValueError('Statistics are not registered for this source')
    options=_source_options(example)
    if options:
        if not isinstance(source_options,dict)or set(source_options)!=set(options):raise ValueError('Fresh complete source-specific metadata/options required; do not inherit old captions/images')
        for key,value in options.items():
            current=source_options[key]
            if isinstance(value,dict)and(not isinstance(current,dict)or set(current)!=set(value)):raise ValueError('Complete exact source-specific option fields required: '+key)
            request[key]=copy.deepcopy(current)
    elif source_options is not None:raise ValueError('No additional source-specific options registered')
    if request['source_sha256']!=entry['seed_sha256']or Path(request['source']).resolve()!=Path(entry['seed']).resolve():raise ValueError('Request example is not bound to the registered seed')
    encoded=json.dumps(request,ensure_ascii=False,indent=2,allow_nan=False)
    if len(encoded.encode('utf-8'))>8*1024*1024:raise ValueError('Request exceeds8MiB')
    destination=_new_output(output_request,'Request output')
    _protect_templates(root,destination)
    with tempfile.TemporaryDirectory(prefix='prism_library_preflight_')as temporary:
        staging=Path(temporary);pending=staging/'request.json';pending.write_text(encoded,encoding='utf-8')
        prepared=_dev(root,'recipe_job').prepare_job(pending,staging/'stage')
        plan=read_json(prepared['recipe'])
        if 'statistics'not in request:
            contract=_dev(root,'recipe_workflow').strict_contract(plan)
            if contract['id']!=template_id:raise ValueError('Prepared source contract changed identity')
        elif prepared.get('strict_seed_preflight')!=template_id:raise ValueError('Statistical source preflight changed identity')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x',encoding='utf-8')as stream:stream.write(encoded)
    return {'status':'request_preflight_verified','template_id':template_id,'request':str(destination),
            'source_sha256':request['source_sha256'],'request_sha256':hashlib.sha256(encoded.encode('utf-8')).hexdigest(),
            'native_executed':False,'ppt_created':False,'fresh_run_required':True,'safe_for_publication':False,
            'color_policy':entry.get('color_policy','source contract required')}

def run(request,output,*,make_pptx=True,root=ROOT):
    if type(make_pptx)is not bool:raise ValueError('PPT option must be boolean')
    request=Path(request)
    if not request.is_absolute():raise ValueError('Absolute request path required')
    parsed=read_json(request)
    clinical=parsed.get('schema')=='prism-clinical49/v1'
    entry=_entry(root,'raw-clinical49')if clinical else _entry_from_request(root,parsed)
    if clinical and not _bounded_clinical(entry):raise ValueError('Clinical bounded source contract is not registered')
    output=_new_output(output,'Result output')
    _protect_templates(root,output)
    if request==output or output in request.parents:raise ValueError('Output cannot overwrite or contain the input request')
    import prism_bridge
    policy=prism_bridge.execution_target()
    if policy.get('backend')!='windows_vm'or policy.get('ready')is not True:raise ValueError('Configured Windows-only executor required')
    started=time.monotonic()
    if clinical:
        summary=_clinical_family(root).run(parsed,output)
        summary.update(template_id=entry['id'],native_seconds=time.monotonic()-started,safe_for_publication=False)
        if make_pptx:
            check=_dev(root,'windows_ppt_qualification');before=check.executor.guest_call(policy,'inventory')
            host=output/'ppt-native';receipt=check.ppt.embed(summary['project'],host,'Prism clinical figure',policy=policy,reference_svg=summary['svg'])
            deck=host/'prism_editable.pptx';check.checked_receipt(receipt,summary['project'],deck,reference_svg=summary['svg'])
            summary['ppt']={'deck':str(deck),'delivery_type':'embedded_prism_ole','physical_size_verified':True,
                'click_action_saved_reopened_verified':True,'test_window_cleanup':check.close_new_test_windows(policy,before)}
        summary['total_seconds']=time.monotonic()-started
        return summary
    workflow=_dev(root,'recipe_workflow')
    result=workflow.run_request(request,output,require_native_titles=True)
    native=_dev(root,'windows_qualification')
    if not native.native_pass(result):raise ValueError('Complete new native save/reopen verification required')
    summary=workflow.result_summary(result,output);summary.update(template_id=entry['id'],native_seconds=time.monotonic()-started)
    if make_pptx:
        check=_dev(root,'windows_ppt_qualification');item={'project':result['project'],'verification':str(output/'verification.json')}
        svg=check.bound_reference_svg(item);host=output/'ppt-native'
        receipt=check.ppt.embed(result['project'],host,'Prism figure: '+entry['id'],policy=policy,reference_svg=svg)
        deck=host/'prism_editable.pptx';check.checked_receipt(receipt,result['project'],deck,reference_svg=svg)
        summary['ppt']={'deck':str(deck),'delivery_type':'embedded_prism_ole','physical_size_verified':True,
                        'click_action_saved_reopened_verified':True,'reference_svg':str(svg),'reference_extent':receipt['reference_extent']}
    summary.update(total_seconds=time.monotonic()-started,safe_for_publication=False)
    return summary

def _entry_from_request(root,request):
    if not isinstance(request,dict):raise ValueError('Closed source request object required')
    if any(e.get('windows_receipt_only')is True and e.get('original_sha256')==request.get('source_sha256')
           for e in _catalog(root)):
        raise ValueError('Recorded exact fixture only: new native execution requires fresh extended source-specific proof; generic fallback is disabled')
    matches=[e for e in _catalog(root)if e.get('seed_sha256')==request.get('source_sha256')]
    if len(matches)!=1:raise ValueError('Request needs one exact registered seed; no guessed template')
    entry=_entry(root,matches[0]['id'])
    if Path(request.get('source','')).resolve()!=Path(entry['seed']).resolve():raise ValueError('Request must use the registered seed, not a previous output')
    return entry

def execute(action,args,*,root=ROOT):
    if not isinstance(args,dict):raise ValueError('Arguments object required')
    if action=='catalog':return catalog(root=root,**args)
    if action=='blueprint':return blueprint(root=root,**args)
    if action=='scope':return _dev(root,'library_scope').build_scope(root=root,campaign_path=_campaign_path(root),**args)
    if action=='prepare':return prepare(root=root,**args)
    if action=='run':return run(root=root,**args)
    raise ValueError('Unknown Windows library action')

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('catalog','scope','blueprint','prepare','run'));parser.add_argument('--input',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.input:value=read_json(args.input)
        else:
            raw=sys.stdin.read(8*1024*1024+1)
            if len(raw.encode('utf-8'))>8*1024*1024:raise ValueError('Arguments exceed8MiB')
            value=json.loads(raw,object_pairs_hook=_object,parse_constant=lambda v:(_ for _ in ()).throw(ValueError('Nonfinite JSON value')))
        # Internal workflow messages must not corrupt the one JSON MCP result.
        with contextlib.redirect_stdout(sys.stderr):result=execute(args.action,value)
        print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0
    except (ValueError,OSError,KeyError,TypeError,RecursionError,subprocess.SubprocessError)as error:
        print(str(error),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
