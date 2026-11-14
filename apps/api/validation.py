import io
import stat
import zipfile
from pathlib import PurePosixPath

MAX_INPUT = 8 * 1024 * 1024  # Spike deliberately below production 100 MiB.


def inspect_zip(data: bytes, max_entries=2000, max_expanded=32 * 1024 * 1024):
    if len(data) > MAX_INPUT:
        raise ValueError('compressed limit')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > max_entries:
            raise ValueError('entry limit')
        total = 0
        for item in entries:
            name = item.filename.replace('\\', '/')
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or ':' in name or '\x00' in name or len(path.parts) > 16:
                raise ValueError('unsafe path')
            if stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1:
                raise ValueError('symlink or encryption')
            if path.suffix.lower() in {'.zip', '.jar', '.7z', '.rar'}:
                raise ValueError('nested archive')
            total += item.file_size
            if total > max_expanded or item.file_size > max(1, item.compress_size) * 100:
                raise ValueError('expanded limit or ratio')
            actual = 0
            with archive.open(item) as stream:
                while chunk := stream.read(65536):
                    actual += len(chunk)
                    if actual > item.file_size or actual > max_expanded:
                        raise ValueError('actual expanded limit')
        return {'entries': len(entries), 'expanded_bytes': total}


def pdf_probe(data):
    # Candidate renderer: availability is a feasibility result, never assumed.
    import pymupdf
    with pymupdf.open(stream=data, filetype='pdf') as document:
        if not document.page_count:
            raise ValueError('empty PDF')
        return document[0].get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5)).tobytes('png')
