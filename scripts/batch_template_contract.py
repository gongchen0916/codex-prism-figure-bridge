"""Fail-closed fixed-shape data patching, without modifying native style bytes.

This module proves file structure and written data, never scientific validity or
the visual binding of data to native graphs. Native fields outside inventory are
deliberately opaque and are preserved byte-for-byte before Prism runs.
"""
from pathlib import Path
import base64
import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import zlib
from xml.etree import ElementTree as ET

import native_inventory


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def local(node):
    return node.tag.rsplit('}', 1)[-1]


def children(node, tag):
    return [n for n in node if local(n) == tag]


def require_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError('%s requires exactly fields %s' % (label, ', '.join(sorted(keys))))


def number(value):
    # Decimal commas, excluded-value suffixes and locale guesses are unsupported.
    if not isinstance(value, str) or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value):
        raise ValueError('Only finite dot-decimal numeric strings or null blanks are supported')
    try:
        n = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Invalid numeric cell') from exc
    if not n.is_finite():
        raise ValueError('Nonfinite numeric cell')
    return n


def _parse(path):
    raw = Path(path).read_bytes()
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('DTD/entity declarations are unsupported')
    root = ET.fromstring(raw)
    if local(root) != 'GraphPadPrismFile':
        raise ValueError('Unsupported XML root')
    return raw, root


def _payload(root):
    nodes = [n for n in root.iter() if local(n) == 'Template']
    known_encoding = {'{urn:schemas-microsoft-com:datatypes}dt': 'bin.base64'}
    if len(nodes) != 1 or nodes[0].attrib not in ({}, known_encoding) or len(nodes[0]):
        raise ValueError('Exactly one plain compressed native Template payload is required')
    try:
        packed = base64.b64decode(re.sub(r'\s+', '', nodes[0].text or ''), validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed) + decoder.flush()
        if not decoder.eof or decoder.unused_data:
            raise ValueError('Incomplete or trailing compressed native data')
        native_inventory.parse_payload(raw)
        return raw
    except (ValueError, zlib.error) as exc:
        raise ValueError('Unsupported native payload: %s' % exc) from exc


def payload_bytes(path):
    return _payload(_parse(path)[1])


def _inventory(raw):
    sheets = native_inventory.parse_payload(raw)[0]['children']
    def names(tag):
        return [next((c['value'] for c in n['children'] if c['tag'] == 9), b'').rstrip(b'\0').decode('utf-8', errors='replace') for n in sheets if n['tag'] == tag]
    return dict(graph_names=names(0x8012), analysis_names=names(0x8039),
                info_names=names(0x8038), layout_names=names(0x8035),
                native_table_objects=len(names(0x8008)),
                **native_inventory.table_storage_roles(raw))


def _xml_native_table_binding(root, tables, inv):
    def ids(nodes, prefix):
        result = []
        for node in nodes:
            value = node.get('ID', '')
            match = re.fullmatch(prefix + r'(0|[1-9][0-9]*)', value)
            if match is None:
                raise ValueError('Unknown XML %s identity' % prefix)
            result.append(int(match.group(1)))
        if len(set(result)) != len(result):
            raise ValueError('Duplicate XML %s identity' % prefix)
        return sorted(result)

    data_ids = ids(tables, 'Table')
    infos = [n for n in root.iter() if local(n) == 'Info']
    info_ids = ids(infos, 'Info')
    if info_ids != inv['native_info_ids']:
        raise ValueError('XML/native Info identities do not match')
    sequences = children(root, 'InfoSequence')
    if len(sequences) > 1 or (info_ids and len(sequences) != 1):
        raise ValueError('Exactly one InfoSequence required when Info sheets exist')
    if sequences:
        refs = list(sequences[0])
        if any(local(n) != 'Ref' for n in refs) or ids(refs, 'Info') != info_ids:
            raise ValueError('InfoSequence must reference each Info sheet exactly once')
    if set(data_ids) & set(inv['info_table_object_ids']):
        raise ValueError('XML data table overlaps an Info backing store')
    if data_ids != inv['non_info_table_object_ids']:
        raise ValueError('XML data IDs do not account for every non-Info native store')


def _title(node):
    titles = children(node, 'Title')
    if len(titles) != 1 or titles[0].attrib or len(titles[0]):
        raise ValueError('Exactly one plain Title required for each table and data column')
    return titles[0].text or ''


def _column(node, identity, numeric=True):
    allowed = {'Width', 'Decimals', 'Subcolumns'}
    if local(node) == 'XAdvancedColumn':
        allowed.add('Version')
    if local(node) == 'RowTitlesColumn':
        allowed = {'Width'}
    if set(node.attrib) - allowed:
        raise ValueError('Unknown or summary column attributes: %s' % node.attrib)
    subs = children(node, 'Subcolumn')
    if not subs:
        raise ValueError('Explicit Subcolumn arrays required; sequence/direct representations unsupported')
    if numeric and node.get('Subcolumns') != str(len(subs)):
        raise ValueError('Declared Subcolumns does not match actual arrays')
    if any(local(n) not in {'Title', 'Subcolumn'} for n in node):
        raise ValueError('Unknown column child')
    title = _title(node) if numeric else None
    result = {'id': identity, 'title': title, 'subcolumns': []}
    shape = {'id': identity, 'attributes': dict(node.attrib), 'subcolumns': []}
    for i, sub in enumerate(subs):
        if sub.attrib or any(local(d) != 'd' or d.attrib or len(d) for d in sub):
            raise ValueError('Unknown subcolumn/cell metadata, excluded or calculated values unsupported')
        values = [d.text if d.text not in (None, '') else None for d in sub]
        if numeric:
            for value in values:
                if value is not None:
                    number(value)
        result['subcolumns'].append({'id': 'S%d' % i, 'values': values})
        shape['subcolumns'].append({'id': 'S%d' % i, 'rows': len(values)})
    return result, shape


def _table(table):
    def namespace_of(node):
        return node.tag.split('}', 1)[0] if node.tag.startswith('{') else ''
    if any(namespace_of(node) != namespace_of(table) for node in table.iter()):
        raise ValueError('Mixed data-table namespaces are unsupported')
    allowed_attrs = {'ID', 'TableType', 'XFormat', 'YFormat', 'Replicates', 'EVFormat'}
    if set(table.attrib) - allowed_attrs:
        raise ValueError('Unknown table attributes, extensions or pairing metadata')
    if any(not table.get(key) for key in ('ID', 'TableType', 'XFormat')):
        raise ValueError('Missing table identity/type/format')
    kind = table.get('TableType')
    if kind not in {'OneWay', 'XY', 'TwoWay'}:
        raise ValueError('Unsupported table type: %s' % kind)
    if table.get('YFormat') not in {None, 'replicates'}:
        raise ValueError('Summary/error-value formats are unsupported')
    if table.get('XFormat') != ('numbers' if kind == 'XY' else 'none'):
        raise ValueError('Unsupported X format')
    if any(local(n) not in {'Title', 'XColumn', 'XAdvancedColumn', 'YColumn', 'RowTitlesColumn'} for n in table):
        raise ValueError('Unknown table child, note or subcolumn title metadata')
    xs, advanced, ys, rows = (children(table, t) for t in ('XColumn', 'XAdvancedColumn', 'YColumn', 'RowTitlesColumn'))
    if len(xs) != (1 if kind == 'XY' else 0) or len(advanced) > 1 or (advanced and not xs) or not ys or len(rows) > 1:
        raise ValueError('Missing/duplicate X, Y or row-title structure')
    data = {'table_id': table.get('ID'), 'table_type': kind, 'table_title': _title(table),
            'x_column': None, 'y_columns': [], 'row_titles': None}
    shape = {'table_id': table.get('ID'), 'table_type': kind, 'attributes': dict(table.attrib),
             'x_column': None, 'x_advanced': None, 'y_columns': [], 'row_titles': None}
    if xs:
        data['x_column'], shape['x_column'] = _column(xs[0], 'X0')
        if len(data['x_column']['subcolumns']) != 1:
            raise ValueError('Only single-value X supported')
    if advanced:
        adv_data, shape['x_advanced'] = _column(advanced[0], 'X0')
        if adv_data != data['x_column']:
            raise ValueError('XAdvancedColumn is inconsistent with XColumn')
    for i, y in enumerate(ys):
        col, colshape = _column(y, 'Y%d' % i)
        if kind in {'XY', 'OneWay'} and len(col['subcolumns']) != 1:
            raise ValueError('OneWay/XY require single-value subcolumns')
        if kind in {'XY', 'TwoWay'} and table.get('Replicates') != str(len(col['subcolumns'])):
            raise ValueError('Replicates declaration does not match Y subcolumns')
        data['y_columns'].append(col)
        shape['y_columns'].append(colshape)
    if rows:
        data['row_titles'], shape['row_titles'] = _column(rows[0], 'Rows', numeric=False)
        if len(data['row_titles']['subcolumns']) != 1:
            raise ValueError('Exactly one row-title subcolumn supported')
    if kind in {'XY', 'TwoWay'}:
        lengths = [len(s['values']) for col in data['y_columns'] for s in col['subcolumns']]
        if xs:
            lengths.append(len(data['x_column']['subcolumns'][0]['values']))
        if rows:
            lengths.append(len(data['row_titles']['subcolumns'][0]['values']))
        if len(set(lengths)) != 1:
            raise ValueError('Ragged XY/TwoWay row arrays are unsupported')
    return data, shape


def inspect_source(path):
    """Read the current file; return explicit unsupported reasons without writing."""
    path = Path(path).resolve()
    result = dict(schema_version=1, source=str(path), source_sha256=sha256(path),
                  payload_sha256=None, structure_sha256=None, tables=[], native_inventory=None,
                  data=None, xml_inventory=[], supported=False, rejection_reasons=[])
    try:
        _, root = _parse(path)
        tables = [n for n in root.iter() if local(n) == 'Table']
        result['xml_inventory'] = [
            {'table_id': t.get('ID'), 'table_type': t.get('TableType'), 'attributes': dict(t.attrib),
             'columns': [{'kind': local(c), 'attributes': dict(c.attrib),
                          'subcolumn_rows': [len(s) for s in children(c, 'Subcolumn')]}
                         for c in t if local(c) in {'XColumn', 'XAdvancedColumn', 'YColumn', 'RowTitlesColumn'}]}
            for t in tables]
        raw = _payload(root)
        result['payload_sha256'] = hashlib.sha256(raw).hexdigest()
        inv = result['native_inventory'] = _inventory(raw)
        if inv['analysis_names'] or any('analysis' in local(n).lower() for n in root.iter()):
            raise ValueError('Analysis dependencies are unsupported')
        if len(tables) != 1:
            raise ValueError('Exactly one XML data table required')
        _xml_native_table_binding(root, tables, inv)
        sequences = children(root, 'TableSequence')
        if len(sequences) > 1:
            raise ValueError('Duplicate TableSequence')
        if sequences:
            refs = list(sequences[0])
            if len(refs) != 1 or local(refs[0]) != 'Ref' or refs[0].get('ID') != tables[0].get('ID'):
                raise ValueError('TableSequence must reference the inspected table exactly once')
        if len(inv['graph_names']) != 1 or inv['layout_names']:
            raise ValueError('Exactly one graph and no layout pages required')
        result['data'], shape = _table(tables[0])
        result['tables'] = [shape]
        result['structure_sha256'] = digest(shape)
        result['supported'] = True
    except (ValueError, ET.ParseError) as exc:
        result['rejection_reasons'].append(str(exc))
    return result


def _valid_range(spec, label):
    require_keys(spec, {'range', 'evidence'}, label)
    limits = spec['range']
    if not isinstance(limits, list) or len(limits) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in limits):
        raise ValueError('%s requires two finite numeric bounds' % label)
    low, high = (number(str(v)) for v in limits)
    if low >= high or not isinstance(spec['evidence'], str) or not spec['evidence'].strip():
        raise ValueError('%s requires ordered bounds and evidence' % label)
    return low, high


def validate_contract(inspected, contract, data):
    require_keys(contract, {'source_sha256', 'payload_sha256', 'structure_sha256', 'graph_index', 'semantics'}, 'contract')
    if not inspected['supported']:
        raise ValueError('; '.join(inspected['rejection_reasons']))
    for key in ('source_sha256', 'payload_sha256', 'structure_sha256'):
        if contract[key] != inspected[key]:
            raise ValueError('%s fingerprint mismatch' % key)
    if type(contract['graph_index']) is not int or contract['graph_index'] != 1:
        raise ValueError('Only the inspected single graph index 1 is supported')
    _validate_data(inspected['data'], data)
    sem = contract['semantics']
    require_keys(sem, {'observation', 'value_kind', 'axes', 'statistics', 'value_domain'}, 'semantics')
    if sem['observation'] != 'independent' or sem['value_kind'] != 'raw':
        raise ValueError('Explicit independent raw observation semantics required')
    require_keys(sem['axes'], {'x', 'y'}, 'axes')
    for axis, spec in sem['axes'].items():
        require_keys(spec, {'scale', 'range', 'evidence'}, axis + ' axis')
        if spec['scale'] not in {'linear', 'categorical'} or not isinstance(spec['evidence'], str) or not spec['evidence'].strip():
            raise ValueError('Known linear/categorical axes and evidence required')
        if spec['scale'] == 'linear':
            _valid_range({'range': spec['range'], 'evidence': spec['evidence']}, axis)
        elif spec['range'] is not None:
            raise ValueError('Categorical axis range must be null')
    require_keys(sem['statistics'], {'state', 'evidence'}, 'statistics')
    if sem['statistics']['state'] != 'none' or not isinstance(sem['statistics']['evidence'], str) or not sem['statistics']['evidence'].strip():
        raise ValueError('Explicit evidence of absent source statistics is required to run')
    low, high = _valid_range(sem['value_domain'], 'value_domain')
    for col in data['y_columns']:
        for sub in col['subcolumns']:
            if any(value is not None and not low <= number(value) <= high for value in sub['values']):
                raise ValueError('Y-table value outside declared value_domain')
    if data['x_column']:
        xspec = sem['axes']['x']
        if xspec['scale'] != 'linear':
            raise ValueError('Numeric XY X requires a declared linear X axis')
        xlow, xhigh = _valid_range({'range': xspec['range'], 'evidence': xspec['evidence']}, 'X')
        xv = data['x_column']['subcolumns'][0]['values']
        for i, value in enumerate(xv):
            if value is None:
                if any(col['subcolumns'][0]['values'][i] is not None for col in data['y_columns']):
                    raise ValueError('Y value without X is unsupported')
            elif not xlow <= number(value) <= xhigh:
                raise ValueError('X value outside declared axis range')


def _validate_data(original, data):
    require_keys(data, original.keys(), 'data')
    for key in ('table_id', 'table_type'):
        if data[key] != original[key]:
            raise ValueError('Table identity changed')
    if not isinstance(data['table_title'], str):
        raise ValueError('Table title must be a string')
    if not isinstance(data['y_columns'], list) or len(data['y_columns']) != len(original['y_columns']):
        raise ValueError('Y column count changed')
    pairs = list(zip(original['y_columns'], data['y_columns'])) + [(original[k], data[k]) for k in ('x_column', 'row_titles')]
    for old, new in pairs:
        if old is None:
            if new is not None:
                raise ValueError('Added unsupported column')
            continue
        require_keys(new, old.keys(), 'column')
        if old['id'] != new['id'] or not isinstance(new['subcolumns'], list) or len(old['subcolumns']) != len(new['subcolumns']):
            raise ValueError('Column identity/subcolumn count changed')
        if (old['title'] is None and new['title'] is not None) or (old['title'] is not None and not isinstance(new['title'], str)):
            raise ValueError('Numeric column title must be a string; row-title column title must remain null')
        for osub, nsub in zip(old['subcolumns'], new['subcolumns']):
            require_keys(nsub, {'id', 'values'}, 'subcolumn')
            if nsub['id'] != osub['id'] or not isinstance(nsub['values'], list) or len(nsub['values']) != len(osub['values']):
                raise ValueError('Subcolumn identity/row count changed')
            for value in nsub['values']:
                if value is not None:
                    if old['id'] == 'Rows':
                        if not isinstance(value, str):
                            raise ValueError('Row title must be string or null')
                    else:
                        number(value)


def patch_candidate(source, data, out, contract):
    """Write one new candidate after validating identities, domain and hashes."""
    source, out = Path(source).resolve(), Path(out).absolute()
    if out.resolve() == source or out.exists() or out.is_symlink():
        raise ValueError('Output must be a new file, never the source or an existing file')
    inspected = inspect_source(source)
    validate_contract(inspected, contract, data)
    raw, root = _parse(source)
    table = next(n for n in root.iter() if local(n) == 'Table')
    children(table, 'Title')[0].text = data['table_title']
    def assign(node, col):
        if col['title'] is not None:
            children(node, 'Title')[0].text = col['title']
        for subnode, sub in zip(children(node, 'Subcolumn'), col['subcolumns']):
            for cell, value in zip(subnode, sub['values']):
                cell.text = value
    for node, col in zip(children(table, 'YColumn'), data['y_columns']):
        assign(node, col)
    for tag, key in (('XColumn', 'x_column'), ('XAdvancedColumn', 'x_column'), ('RowTitlesColumn', 'row_titles')):
        for node in children(table, tag):
            assign(node, data[key])
    # Replace only the data-table region. The native payload and all bytes outside
    # that region retain the original encoding, whitespace and compressed bytes.
    pattern = rb'<(?P<prefix>[A-Za-z_][\w.-]*:)?Table\b[^>]*>.*?</(?P=prefix)Table\s*>'
    matches = list(re.finditer(pattern, raw, re.S))
    if not matches:  # Python backreferences to unmatched optional groups do not match.
        matches = list(re.finditer(rb'<Table\b[^>]*>.*?</Table\s*>', raw, re.S))
    if len(matches) != 1:
        raise ValueError('Cannot isolate exactly one native XML table region')
    match = matches[0]
    table.tail = None
    serialized_table = copy.deepcopy(table)
    if serialized_table.tag.startswith('{'):
        # Prism's native reader silently ignored an ElementTree-generated
        # ns0:Table and replaced 2x25 observations with a default 1x1 table.
        # Use an explicit default namespace, keeping expanded XML names intact
        # without mutating ElementTree's process-global namespace registry.
        namespace_prefix = serialized_table.tag.split('}', 1)[0] + '}'
        for node in serialized_table.iter():
            if not node.tag.startswith(namespace_prefix):
                raise ValueError('Mixed data-table namespaces are unsupported')
            node.tag = node.tag[len(namespace_prefix):]
        serialized_table.set('xmlns', namespace_prefix[1:-1])
    patched = raw[:match.start()] + ET.tostring(serialized_table, encoding='utf-8') + raw[match.end():]
    # Validate in memory before touching the output. Exclusive creation prevents
    # replacing files created by another actor between existence checks.
    try:
        patched_root = ET.fromstring(patched)
    except (ET.ParseError, UnicodeError) as exc:
        raise ValueError('Candidate text cannot be represented as valid XML') from exc
    if _payload(patched_root) != _payload(root):
        raise ValueError('Native payload bytes changed before write')
    if sha256(source) != inspected['source_sha256']:
        raise ValueError('Source changed during patch preparation')
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('xb') as stream:
        stream.write(patched)
    check = inspect_source(out)
    if not check['supported'] or check['data'] != data or check['structure_sha256'] != inspected['structure_sha256']:
        raise ValueError('Written candidate failed exact data/structure verification; file retained as evidence')
    if sha256(source) != inspected['source_sha256']:
        raise ValueError('Source changed during patch write')
    return {'path': str(out.resolve()), 'sha256': sha256(out), 'data_mapping_check': True,
            'native_payload_bytes_preserved_pre_prism': check['payload_sha256'] == inspected['payload_sha256'],
            'source_unchanged': True}
