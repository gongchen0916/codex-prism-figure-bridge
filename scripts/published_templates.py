"""Curated fixed-shape templates with exact data and bounded native readback.

The registry is a local release ledger, not a signature or an authorization API.
Tool handlers must resolve entries through this module's fixed registry path;
the explicit-entry Python interface also permits isolated release acceptance.
"""
from __future__ import annotations

import copy
import csv
from decimal import localcontext
import io
import json
import math
from pathlib import Path
import re
import shutil
import unicodedata
from xml.etree import ElementTree as ET

import batch_template_contract as contract
import batch_template_pilot as pilot

ADAPTER_VERSION = 1
EXECUTION_FILES = ('published_templates.py', 'batch_template_contract.py', 'batch_template_pilot.py',
                   'prism_bridge.py', 'svg_render.py', 'native_inventory.py',
                   'template_styles.py', 'native_palette.py', 'prism_mcp_server.py',
                   'template_matcher.py', 'template_identity.py', 'template_gallery.py',
                   'runtime_state.py', 'execution_state.py', 'batch_template_report.py',
                   'windows_prism_executor.py', 'windows_prism_worker.py')
REGISTRY = Path('assets/templates/published_templates.json')
_HASH = re.compile(r'[0-9a-f]{64}')
_OPTIONS = {'palette', 'group_colors', 'graph_index', 'title_mode', 'close_prism',
            'auto_axis', 'graph_title', 'title', 'x_axis_title', 'y_axis_title',
            'run_prism', 'timeout', 'make_pptx', 'return_image_data', 'first_row_is_data', 'hint'}


def implementation_fingerprint() -> str:
    """Hash actual executing-install script bytes, independent of install path.

    The canonical JSON mapping of dependency basename to SHA256 is hashed with
    batch_template_contract.digest. Tests, records and absolute root paths are
    excluded. MCP routing is included because it selects the published adapter.
    Every lookup recomputes this fingerprint; no caller root can substitute code.
    """
    directory = Path(__file__).resolve().parent
    return contract.digest({filename: contract.sha256(directory / filename) for filename in EXECUTION_FILES})


def _plain(value, label, limit=80, empty=False):
    if (not isinstance(value, str) or len(value) > limit or
            (not empty and not value.strip()) or value != value.strip() or
            any(unicodedata.category(c).startswith('C') or unicodedata.category(c) in {'Zl', 'Zp'}
                or c in '\r\n' for c in value)):
        raise ValueError('%s must be plain text of at most %d characters without control characters' % (label, limit))
    return value


def _contained(root, relative, under=None):
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError('Release paths must be relative paths within the root')
    if '..' in Path(relative).parts:
        raise ValueError('Release path traversal is unsupported')
    root = Path(root).resolve()
    base = (root / under).resolve() if under else root
    path = (root / relative).resolve()
    if base not in path.parents or not path.is_file():
        raise ValueError('Missing release file or path outside permitted directory: %s' % relative)
    return path


def _hash_file(path, expected, label, hashes=None):
    if not isinstance(expected, str) or not _HASH.fullmatch(expected):
        raise ValueError('%s fingerprint mismatch' % label)
    if hashes is None:
        actual = contract.sha256(path)
    else:
        if path not in hashes:
            hashes[path] = contract.sha256(path)
        actual = hashes[path]
    if actual != expected:
        raise ValueError('%s fingerprint mismatch' % label)


def _range(value, label):
    if (not isinstance(value, list) or len(value) != 2 or
            any(type(v) not in (int, float) for v in value)):
        raise ValueError('%s requires two finite numeric bounds' % label)
    bounds = [contract.number(str(v)) for v in value]
    if bounds[0] >= bounds[1]:
        raise ValueError('%s requires increasing bounds' % label)
    return value


def _entry(root, entry, execution_sha=None, hashes=None):
    required = {'alias', 'name', 'aliases', 'kind', 'seed', 'seed_sha256', 'capabilities', 'approval'}
    if not isinstance(entry, dict) or not required <= set(entry):
        raise ValueError('Published entry is missing required release fields')
    _plain(entry['alias'], 'alias')
    _plain(entry['name'], 'name', 160)
    if not isinstance(entry['aliases'], list):
        raise ValueError('aliases must be a list of exact plain phrases')
    for alias in entry['aliases']:
        _plain(alias, 'alias', 160)
    if entry['kind'] not in {'stacked_xy', 'grouped_sem', 'mean_heatmap'}:
        raise ValueError('Unsupported published template kind')
    source = _contained(root, entry['seed'], 'assets/templates')
    _hash_file(source, entry['seed_sha256'], 'Seed', hashes)
    approval = entry['approval']
    if not isinstance(approval, dict) or not {'reviewer', 'reviewed_at', 'adapter_version', 'execution_sha256', 'evidence'} <= set(approval):
        raise ValueError('Missing curated release approval')
    _plain(approval['reviewer'], 'reviewer', 160)
    _plain(approval['reviewed_at'], 'reviewed_at', 80)
    if type(approval['adapter_version']) is not int or approval['adapter_version'] != ADAPTER_VERSION:
        raise ValueError('Unsupported approved adapter version')
    execution = approval['execution_sha256']
    if execution_sha is None:
        execution_sha = implementation_fingerprint()
    if not isinstance(execution, str) or not _HASH.fullmatch(execution) or execution != execution_sha:
        raise ValueError('Release execution fingerprint mismatch; current code requires renewed acceptance')
    evidence = approval['evidence']
    if not isinstance(evidence, list) or len(evidence) < 2:
        raise ValueError('At least two immutable release evidence files are required')
    paths = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('Evidence requires path and sha256')
        path = _contained(root, item['path'])
        if path in paths or path == source or path == (Path(root) / REGISTRY).resolve():
            raise ValueError('Release evidence must reference distinct independent files')
        paths.add(path)
        _hash_file(path, item['sha256'], 'Evidence', hashes)
    caps = entry['capabilities']
    keys = {'series_count', 'row_count', 'replicates', 'x_values', 'value_domain',
            'sum_max', 'x_axis_title', 'y_axis_title', 'graph_title', 'max_label_chars', 'exact_hex_colors'}
    if not isinstance(caps, dict) or not keys <= set(caps):
        raise ValueError('Missing published capacity fields')
    for key in ('series_count', 'row_count', 'replicates', 'max_label_chars'):
        if type(caps[key]) is not int or caps[key] < 1:
            raise ValueError('Capacity %s must be a positive integer' % key)
    if caps['exact_hex_colors'] is not False or type(caps['graph_title']) is not bool:
        raise ValueError('Only inherited colors and explicit graph-title capability are supported')
    _range(caps['value_domain'], 'value_domain')
    for key in ('x_axis_title', 'y_axis_title'):
        _plain(caps[key], key, caps['max_label_chars'], empty=True)
    if entry['kind'] == 'mean_heatmap':
        fixed = {'aggregation': 'mean', 'color_scale_mode': 'auto',
                 'category_axes': True, 'colorbar_label': 'Mean value',
                 'x_axis_title': '', 'y_axis_title': ''}
        for key, expected in fixed.items():
            if key not in caps or type(caps[key]) is not type(expected) or caps[key] != expected:
                raise ValueError('Mean heatmap capability %s must be %r' % (key, expected))
        if any(caps.get(key) is not None for key in ('x_axis_range', 'y_axis_range')):
            raise ValueError('Mean heatmap categorical axes do not accept numeric axis ranges')
    if 'x_axis_range' in caps and caps['x_axis_range'] is not None:
        _range(caps['x_axis_range'], 'x_axis_range')
    if 'y_axis_range' in caps:
        _range(caps['y_axis_range'], 'y_axis_range')
    inspected = contract.inspect_source(source)
    if not inspected['supported']:
        raise ValueError('Unsupported published seed: ' + '; '.join(inspected['rejection_reasons']))
    data = inspected['data']
    if (len(data['y_columns']) != caps['series_count'] or
            any(len(c['subcolumns']) != caps['replicates'] or
                any(len(s['values']) != caps['row_count'] for s in c['subcolumns'])
                for c in data['y_columns'])):
        raise ValueError('Seed structure does not match approved fixed capacity')
    if entry['kind'] == 'stacked_xy':
        if data['table_type'] != 'XY' or caps['replicates'] != 1:
            raise ValueError('Stacked release requires a single-value XY seed')
        xvalues = caps['x_values']
        if not isinstance(xvalues, list) or len(xvalues) != caps['row_count']:
            raise ValueError('Stacked release requires every approved X value')
        expected = [contract.number(str(v)) for v in xvalues]
        actual = [contract.number(v) for v in data['x_column']['subcolumns'][0]['values']]
        if expected != actual or min(expected) >= max(expected):
            raise ValueError('Seed X values differ from approved fixed X values')
        if (type(caps['sum_max']) not in (int, float) or
                contract.number(str(caps['sum_max'])) <= 0 or
                contract.number(str(caps['value_domain'][0])) < 0):
            raise ValueError('Stacked release requires a positive sum_max and nonnegative domain')
    elif (data['table_type'] != 'TwoWay' or data['row_titles'] is None or
          caps['x_values'] is not None or caps['sum_max'] is not None or caps['replicates'] < 2):
        raise ValueError('Grouped/heatmap release requires raw TwoWay replicates and row titles')
    return source, inspected


def _registry_entries(root: Path) -> list[dict]:
    """Read identities first, without validating unrelated execution evidence."""
    path = Path(root).resolve() / REGISTRY
    if not path.exists():
        return []
    registry = pilot.read_json(path)
    contract.require_keys(registry, {'version', 'templates'}, 'published registry')
    if type(registry['version']) is not int or registry['version'] != 1 or not isinstance(registry['templates'], list):
        raise ValueError('Unsupported published registry schema')
    entries, identifiers = [], set()
    for entry in registry['templates']:
        if not isinstance(entry, dict):
            raise ValueError('Registry templates must be objects')
        if entry.get('status') != 'published':
            continue
        _plain(entry.get('alias'), 'alias')
        _plain(entry.get('name'), 'name', 160)
        if not isinstance(entry.get('aliases'), list):
            raise ValueError('aliases must be a list of exact plain phrases')
        for alias in entry['aliases']:
            _plain(alias, 'alias', 160)
        names = {entry['alias'], entry['name'], *entry['aliases']}
        if identifiers & names:
            raise ValueError('Duplicate published alias/name: %s' % sorted(identifiers & names))
        identifiers.update(names)
        entries.append(copy.deepcopy(entry))
    return entries


def list_published_identities(root: Path) -> list[dict]:
    """Unvalidated catalog identities, never entries approved for drawing."""
    return [{'alias': entry['alias'], 'name': entry['name'], 'source_name': entry['name'],
             'aliases': list(entry['aliases'])}
            for entry in _registry_entries(root)]


def list_published(root: Path, identifiers=None) -> list[dict]:
    """Revalidate each request, sharing hashes only during this one lookup."""
    entries = _registry_entries(root)
    if identifiers is not None:
        selected = set(identifiers)
        entries = [entry for entry in entries if entry['alias'] in selected]
    if entries:
        execution_sha, hashes = implementation_fingerprint(), {}
        for entry in entries:
            _entry(root, entry, execution_sha, hashes)
    return entries


def resolve_published(root: Path, identifier: str) -> dict | None:
    if not isinstance(identifier, str):
        raise ValueError('Template identifier must be an exact alias, name or phrase')
    for entry in _registry_entries(root):
        if identifier in {entry['alias'], entry['name'], *entry['aliases']}:
            _entry(root, entry)
            return entry
    return None


def _check_hint(kind, hint):
    """Reject explicit incompatible requests without deriving new statistics.

    Narrow negated lists ("not SD or CI") describe absence. Arbitrary prose is
    not interpreted as a statistical model, and adversative clauses remain
    independently checked ("not SEM but SD" still rejects).
    """
    text = unicodedata.normalize('NFKC', hint).casefold()
    if kind == 'mean_heatmap':
        # Only established absence phrases are removed. An adversative clause
        # such as "no normalization but median" is checked independently.
        absence = r'(?:normali[sz]ation|standardi[sz]ation|z[ -]?scores?|paired|pairing|inference)'
        text = re.sub(r'\b(?:not|no|without)\s+' + absence +
                      r'(?:\s*(?:or|and)\s*' + absence + r')*\b', ' ', text)
        text = re.sub(r'\b(?:unpaired|non[ -]?paired)\b', ' ', text)
        text = re.sub(r'(?:非|不做|不需要|无需|不要|不|无)(?:配对|归一化|标准化|推断)', ' ', text)
        # Adjacent numeric bounds identify a requested scale. Do not scan
        # arbitrary following prose: it may describe the raw input domain.
        numeric = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?'
        scale = r'(?:colo[u]?r[ -]*(?:bar|scale|limits?|range)|色标|色条|色阶|颜色范围)'
        scale_bounds = re.search(
            scale + r'\s*(?:(?:limits?|range|bounds?|范围|上下限)\s*)?'
            r'(?:(?:set\s+(?:to|at)|is|are|to|from|设为|设置为|设置成|为|从)\s*)?'
            r'[:=：]?\s*[\[(]?\s*' + numeric + r'\s*(?:to|through|[–—−,，-]|到|至)\s*' + numeric,
            text)
        if scale_bounds:
            raise ValueError('Explicit numeric color-scale bounds are incompatible with automatic mean heatmap: %s' %
                             scale_bounds.group(0))
        conflict = re.search(
            r'\b(?:median|minimum|maximum|min|max|sum|summed|summation|'
            r'normali[sz](?:e[ds]?|ing|ation)|standardi[sz](?:e[ds]?|ing|ation)|'
            r'z[ -]?scores?|paired|pairing|repeated[ -]*measures?|inference|inferential|'
            r'anova|t[ -]?test|p[ -]?values?|significan(?:ce|t)|hypothesis[ -]+test(?:ing|s)?)\b'
            r'|\b(?:fixed|manual(?:ly)?|specified)\b[^,;.]*\b(?:colo[u]?r[ -]*(?:bar|scale|limits?|range)|scale)\b'
            r'|\b(?:colo[u]?r[ -]*(?:bar|scale|limits?|range))\b[^,;.]*\b(?:fixed|manual(?:ly)?|specified)\b'
            r'|\b(?:bar|line)[ -]*(?:plot|chart|graph)s?\b'
            r'|中位数|最小值|最大值|求和|总和|归一化|标准化|配对|重复测量|重复测定|'
            r'统计推断|显著性|假设检验|方差分析|(?:固定|手动).{0,8}(?:色标|色条|颜色|色阶)|'
            r'(?:色标|色条|色阶).{0,8}(?:固定|手动)|柱状图|柱形图|折线图', text)
        if conflict:
            raise ValueError('Hint is incompatible with arithmetic mean heatmap of independent raw replicates '
                             'and automatic color scale: %s' % conflict.group(0))
        return
    statistical = r'(?:standard[ -]*deviations?|stdev|sd|confidence[ -]*intervals?|ci|paired|pairing|repeated[ -]*measures?)'
    transforms = r'(?:normali[sz]ation|fitting|curve[ -]*fitting|regression|negative(?:[ -]+values?)?)'
    absent = '(?:' + statistical + '|' + transforms + ')'
    text = re.sub(r'\b(?:not|no|without)\s+' + absent + r'(?:\s*(?:or|and)\s*' + absent + r')*\b', ' ', text)
    text = re.sub(r'\b(?:unpaired|non[ -]?negative)\b', ' ', text)
    chinese_terms = r'(?:标准差|置信区间|配对|重复测量|重复测定|归一化|标准化|拟合(?:曲线)?|负值|负数)'
    text = re.sub(r'(?:非|不做|不需要|无需|不要|不|无)' + chinese_terms, ' ', text)
    if kind == 'grouped_sem':
        conflict = re.search(r'(?<![a-z])' + statistical + r'(?![a-z])|标准差|置信区间|配对|重复测量|重复测定', text)
        meaning = 'grouped mean ± sample SEM with independent raw replicates'
    else:
        conflict = re.search(r'\b(?:normali[sz](?:e[ds]?|ing|ation)|fitt?(?:ed|ing)?|regression|negative)\b'
                             r'|100\s*%|\bpercent(?:age)?[ -]+stack|归一化|标准化|百分比|拟合|负值|负数'
                             r'|\b(?:stacked(?:[ -]+xy)?[ -]+(?:filled[ -]+)?area|filled[ -]+area|area[ -]+(?:plot|chart|graph))\b'
                             r'|(?:堆叠|堆积)?面积图', text)
        meaning = 'raw nonnegative stacked XY bars with fixed X and no normalization or fitting'
    if conflict:
        raise ValueError('Hint is incompatible with this published template (%s): %s' % (meaning, conflict.group(0)))


def _options(entry, options):
    if not isinstance(options, dict) or set(options) - _OPTIONS:
        raise ValueError('Unsupported published style options: %s' % sorted(set(options) - _OPTIONS) if isinstance(options, dict)
                         else 'Published options must be an object')
    result = dict(options)
    result.setdefault('hint', entry['name'])
    _plain(result['hint'], 'hint', 2000)
    _check_hint(entry['kind'], result['hint'])
    fixed = {'palette': 'inherit', 'graph_index': 1, 'title_mode': 'native',
             'close_prism': True, 'auto_axis': False, 'make_pptx': False}
    for key, expected in fixed.items():
        if key in result and (type(result[key]) is not type(expected) or result[key] != expected):
            raise ValueError('%s=%r is unsupported; published templates require %r' % (key, result[key], expected))
        result[key] = expected
    if 'group_colors' in result:
        raise ValueError('group_colors is unsupported; published templates inherit seed palette slots')
    header_mode = result.get('first_row_is_data')
    if header_mode is not None and header_mode is not False:
        raise ValueError('Published templates require a header row')
    for key in ('run_prism', 'return_image_data'):
        if key in result and type(result[key]) is not bool:
            raise ValueError('%s must be boolean' % key)
    result.setdefault('run_prism', True)
    if 'timeout' in result and (type(result['timeout']) is not int or not 1 <= result['timeout'] <= 600):
        raise ValueError('timeout must be an integer from 1 to 600 seconds')
    result.setdefault('timeout', 120)
    caps = entry['capabilities']
    if 'title' in result and 'graph_title' in result and result['title'] != result['graph_title']:
        raise ValueError('Conflicting graph title options')
    if 'title' in result:
        result['graph_title'] = result.pop('title')
    if 'graph_title' in result:
        if not caps['graph_title']:
            raise ValueError('This published seed does not support graph-title changes')
        _plain(result['graph_title'], 'graph_title', caps['max_label_chars'])
    for key in ('x_axis_title', 'y_axis_title'):
        value = result.get(key, caps[key])
        heatmap = entry['kind'] == 'mean_heatmap'
        _plain(value, key, caps['max_label_chars'], empty=heatmap or key not in result)
        if heatmap and value:
            raise ValueError('Mean heatmap categorical axis titles cannot be overridden')
        result[key] = value
    return result


def _read_rows(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    if not text.strip():
        raise ValueError('A nonempty headered CSV/TSV table is required')
    delimiter = '\t' if '\t' in text.splitlines()[0] else ','
    try:
        rows = list(csv.reader(io.StringIO(text, newline=''), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise ValueError('Invalid CSV/TSV data: %s' % exc) from exc
    if not rows or any(not row or len(row) != len(rows[0]) for row in rows):
        raise ValueError('CSV/TSV rows must have the exact header width, without blank rows')
    return rows


def _natural(value):
    # Typed tokens avoid comparisons between numbers and strings. The original
    # label breaks case/leading-zero ties, making record order irrelevant.
    return ([(1, int(t)) if t.isdigit() else (0, t.casefold()) for t in re.split(r'(\d+)', value)], value)


def _candidate(root, entry, data_path, options):
    source, inspected = _entry(root, entry)
    style = _options(entry, options)
    caps = entry['capabilities']
    rows = _read_rows(data_path)
    data = copy.deepcopy(inspected['data'])
    heatmap = entry['kind'] == 'mean_heatmap'
    mean_metadata = {}
    low, high = [contract.number(str(v)) for v in caps['value_domain']]

    def value(cell):
        numeric = contract.number(cell)
        if not low <= numeric <= high:
            raise ValueError('Raw value outside approved value_domain %s' % caps['value_domain'])
        return numeric

    if entry['kind'] == 'stacked_xy':
        if rows[0][0] != 'X' or len(rows[0]) != caps['series_count'] + 1 or len(rows) != caps['row_count'] + 1:
            raise ValueError('Expected stacked CSV: X plus exactly %d series headers and %d data rows with original X values' %
                             (caps['series_count'], caps['row_count']))
        headers = rows[0][1:]
        for header in headers:
            _plain(header, 'series header', caps['max_label_chars'])
        if len(set(rows[0])) != len(rows[0]):
            raise ValueError('Stacked headers must be unique')
        expected = [contract.number(str(v)) for v in caps['x_values']]
        if [contract.number(row[0]) for row in rows[1:]] != expected:
            raise ValueError('Stacked X must exactly match the approved original X values in order')
        for row in rows[1:]:
            values = [value(cell) for cell in row[1:]]
            # Decimal arithmetic otherwise inherits a 28-digit context and can
            # round a tiny excess back down to the permitted maximum.
            with localcontext() as context:
                smallest = min(n.as_tuple().exponent for n in values)
                largest = max(n.adjusted() for n in values)
                context.prec = max(28, largest - smallest + len(str(len(values))) + 3)
                if sum(values) > contract.number(str(caps['sum_max'])):
                    raise ValueError('Stacked row sum exceeds approved sum_max %s' % caps['sum_max'])
        data['x_column']['subcolumns'][0]['values'] = [row[0] for row in rows[1:]]
        for i, col in enumerate(data['y_columns']):
            col['title'] = headers[i]
            col['subcolumns'][0]['values'] = [row[i + 1] for row in rows[1:]]
    else:
        if rows[0] != ['row', 'group', 'replicate', 'value']:
            raise ValueError('Expected grouped raw-data CSV headers exactly: row,group,replicate,value')
        records = {}
        for row, group, replicate, cell in rows[1:]:
            for label in (row, group, replicate):
                _plain(label, 'row/group/replicate label', caps['max_label_chars'])
            identity = (row, group, replicate)
            if identity in records:
                raise ValueError('Duplicate row/group/replicate identity: %r' % (identity,))
            value(cell)
            records[identity] = cell
        row_labels = sorted({r for r, _, _ in records}, key=_natural)
        groups = sorted({g for _, g, _ in records}, key=_natural)
        if len(row_labels) != caps['row_count'] or len(groups) != caps['series_count']:
            raise ValueError('Expected exactly %d distinct rows and %d distinct groups' % (caps['row_count'], caps['series_count']))
        data['row_titles']['subcolumns'][0]['values'] = row_labels
        for col, group in zip(data['y_columns'], groups):
            col['title'] = group
            for row_index, row in enumerate(row_labels):
                replicas = sorted([rep for r, g, rep in records if r == row and g == group], key=_natural)
                if len(replicas) != caps['replicates']:
                    raise ValueError('Every row/group requires exactly %d distinct raw replicate IDs' % caps['replicates'])
                for sub, rep in zip(col['subcolumns'], replicas):
                    sub['values'][row_index] = records[(row, group, rep)]
        if heatmap:
            numbers = [contract.number(v) for v in records.values()]
            with localcontext() as context:
                # Enough precision for exact sums, followed by guarded mean
                # division. Compare sums before division since counts match.
                smallest = min(n.as_tuple().exponent for n in numbers)
                largest = max(n.adjusted() for n in numbers)
                context.prec = max(50, largest - smallest + len(str(caps['replicates'])) + 10)
                sums = [sum(contract.number(sub['values'][i]) for sub in col['subcolumns'])
                        for col in data['y_columns'] for i in range(caps['row_count'])]
                if min(sums) == max(sums):
                    raise ValueError('Automatic mean heatmap requires at least two distinct cell means; '
                                     'constant mean color expansion is not calibrated')
                bounds = [v / caps['replicates'] for v in (min(sums), max(sums))]
                color_domain = [float(v) for v in bounds]
                if not all(math.isfinite(v) for v in color_domain):
                    raise ValueError('Mean heatmap color domain must have finite JSON numeric bounds')
                if color_domain[0] >= color_domain[1]:
                    raise ValueError('Distinct Decimal mean endpoints collapse as native numeric color bounds; '
                                     'automatic constant-range expansion is not calibrated')
                mean_metadata = {'aggregation': 'mean', 'color_scale_mode': 'auto',
                                 'input_domain': copy.deepcopy(caps['value_domain']),
                                 'color_domain': color_domain,
                                 'color_domain_decimal': [str(v) for v in bounds]}
    evidence = 'Curated release %s; rehashed seed and review evidence; adapter v%d' % (entry['alias'], ADAPTER_VERSION)
    xr = caps.get('x_axis_range')
    if entry['kind'] == 'stacked_xy' and xr is None:
        xr = [float(min(expected)), float(max(expected))]
    yr = caps.get('y_axis_range', [caps['value_domain'][0], caps['sum_max'] or caps['value_domain'][1]])
    candidate = {'schema_version': 1, 'id': 'published', 'source': str(source), 'data': data,
                 'contract': {**{k: inspected[k] for k in ('source_sha256', 'payload_sha256', 'structure_sha256')},
                              'graph_index': 1, 'semantics': {
                                  'observation': 'independent', 'value_kind': 'raw',
                                  'axes': {'x': {'scale': 'linear' if entry['kind'] == 'stacked_xy' else 'categorical',
                                                 'range': xr if entry['kind'] == 'stacked_xy' else None, 'evidence': evidence},
                                           'y': {'scale': 'categorical' if heatmap else 'linear',
                                                 'range': None if heatmap else yr, 'evidence': evidence}},
                                  'statistics': {'state': 'none', 'evidence': evidence + '; no attached analyses or hypothesis tests'},
                                  'value_domain': {'range': caps['value_domain'], 'evidence': evidence}}}}
    candidate.update(mean_metadata)
    contract.validate_contract(inspected, candidate['contract'], data)
    return candidate, style


def validate_request(root: Path, entry: dict, data_path: Path, options: dict) -> dict:
    """Validate all data, style options and release evidence without any writes."""
    return _candidate(root, entry, data_path, options)[0]


def _json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


def _seed_info(source):
    root = ET.parse(source).getroot()
    # Surface the immutable XML Info content for release review. Native/Info
    # identity and schema eligibility have already passed inspect_source.
    return [ET.tostring(node, encoding='unicode') for node in root
            if contract.local(node) == 'Info']


def _native_axis_limits(candidate):
    """Apply numeric Y limits to bars; heatmap axes are both categorical.

    An XY source table does not make a stacked bar graph's X axis scriptable.
    Prism rejects SetAxisLimits X here. The unchanged seed and exact ordered
    X-value contract preserve positions; acceptance checks the rendered ticks.
    """
    result = {}
    for axis, specification in candidate['contract']['semantics']['axes'].items():
        if axis == 'x' or specification['scale'] == 'categorical':
            result[axis] = None
            continue
        low, high = [contract.number(str(v)) for v in specification['range']]
        step = (high - low) / 5
        triple = tuple(float(v) for v in (low, high, step))
        if not all(math.isfinite(v) for v in triple) or triple[0] >= triple[1] or triple[2] <= 0:
            raise ValueError('Approved %s axis cannot be represented by finite native limits and a positive tick step' % axis)
        result[axis] = triple
    return result


def _data_labels_visible(svg, labels):
    """Require each whole requested label in a positioned native text run.

    Do not concatenate separate baselines: a wrapped Set13 rendered as Set1 and
    3 does not verify Set13. Only whitespace within one run is normalized.
    """
    import svg_render
    compact = lambda value: ' '.join(value.split())
    actual = {compact(run['text']) for run in svg_render.text_runs(svg)
              if run.get('orientation') is not None and run.get('font_size', 0) > 0
              and all(math.isfinite(run.get(key, float('nan'))) for key in ('x0', 'x1', 'y0', 'y1'))}
    return {kind: {label: compact(label) in actual for label in requested}
            for kind, requested in [('series', labels['series_order']), ('rows', labels['row_order'] or [])]}


def _heatmap_colorbar_visible(svg, label):
    """Verify the calibrated vertical label beside the heatmap frame.

    The approved native seed has its colorbar label to the right of the plot,
    centered vertically. Relative distances tolerate native crop/translation
    when labels change, without letting title/row/series text impersonate it.
    """
    import svg_render
    root = ET.parse(svg).getroot()
    frame = svg_render._plot_geometry(root)
    if frame is None:
        return False
    compact = lambda value: ' '.join(value.split())
    found = []
    height = frame['bottom'] - frame['top']
    for run in svg_render.text_runs(root):
        if (compact(run['text']) != compact(label) or run.get('orientation') != 'vertical' or
                run.get('font_size', 0) <= 0 or
                not all(math.isfinite(run.get(key, float('nan')))
                        for key in ('x0', 'x1', 'y0', 'y1', 'font_size'))):
            continue
        size = run['font_size']
        center = (run['y0'] + run['y1']) / 2
        if (frame['right'] + 0.5 * size <= run['x0'] < run['x1'] <= frame['right'] + 8 * size and
                frame['top'] <= run['y0'] < run['y1'] <= frame['bottom'] and
                frame['top'] + 0.25 * height <= center <= frame['bottom'] - 0.25 * height):
            found.append(run)
    return len(found) == 1


def draw_published(root: Path, entry: dict, data_path: Path, outdir: Path, name: str,
                   options: dict, exporter=None) -> dict:
    """Prepare, save and reopen once; allow just one extra normalization export."""
    candidate, style = _candidate(root, entry, data_path, options)
    axis_limits = _native_axis_limits(candidate)
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', name):
        raise ValueError('Output name must contain 1–80 ASCII letters, numbers, underscores or hyphens')
    requested_out = Path(outdir).expanduser().absolute()
    if requested_out.exists() or requested_out.is_symlink():
        raise ValueError('Output must be a new directory; existing projects are never overwritten')
    output = requested_out.resolve()
    source = Path(candidate['source'])
    output.mkdir(parents=True, exist_ok=False)
    prepared = output / (name + '_prepared.pzfx')
    contract.patch_candidate(source, candidate['data'], prepared, candidate['contract'])
    prepared_hash = contract.sha256(prepared)
    data = candidate['data']
    labels = {'series_order': [c['title'] for c in data['y_columns']],
              'row_order': data['row_titles']['subcolumns'][0]['values'] if data['row_titles'] else None,
              'x_axis_title': style['x_axis_title'], 'y_axis_title': style['y_axis_title'],
              'graph_title': style.get('graph_title')}
    spec_path = output / (name + '.spec.json')
    spec_args = {**style, 'template': entry['alias'], 'name': name,
                 'data': Path(data_path).read_text(encoding='utf-8-sig'), 'data_is_path': False}
    # An inherited absent label is not an explicit request to clear a title.
    # Preserve that distinction so default preparation can replay unchanged.
    for key in ('x_axis_title', 'y_axis_title'):
        if not spec_args[key]:
            spec_args.pop(key)
    _json(spec_path, {'version': 1, 'tool': 'prism_draw', 'adapter_version': ADAPTER_VERSION,
                      'template_sha256': entry['seed_sha256'],
                      'execution_sha256': entry['approval']['execution_sha256'], 'arguments': spec_args})
    _json(output / (name + '.candidate.json'), candidate)
    summary = {'ok': False, 'template': entry['alias'], 'hint': style['hint'],
               'plot_definition': ('arithmetic mean heatmap of all supplied raw replicates; automatic color scale; no hypothesis testing'
                                   if entry['kind'] == 'mean_heatmap' else
                                   'inherited stacked XY bars; fixed X and original palette slots' if entry['kind'] == 'stacked_xy'
                                   else 'grouped mean ± sample SEM from all supplied raw replicates; no hypothesis testing'),
               'native_style': 'inherit', 'graph_index': 1, 'auto_axis': False,
               'x_axis_limits': axis_limits['x'], 'y_axis_limits': axis_limits['y'],
               'source_matches_export': False, 'paths': {'project': str(prepared), 'spec': str(spec_path)},
               'capabilities': copy.deepcopy(entry['capabilities']), 'applied_labels': labels,
               'x_axis_title': style['x_axis_title'], 'y_axis_title': style['y_axis_title'],
               'title': style.get('graph_title'), 'prism_log': '', 'prism_run_ok': None,
               'requires_manual_review': True, 'calls': [], 'seed_info_xml': _seed_info(source),
               'outdir': str(output), 'export_format': 'svg', 'status': 'prepared',
               'template_sha256': entry['seed_sha256'], 'execution_sha256': entry['approval']['execution_sha256']}
    if entry['kind'] == 'mean_heatmap':
        summary.update({key: candidate[key] for key in ('aggregation', 'color_scale_mode', 'input_domain',
                                                       'color_domain', 'color_domain_decimal')})
    if not style['run_prism']:
        _json(output / (name + '.result.json'), summary)
        return summary
    export = exporter or pilot._native_export
    previous_project, previous_png = prepared, None
    immutable = {prepared: prepared_hash}
    try:
        import svg_render
        for stage in ('new', 'reopened', 'normalized'):
            _hash_file(source, entry['seed_sha256'], 'Seed')
            if implementation_fingerprint() != entry['approval']['execution_sha256']:
                raise ValueError('Release execution fingerprint changed during native export')
            for path, fingerprint in immutable.items():
                _hash_file(path, fingerprint, 'Previously saved artifact')
            basename = name + '_' + stage
            project = output / (basename + '.pzfx')
            shutil.copy2(previous_project, project)
            native_limits = ({} if entry['kind'] == 'mean_heatmap' else
                             {'x_axis_limits': axis_limits['x'], 'y_axis_limits': axis_limits['y']})
            ok, log, svg = export(project, output, basename, clean_metadata=False, graph_index=1,
                                  title_mode='native', keep_prism_warm=False, timeout=style['timeout'],
                                  x_axis_title=style['x_axis_title'] or None,
                                  y_axis_title=style['y_axis_title'] or None,
                                  graph_title=style.get('graph_title'), **native_limits)
            call = {'stage': stage, 'project': str(project), 'svg': str(svg), 'ok': bool(ok), 'message': str(log)}
            summary['calls'].append(call)
            summary['prism_log'] += '%s: %s\n' % (stage, log)
            if not ok:
                raise ValueError('Native %s export failed: %s' % (stage, log))
            svg = Path(svg).resolve()
            if svg != output / (basename + '.svg'):
                raise ValueError('Native exporter returned an unexpected SVG path')
            pilot._check_svg(svg)
            pilot._check_saved(project, data)
            visible = svg_render.titles_visible(svg, **{key: style.get(key) for key in
                                                       ('x_axis_title', 'y_axis_title', 'graph_title')})
            if not all(visible.values()):
                raise ValueError('Requested native axis/graph titles are not visibly verified: %s' % visible)
            call['data_labels_visible'] = _data_labels_visible(svg, labels)
            missing = {kind: [label for label, found in checks.items() if not found]
                       for kind, checks in call['data_labels_visible'].items()}
            if any(missing.values()):
                raise ValueError('Requested native series/row labels are missing or split across runs: %s' % missing)
            if entry['kind'] == 'mean_heatmap':
                label = entry['capabilities']['colorbar_label']
                call['colorbar_label_visible'] = _heatmap_colorbar_visible(svg, label)
                if not call['colorbar_label_visible']:
                    raise ValueError('Fixed native colorbar label is not verified in its calibrated vertical right-side role: %s' % label)
            png = output / (basename + '.png')
            svg_render.render(svg, png)
            if not png.is_file() or png.stat().st_size == 0:
                raise ValueError('Readable native preview is missing')
            for path, fingerprint in immutable.items():
                _hash_file(path, fingerprint, 'Previously saved artifact')
            png_hash = contract.sha256(png)
            call.update(saved_data_check=True, titles_visible=visible, preview_sha256=png_hash)
            immutable.update({project: contract.sha256(project), svg: contract.sha256(svg), png: png_hash})
            if previous_png == png_hash:
                summary.update(ok=True, source_matches_export=True, prism_run_ok=True,
                               requires_manual_review=False, status='complete', svg_titles_rendered=True,
                               stability_check='Consecutive native exports have identical readable PNG hashes')
                summary['paths'].update(project=str(project), svg=str(svg), preview_png=str(png))
                break
            previous_project, previous_png = project, png_hash
        else:
            raise ValueError('Native appearance did not stabilize within three saves/exports')
        _hash_file(source, entry['seed_sha256'], 'Seed')
        if implementation_fingerprint() != entry['approval']['execution_sha256']:
            raise ValueError('Release execution fingerprint changed during native export')
    except Exception as exc:
        summary.update(ok=False, source_matches_export=False, prism_run_ok=False,
                       requires_manual_review=True, status='failed', error=str(exc))
        summary['paths'] = {'project': str(prepared), 'spec': str(spec_path)}
    summary['seed_unchanged'] = source.is_file() and contract.sha256(source) == entry['seed_sha256']
    if not summary['seed_unchanged']:
        summary.update(ok=False, source_matches_export=False, prism_run_ok=False,
                       requires_manual_review=True, status='failed', error='Seed fingerprint changed during native export')
        summary['paths'] = {'project': str(prepared), 'spec': str(spec_path)}
    _json(output / (name + '.result.json'), summary)
    return summary
