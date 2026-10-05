"""Measure painted positioned glyph ink in an analysis copy, never edit SVG."""
import json
import sys
import math
from xml.etree import ElementTree as ET


def measure(svg):
    import pymupdf
    root=ET.fromstring(svg)
    def clean(node):
        if node.get('display')=='none' or node.get('visibility')in('hidden','collapse'):return False
        for key in('opacity','fill-opacity'):
            if key in node.attrib and (not math.isfinite(float(node.get(key)))or float(node.get(key))<=0):return False
        if any(key in node.attrib for key in('style','mask','filter','clip-path')):return False
        for child in list(node):
            tag=child.tag.rsplit('}',1)[-1]
            if tag not in('g','text','tspan'):node.remove(child)
            elif not clean(child):node.remove(child)
        return True
    if not clean(root):return []
    for node in list(root.iter()):
        if node.tag.rsplit('}',1)[-1]not in('text','tspan'):continue
        xs=node.get('x','').replace(',',' ').split();value=node.text or ''
        if len(xs)<=1:continue
        ys=node.get('y','').replace(',',' ').split()
        if len(node) or len(xs)!=len(value) or len(ys)not in(1,len(value)) or 'rotate'in node.attrib:
            raise ValueError('Unknown positioned glyph metrics')
        node.text=None;node.attrib.pop('x',None);node.attrib.pop('y',None)
        for i,char in enumerate(value):
            child=ET.SubElement(node,'{http://www.w3.org/2000/svg}tspan',{'x':xs[i],'y':ys[0]if len(ys)==1 else ys[i]});child.text=char
    with pymupdf.open('svg',ET.tostring(root))as document:pdf_bytes=document.convert_to_pdf()
    previous=pymupdf.TOOLS.unset_quad_corrections()
    glyphs=[]
    try:
        pymupdf.TOOLS.unset_quad_corrections(True)
        with pymupdf.open('pdf',pdf_bytes)as document:
            blocks=document[0].get_text('rawdict',flags=pymupdf.TEXT_ACCURATE_BBOXES,clip=pymupdf.INFINITE_RECT())['blocks']
            for block in blocks:
                for line in block.get('lines',[]):
                    if abs(line['dir'][0]-1)>1e-7 or abs(line['dir'][1])>1e-7:continue
                    for span in line['spans']:
                        for char in span['chars']:
                            glyphs.append({'text':char['c'],'origin':char['origin'],'bbox':char['bbox'],'size':span['size']})
    finally:pymupdf.TOOLS.unset_quad_corrections(previous)
    return glyphs


if __name__=='__main__':
    value=sys.stdin.buffer.read(4*1024*1024+1)
    if len(value)>4*1024*1024:raise ValueError('Oversized glyph analysis')
    print(json.dumps(measure(value)))
