"""Serial native pilot with immutable attempts and conservative evidence gates."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import re
import shutil
import time
import traceback

import batch_template_contract as contract
import batch_template_report as report

SCHEMA_VERSION = 1
DEPENDENCIES = ('batch_template_contract.py', 'batch_template_pilot.py', 'batch_template_report.py',
                'native_inventory.py', 'template_styles.py', 'native_palette.py',
                'prism_bridge.py', 'execution_state.py', 'svg_render.py',
                'windows_prism_executor.py', 'windows_prism_worker.py')


def runner_fingerprint():
    base = Path(__file__).resolve().parent
    return contract.digest({name: contract.sha256(base / name) for name in DEPENDENCIES})


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field: %s' % key)
        result[key] = value
    return result


def read_json(path):
    def reject_constant(value):
        raise ValueError('Nonfinite JSON constant: %s' % value)
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=_unique_pairs, parse_constant=reject_constant)


def _write_json(path, value, exclusive=True):
    with Path(path).open('x' if exclusive else 'w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def prepare_candidate(source, case_id):
    source = Path(source).resolve()
    inspected = contract.inspect_source(source)
    return {'schema_version': 1, 'id': case_id, 'source': str(source),
            'contract': {**{k: inspected[k] for k in ('source_sha256', 'payload_sha256', 'structure_sha256')},
                         'graph_index': 1,
                         'semantics': {'observation': 'unknown', 'value_kind': 'unknown',
                                       'axes': {axis: {'scale': 'unknown', 'range': None, 'evidence': ''} for axis in ('x', 'y')},
                                       'statistics': {'state': 'unknown', 'evidence': ''},
                                       'value_domain': {'range': None, 'evidence': ''}}},
            'data': inspected['data']}


def _load_candidate(path):
    path = Path(path).resolve()
    candidate = read_json(path)
    contract.require_keys(candidate, {'schema_version', 'id', 'source', 'contract', 'data'}, 'candidate')
    if type(candidate['schema_version']) is not int or candidate['schema_version'] != SCHEMA_VERSION:
        raise ValueError('Unsupported candidate schema')
    if not isinstance(candidate['id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', candidate['id']):
        raise ValueError('Case ID must contain 1–80 ASCII letters, digits, underscores or hyphens')
    if not isinstance(candidate['source'], str):
        raise ValueError('Source must be a filesystem path')
    source = Path(candidate['source'])
    if not source.is_absolute():
        source = path.parent / source
    source = source.resolve()
    if source == path:
        raise ValueError('Source and candidate must be different files')
    identity = {'candidate_sha256': contract.sha256(path), 'source_sha256': contract.sha256(source),
                'runner_sha256': runner_fingerprint()}
    return path, source, candidate, identity


def _equivalent_data(left, right):
    left, right = json.loads(json.dumps(left)), json.loads(json.dumps(right))
    for data in (left, right):
        columns = data['y_columns'] + ([data['x_column']] if data['x_column'] else [])
        for col in columns:
            for sub in col['subcolumns']:
                # Decimal construction/equality preserve all input digits;
                # normalize() would round under the active decimal context.
                sub['values'] = [None if v is None else contract.number(v) for v in sub['values']]
    return left == right


def _check_saved(project, data):
    inspected = contract.inspect_source(project)
    if not inspected['supported'] or not _equivalent_data(inspected['data'], data):
        raise ValueError('Saved native project data/structure did not match candidate: %s' % inspected['rejection_reasons'])


def _native_export(project, directory, basename, **kwargs):
    import prism_bridge
    return prism_bridge.run_prism_export_staged(project, directory, basename, **kwargs)


def _check_svg(path):
    from xml.etree import ElementTree as ET
    root = ET.parse(path).getroot()
    if root.tag.rsplit('}', 1)[-1] != 'svg' or not list(root):
        raise ValueError('Missing or empty native SVG')


def _perform_attempt(candidate_path, source, candidate, identity, attempt, output, run_prism, exporter):
    gates = {name: None for name in ('structural_check', 'data_mapping_check', 'semantic_check', 'native_render_check', 'style_check', 'reopen_check', 'geometry_check')}
    gates.update(review_check=False, native_payload_bytes_preserved_pre_prism=None, source_unchanged=None,
                 saved_data_check=None, reopened_data_check=None, pre_reopen_copy_unchanged=None)
    result = {'schema_version': 1, 'id': candidate['id'], 'identity': identity, 'attempt_dir': str(attempt),
              'status': 'validating', 'gates': gates, 'calls': [], 'previews': {}, 'artifacts': {},
              'review_required': ['Validate each series binding and geometry against sentinel data.',
                                  'Review native graph type, color, font and line-width inheritance.',
                                  'Check statistics, annotations, labels, units, axes, clipping and overlap.',
                                  'Review source/new/reopened images and define supported input domain.',
                                  'Record independent reviewer identity, per-gate evidence and final approval outside this runner.']}
    started = time.monotonic()
    native_started = False
    try:
        inspected = contract.inspect_source(source)
        _write_json(attempt / 'inspection.json', inspected)
        contract.validate_contract(inspected, candidate['contract'], candidate['data'])
        gates['structural_check'] = True
        _write_json(attempt / 'candidate.json', candidate)
        patch = contract.patch_candidate(source, candidate['data'], attempt / 'patched-input.pzfx', candidate['contract'])
        gates['data_mapping_check'] = patch['data_mapping_check']
        gates['native_payload_bytes_preserved_pre_prism'] = patch['native_payload_bytes_preserved_pre_prism']
        if not run_prism:
            result['status'] = 'ready_for_native'
        else:
            shutil.copy2(source, attempt / 'reference.pzfx')
            shutil.copy2(attempt / 'patched-input.pzfx', attempt / 'new.pzfx')
            patched_hash = contract.sha256(attempt / 'patched-input.pzfx')
            new_saved_hash = None
            for stage in ('reference', 'new', 'reopened'):
                if stage == 'reopened':
                    new_saved_hash = contract.sha256(attempt / 'new.pzfx')
                    result['pre_reopen_sha256'] = new_saved_hash
                    shutil.copy2(attempt / 'new.pzfx', attempt / 'reopened.pzfx')
                project = attempt / (stage + '.pzfx')
                if contract.sha256(source) != identity['source_sha256'] or contract.sha256(candidate_path) != identity['candidate_sha256']:
                    raise ValueError('Input/source changed during attempt')
                call = {'stage': stage, 'project': str(project), 'started_unix': time.time(), 'ok': False}
                result['calls'].append(call)
                call_started = time.monotonic()
                native_started = True
                try:
                    ok, message, svg = exporter(project, attempt, stage, clean_metadata=False,
                                                graph_index=candidate['contract']['graph_index'],
                                                title_mode='native', keep_prism_warm=False)
                    call.update(ok=bool(ok), message=str(message), svg=str(svg))
                    svg = Path(svg).resolve()
                    if svg != (attempt / (stage + '.svg')).resolve():
                        raise ValueError('Native exporter returned unexpected output path')
                    (attempt / (stage + '_call.log')).write_text(str(message), encoding='utf-8')
                    if not ok:
                        raise RuntimeError('Native %s export failed: %s' % (stage, message))
                    _check_svg(svg)
                    _check_saved(project, inspected['data'] if stage == 'reference' else candidate['data'])
                    result['previews'][stage] = {'svg': str(svg.relative_to(output))}
                    try:
                        import svg_render
                        png = attempt / (stage + '.png')
                        svg_render.render(svg, png)
                        result['previews'][stage]['png'] = str(png.relative_to(output))
                    except Exception as exc:
                        call['preview_error'] = str(exc)
                    if stage == 'new':
                        gates['saved_data_check'] = True
                    if stage == 'reopened':
                        gates['reopened_data_check'] = True
                        gates['pre_reopen_copy_unchanged'] = contract.sha256(attempt / 'new.pzfx') == new_saved_hash
                        if not gates['pre_reopen_copy_unchanged']:
                            raise ValueError('Pre-reopen saved project changed')
                    if contract.sha256(attempt / 'patched-input.pzfx') != patched_hash:
                        raise ValueError('Immutable pre-Prism patched input changed')
                except Exception as exc:
                    call.update(ok=False, error=str(exc))
                    (attempt / (stage + '_exception.log')).write_text(traceback.format_exc(), encoding='utf-8')
                    raise
                finally:
                    call['duration_seconds'] = round(time.monotonic() - call_started, 6)
            gates['native_render_check'] = True
            # Reopen gate is narrowly mechanical; matching rendered geometry remains unknown.
            gates['reopen_check'] = True
            result['status'] = 'review_required'
    except Exception as exc:
        result['status'] = 'failed' if native_started else 'blocked'
        result['error'] = str(exc)
        if gates['structural_check'] is None:
            gates['structural_check'] = False
        if native_started:
            gates['native_render_check'] = False
        (attempt / 'attempt_exception.log').write_text(traceback.format_exc(), encoding='utf-8')
    finally:
        gates['source_unchanged'] = source.is_file() and contract.sha256(source) == identity['source_sha256']
        if not gates['source_unchanged']:
            result['status'] = 'failed'
            result['error'] = 'Source fingerprint changed during attempt'
        result['duration_seconds'] = round(time.monotonic() - started, 6)
        for path in sorted(attempt.iterdir()):
            if path.is_file():
                result['artifacts'][str(path.relative_to(output))] = contract.sha256(path)
        _write_json(attempt / 'result.json', result)
    return result


def _validate_previous(case, output):
    attempt = Path(case['attempt_dir']).resolve()
    if output not in attempt.parents:
        raise ValueError('Attempt is outside run directory')
    result_path = attempt / 'result.json'
    if not result_path.is_file() or read_json(result_path) != case:
        raise ValueError('Missing/tampered immutable attempt result')
    for name, expected in case.get('artifacts', {}).items():
        path = (output / name).resolve()
        if output not in path.parents or not path.is_file() or contract.sha256(path) != expected:
            raise ValueError('Missing/tampered attempt output: %s' % name)


def _persist_manifest(output, manifest):
    # This ledger/report is mutable; attempt directories never are.
    temporary = output / '.manifest.pending.json'
    _write_json(temporary, manifest)
    temporary.replace(output / 'manifest.json')
    report.write_report(manifest, output / 'report.html')


def run_candidates(candidate_paths, outdir, *, run_prism=False, resume=False, retry=False, exporter=None):
    if resume and retry:
        raise ValueError('Choose resume or retry, not both')
    loaded = [_load_candidate(p) for p in candidate_paths]
    if not loaded or len({item[2]['id'] for item in loaded}) != len(loaded):
        raise ValueError('At least one candidate and unique case IDs required')
    output = Path(outdir).absolute()
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ValueError('Output must be a dedicated directory, not an existing file/symlink')
    output = output.resolve()
    for reserved in ('manifest.json', 'report.html', '.batch.lock', '.manifest.pending.json'):
        path = output / reserved
        if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
            raise ValueError('Reserved run path is a symlink/hardlink; refusing overwrite')
    if any(output == p or output in p.parents for item in loaded for p in item[:2]):
        raise ValueError('Inputs and sources must be outside the run output directory')
    manifest_path = output / 'manifest.json'
    if output.exists() and not manifest_path.exists() and any(output.iterdir()):
        raise ValueError('Refusing to overwrite an unowned output directory')
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.batch.lock').open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another batch owns this run directory') from exc
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest.get('schema_version') != 1 or manifest.get('kind') != 'prism_batch_pilot':
                raise ValueError('Unrecognized manifest; refusing overwrite')
            if not (resume or retry):
                raise ValueError('Existing run requires --resume or --retry')
        else:
            manifest = {'schema_version': 1, 'kind': 'prism_batch_pilot', 'cases': [], 'history': []}
        if manifest.get('native_halted') and run_prism and not retry:
            raise ValueError('Previous native failure halted this run; resolve Prism state and use explicit --retry')
        if retry:
            manifest['native_halted'] = False
            manifest['deferred'] = []
        for index, (candidate_path, source, candidate, identity) in enumerate(loaded):
            previous = [c for c in manifest['history'] if c['id'] == candidate['id']]
            for case in previous:
                if case['identity'] != identity:
                    raise ValueError('Candidate/source/runner changed; previous case is invalidated. Use a new run directory.')
                _validate_previous(case, output)
            if previous and resume:
                if run_prism and not previous[-1]['calls'] and previous[-1]['status'] == 'ready_for_native':
                    raise ValueError('Prepared-only attempt requires --retry to execute native work')
                continue
            if len(previous) >= 2:
                raise ValueError('Two-attempt ceiling reached; escalate this immutable case')
            case_dir = output / candidate['id']
            if case_dir.is_symlink():
                raise ValueError('Case directory must not be a symlink')
            case_dir.mkdir(exist_ok=True)
            attempt = case_dir / ('attempt-%02d' % (len(previous) + 1))
            attempt.mkdir(exist_ok=False)
            result = _perform_attempt(candidate_path, source, candidate, identity, attempt, output, run_prism, exporter or _native_export)
            manifest['history'].append(result)
            manifest['cases'] = [c for c in manifest['cases'] if c['id'] != candidate['id']] + [result]
            if result['status'] == 'failed' and result['calls']:
                manifest['native_halted'] = True
                manifest['deferred'] = [item[2]['id'] for item in loaded[index + 1:]]
                for _, _, remaining, remaining_identity in loaded[index + 1:]:
                    if not any(c['id'] == remaining['id'] for c in manifest['cases']):
                        manifest['cases'].append({'schema_version': 1, 'id': remaining['id'],
                                                  'identity': remaining_identity, 'status': 'deferred',
                                                  'calls': [], 'gates': {'review_check': False},
                                                  'error': 'Native submissions halted after failure in %s' % candidate['id']})
            _persist_manifest(output, manifest)
            if manifest.get('native_halted'):
                break
        return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--source', type=Path, required=True)
    prepare = commands.add_parser('prepare')
    prepare.add_argument('--source', type=Path, required=True)
    prepare.add_argument('--id', required=True)
    prepare.add_argument('--out', type=Path, required=True)
    run = commands.add_parser('run')
    run.add_argument('--candidate', action='append', type=Path, required=True)
    run.add_argument('--outdir', type=Path, required=True)
    run.add_argument('--run-prism', action='store_true')
    mode = run.add_mutually_exclusive_group()
    mode.add_argument('--resume', action='store_true')
    mode.add_argument('--retry', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            result = contract.inspect_source(args.source)
        elif args.command == 'prepare':
            if args.out.resolve() == args.source.resolve() or args.out.exists() or args.out.is_symlink():
                raise ValueError('Draft output must be a new file, never the source')
            result = prepare_candidate(args.source, args.id)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.out, result)
        else:
            result = run_candidates(args.candidate, args.outdir, run_prism=args.run_prism, resume=args.resume, retry=args.retry)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, 'Batch pilot rejected: %s\n' % exc)


if __name__ == '__main__':
    main()
