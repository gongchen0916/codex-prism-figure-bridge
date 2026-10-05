"""Deployable pure compiler for three local legacy PZFX mapping/reopen phases.

This module does not execute Prism or qualify a template. It accepts the closed
legacy mapping grammar only; modern, statistical and preparation routes require
their own execution contracts. All phase files share the original staged path.
"""

import math
import re


TAIL = 'Save\nClose\nOpenOutput "mapping_done.txt", CLEAR\nWText "done"\nCloseOutput\n'
NUMBER = r'[-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?'


def _require(condition, message):
    if not condition:
        raise ValueError('Local phase compiler: ' + message)


def _input_filename(value):
    _require(bool(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_. -]*\.csv', value)),
             'imports require local CSV filenames')
    _require('..' not in value and not re.match(r'p[012]-', value),
             'phase outputs or traversal cannot be import inputs')
    return value


def compile_local_three_phase(initial, pages):
    """Return ``(script_text, manifest)`` without filesystem or native effects.

    ``pages`` is an ordered list of unique ``gNNN`` then ``lNNN`` page names.
    The manifest lists original inputs, every generated output, the completion
    filename and each phase's project/SVG mapping. The original seed and CSV
    filenames are inputs only; no output replaces an original input.
    """
    _require(isinstance(pages, list) and bool(pages), 'ordered page list required')
    _require(all(isinstance(p, str) and re.fullmatch(r'[gl][0-9]{3}', p)
                 and int(p[1:]) > 0 for p in pages), 'canonical page names required')
    _require(len(set(pages)) == len(pages) and pages == sorted(pages)
             and pages[0].startswith('g'), 'unique graph-then-layout page order required')
    _require(isinstance(initial, str) and '\r' not in initial
             and '\x00' not in initial and initial.endswith(TAIL),
             'exact legacy completion tail required')
    lines = initial[:-len(TAIL)].splitlines()
    _require(len(lines) >= 4 and lines[0] == 'CreateLog'
             and re.fullmatch(r'SetPath "[^"\r\n%]+"', lines[1])
             and lines[2] == 'Open "project.pzfx"', 'exact staged legacy header required')

    mapping = True
    current_table = None
    cleared = False
    table_imports = 0
    table_titled = False
    imports = []
    seen_tables = []
    exported = []
    current_page = None
    renamed = lines[:3]
    for line in lines[3:]:
        _require(bool(line) and line == line.strip(), 'blank or padded commands are unsupported')
        if mapping:
            match = re.fullmatch(r'GoTo D, ([1-9][0-9]*)', line)
            if match:
                if current_table is not None:
                    _require(cleared and table_imports and table_titled, 'incomplete data-table mapping')
                current_table = int(match[1])
                _require(current_table not in seen_tables, 'duplicate data-table mapping')
                seen_tables.append(current_table)
                cleared = table_titled = False
                table_imports = 0
            elif line == 'ClearTable':
                _require(current_table is not None and not cleared, 'unexpected ClearTable')
                cleared = True
            elif line.startswith('Import '):
                match = re.fullmatch(r'Import "([^"\r\n]+)", ([0-9]+), (-1|[0-9]+), ([1-9][0-9]*)', line)
                _require(match is not None and cleared and not table_titled, 'unexpected or malformed data import')
                imports.append(_input_filename(match[1]))
                table_imports += 1
            elif re.fullmatch(r'SetSheetTitle "[^"\r\n]*"', line):
                _require(cleared and table_imports and not table_titled, 'unexpected data-table title')
                table_titled = True
            elif line == 'Save':
                _require(current_table is not None and cleared and table_imports and table_titled,
                         'complete mapping required before pre-export Save')
                mapping = False
            else:
                raise ValueError('Local phase compiler: unexpected mapping command: ' + line.split()[0])
            renamed.append(line)
            continue

        match = re.fullmatch(r'GoTo ([GL]), ([1-9][0-9]*)', line)
        if match:
            _require(current_page is None, 'page switched before export')
            current_page = match[1].lower() + f'{int(match[2]):03d}'
            _require(len(exported) < len(pages) and current_page == pages[len(exported)],
                     'native page scope or order differs')
            renamed.append(line)
        elif line.startswith('ExportSVG '):
            _require(current_page is not None and line == f'ExportSVG "{current_page}.svg"',
                     'unexpected SVG export filename')
            renamed.append(f'ExportSVG "p0-{current_page}.svg"')
            exported.append(current_page)
            current_page = None
        else:
            _require(current_page is not None and current_page.startswith('g'),
                     'only graph settings may precede an export')
            title = re.fullmatch(r'(?:SetSheetTitle|SetGraphTitle) "[^"\r\n]*"', line)
            axis_title = re.fullmatch(r'SetAxisTitle (?:X|Y|Y2), "[^"\r\n]*"', line)
            axis_limit = re.fullmatch(r'SetAxisLimits (?:X|Y|Y2) (?:bottom|top|interval) (' + NUMBER + ')', line)
            _require(bool(title or axis_title or axis_limit), 'unexpected graph command: ' + line.split()[0])
            if axis_limit:
                _require(math.isfinite(float(axis_limit[1])), 'nonfinite axis limit')
            renamed.append(line)

    _require(not mapping and exported == pages and current_page is None,
             'complete requested graph/layout exports required')
    renamed += ['Save "p0-project.pzfx"', 'Close']
    phases = []
    for phase in range(3):
        phase_pages = {page: f'p{phase}-{page}.svg' for page in pages}
        phases.append({'phase': phase, 'project': f'p{phase}-project.pzfx', 'pages': phase_pages})
        if phase:
            renamed.append(f'Open "p{phase-1}-project.pzfx"')
            for page in pages:
                renamed += [f'GoTo {page[0].upper()}, {int(page[1:])}',
                            f'ExportSVG "{phase_pages[page]}"']
            renamed += [f'Save "p{phase}-project.pzfx"', 'Close']
    renamed += ['OpenOutput "mapping_done.txt", CLEAR', 'WText "done"', 'CloseOutput']
    outputs = [name for phase in phases for name in [phase['project'], *phase['pages'].values()]]
    outputs.append('mapping_done.txt')
    manifest = {'input_files': ['project.pzfx', *dict.fromkeys(imports)],
                'output_files': outputs, 'done_file': 'mapping_done.txt', 'phases': phases}
    return '\n'.join(renamed) + '\n', manifest
