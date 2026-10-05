"""Filesystem release snapshot for a long-lived MCP process, not hot reload.

Code is sampled before and after bootstrap imports. Every public tool request
compares fresh bytes with that snapshot; template metadata has its own content
invalidation and approval checks. This is a local consistency guard, not a code
signature or protection against a hostile process modifying Python internals.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid
import datetime


def _snapshot(root):
    directory = Path(root).resolve() / 'scripts'
    files, errors = {}, []
    try:
        paths = sorted(directory.rglob('*.py'))
        for path in paths:
            relative = path.relative_to(directory.parent).as_posix()
            try:
                resolved = path.resolve()
                if directory not in resolved.parents or not path.is_file():
                    raise ValueError('runtime path escapes its scripts directory')
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except (OSError, ValueError) as exc:
                errors.append(relative + ': ' + str(exc))
    except OSError as exc:
        errors.append('scripts: ' + str(exc))
    if not files:
        errors.append('No Python runtime files found')
    # The Windows library executor uses owned, source-only sibling modules.
    # Bind these too; do not traverse fixtures, environments or run evidence.
    library = Path(root).resolve() / 'development/library_v1'
    if library.exists() or library.is_symlink():
        try:
            if library.is_symlink() or not library.is_dir():
                raise ValueError('Unsafe Windows library directory')
            for path in sorted(library.iterdir()):
                if path.suffix not in ('.py', '.json'):
                    continue
                relative = path.relative_to(Path(root).resolve()).as_posix()
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Unsafe Windows library runtime file: ' + relative)
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            errors.append('development/library_v1: ' + str(exc))
    policy=Path(root).resolve()/'assets/prism_execution.json'
    if policy.exists()or policy.is_symlink():
        try:
            if policy.is_symlink()or not policy.is_file()or policy.stat().st_size>4096:raise ValueError('Unsafe execution policy')
            files['assets/prism_execution.json']=hashlib.sha256(policy.read_bytes()).hexdigest()
        except (OSError,ValueError)as exc:errors.append('assets/prism_execution.json: '+str(exc))
    identity = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return files, identity, errors


class RuntimeRestartRequired(RuntimeError):
    def __init__(self, status):
        self.status = dict(status)
        self.status['error'] = 'runtime_restart_required'
        super().__init__('Prism MCP runtime restart required: code changed or bootstrap was inconsistent. '
                         'Reconnect the Prism MCP server, not the Prism application.')


class RuntimeState:
    def __init__(self, root, *, version):
        self.root = Path(root).resolve()
        self.version = version
        self.instance_id=str(uuid.uuid4())
        self.started_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._files, self._identity, self._errors = _snapshot(self.root)
        self._consistent = not self._errors
        self._modules = {}
        self._mismatches = []
        self._source_modules = {}
        self._source_hashes = {}
        self._local_names = {Path(name).stem for name in self._files
                             if Path(name).parent == Path('scripts')}
        self._builtins = dict(vars(builtins), __import__=self._import_local)

    def bind_server(self, namespace, code, runtime_module, runtime_source):
        """Bind source-only eager/lazy local imports without altering sys.modules.

        The entrypoint and this helper have already started executing, so their
        actual compilation inputs must be checked separately from disk hashes.
        The server supplies its current frame code and the bytes it compiled for
        this helper. Standard-library and installed third-party imports retain
        Python's ordinary importer and virtual-environment behavior.
        """
        self.verify_bootstrap(runtime_module, runtime_source)
        self.verify_entrypoint(namespace['__file__'], code)
        namespace['__builtins__'] = self._builtins

    def verify_bootstrap(self, runtime_module, runtime_source):
        """Record the exact helper source compiled by a source-only bootstrap."""
        self._record_source('runtime_state', runtime_module, runtime_source)
        self._source_modules['runtime_state'] = runtime_module

    def verify_entrypoint(self, filename, code):
        """Check an already-executing entrypoint against its full source code."""
        filename = Path(filename).resolve()
        relative = filename.relative_to(self.root).as_posix()
        source = filename.read_bytes()
        expected = compile(source, str(filename), 'exec', dont_inherit=True)
        digest = hashlib.sha256(source).hexdigest()
        if code != expected or digest != self._files.get(relative):
            self._consistent = False
            self._errors.append(relative + ': executing entrypoint does not match source')
        else:
            self._source_hashes[relative] = digest
            self._modules[filename.stem] = str(filename)

    def _record_source(self, name, module, source):
        path = Path(module.__file__).resolve()
        relative = path.relative_to(self.root).as_posix()
        digest = hashlib.sha256(source).hexdigest()
        self._modules[name] = str(path)
        self._source_hashes[relative] = digest
        if digest != self._files.get(relative):
            self._consistent = False
            self._errors.append(relative + ': compiled bytes differ from startup snapshot')

    def load_module(self, name):
        """Load one flat scripts module from captured source, including imports.

        This private graph deliberately does not reuse caller-loaded modules.
        No bytecode caches are read or written and no modules are hot reloaded.
        """
        if name in self._source_modules:
            return self._source_modules[name]
        if name not in self._local_names:
            raise ImportError('Not a local runtime module: ' + name)
        path = self.root / 'scripts' / (name + '.py')
        source = path.read_bytes()
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        module.__dict__['__builtins__'] = self._builtins
        self._record_source(name, module, source)
        if self._source_hashes.get('scripts/' + name + '.py') != self._files.get('scripts/' + name + '.py'):
            raise RuntimeRestartRequired(self.status())
        self._source_modules[name] = module
        try:
            exec(compile(source, str(path), 'exec', dont_inherit=True), module.__dict__)
        except BaseException:
            self._source_modules.pop(name, None)
            self._consistent = False
            raise
        return module

    def _import_local(self, name, globals=None, locals=None, fromlist=(), level=0):
        if not level and name.split('.', 1)[0] in self._local_names:
            # The runtime is a flat scripts directory, not a Python package.
            if '.' in name:
                raise ImportError('Local runtime modules are not packages: ' + name)
            return self.load_module(name)
        return builtins.__import__(name, globals, locals, fromlist, level)

    def finish_loading(self, modules):
        directory = self.root / 'scripts'
        for name, module in modules.items():
            filename = getattr(module, '__file__', None)
            path = Path(filename).resolve() if filename else None
            self._modules[name] = str(path) if path else None
            if path is None or directory not in path.parents:
                self._mismatches.append(name)
        files, _, errors = _snapshot(self.root)
        self._consistent = self._consistent and files == self._files and not errors and not self._mismatches

    def status(self):
        files, identity, errors = _snapshot(self.root)
        changed = sorted(name for name in self._files.keys() | files.keys() if self._files.get(name) != files.get(name))
        restart = bool(changed or errors or self._errors or not self._consistent)
        return {
            'version': self.version,
            'pid': os.getpid(),
            'instance_id': self.instance_id,
            'started_at': self.started_at,
            'transport': 'stdio',
            'http_origin': None,
            'loaded_root': str(self.root),
            'loaded_identity': self._identity,
            'disk_identity': identity,
            'runtime_file_count': len(self._files),
            'loaded_modules': dict(self._modules),
            'loaded_source_hashes': dict(self._source_hashes),
            'module_root_mismatches': list(self._mismatches),
            'startup_consistent': self._consistent,
            'changed_files': changed,
            'read_errors': list(self._errors) + errors,
            'restart_required': restart,
        }

    def assert_current(self):
        status = self.status()
        if status['restart_required']:
            raise RuntimeRestartRequired(status)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--expected-identity', help='Required parent release identity for --execute')
    parser.add_argument('--execute', nargs=argparse.REMAINDER, metavar='MODULE')
    args = parser.parse_args()
    state = RuntimeState(args.root, version='filesystem-check')
    state.verify_entrypoint(__file__, sys._getframe().f_code)
    state.finish_loading({})
    if args.execute is not None:
        if not args.execute or not args.expected_identity:
            parser.error('--execute requires a module and --expected-identity')
        try:
            if args.expected_identity != state.status()['loaded_identity']:
                state._consistent = False
                state._errors.append('Child runtime differs from the parent release identity')
            state.assert_current()
            module = state.load_module(args.execute[0])
            state.assert_current()
            result = module.main(args.execute[1:])
            state.assert_current()
            raise SystemExit(result)
        except RuntimeRestartRequired as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
    else:
        print(json.dumps(state.status(), indent=2))
