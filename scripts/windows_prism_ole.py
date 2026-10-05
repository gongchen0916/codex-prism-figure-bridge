"""Read current Prism OLE storage from a saved PPT, never patch its cache."""
import io
from pathlib import Path
import struct
import zipfile


def prism_packages(pptx):
    import olefile
    result=[]
    with zipfile.ZipFile(pptx)as deck:
        for name in deck.namelist():
            if not name.startswith('ppt/embeddings/') or not name.endswith('.bin'):
                continue
            if deck.getinfo(name).file_size>128*1024*1024:
                raise ValueError('Oversized OLE embedding')
            data=deck.read(name)
            if not olefile.isOleFile(io.BytesIO(data)):
                continue
            with olefile.OleFileIO(io.BytesIO(data))as ole:
                if not ole.exists('CONTENTS'):
                    continue
                contents=ole.openstream('CONTENTS').read()
            if len(contents)<8 or contents[4:8]!=b'PK\x03\x04':
                continue
            # OLE stream capacity can exceed the current native ZIP size. Using
            # the whole stream may select an obsolete ZIP central directory.
            size=struct.unpack('<I',contents[:4])[0]
            # Initial Office embeddings can record the total stream capacity
            # (including the four-byte prefix); edited objects record the used
            # payload boundary. Both were observed in native Prism 11 storage.
            if size<22 or size>len(contents):
                raise ValueError('Invalid Prism OLE payload length')
            payload=contents[4:min(len(contents),4+size)]
            with zipfile.ZipFile(io.BytesIO(payload))as native:
                if 'document.json'not in native.namelist():
                    raise ValueError('Embedded CONTENTS is not a native Prism project')
                if sum(i.file_size for i in native.infolist())>256*1024*1024:
                    raise ValueError('Oversized native Prism content')
                native.testzip()
            result.append((name,payload))
    return result


def extract_one(pptx,output):
    packages=prism_packages(pptx)
    if len(packages)!=1:
        raise ValueError('An exact embedding must be selected when a PPT has multiple Prism objects')
    output=Path(output)
    with output.open('xb')as stream:
        stream.write(packages[0][1])
    return packages[0][0]
