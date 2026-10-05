"""Use an exact private copy of the user's installed Windows symbol font.

Fonts are not installed globally or included in releases. A missing/different
copy fails closed instead of silently substituting a scientific symbol.
"""
import hashlib
from pathlib import Path

SEGOE_SYMBOL_SHA256='a4a35dcc62cd30e1a6c97b695ecef83e59d2b149e3af834f6a49f11851d56b37'

def symbol_font(cache=None):
    directory=Path(cache) if cache is not None else Path.home()/'.codex/prism-font-cache'
    path=directory/(SEGOE_SYMBOL_SHA256+'.ttf')
    if path.is_symlink() or not path.is_file():
        raise ValueError('Exact local Windows Segoe UI Symbol font cache required')
    if path.stat().st_size!=2514056 or hashlib.sha256(path.read_bytes()).hexdigest()!=SEGOE_SYMBOL_SHA256:
        raise ValueError('Windows symbol font fingerprint differs')
    return path

def svg_fonts(root):
    if any(n.get('font-family')=='Segoe UI Symbol'for n in root.iter()):
        return [str(symbol_font())]
    return []
