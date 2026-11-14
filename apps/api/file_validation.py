"""Untrusted parsers: invoked only inside the limited child process in production."""
import html
import json
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class Limits:
    input_bytes: int = 104857600
    entries: int = 2000
    expanded_bytes: int = 524288000
    entry_bytes: int = 104857600
    ratio: int = 100
    depth: int = 16
    path_chars: int = 1024
    preview_bytes: int = 262144
    readme_bytes: int = 32768
    pages: int = 200
    pdf_objects: int = 100000


class InvalidContent(Exception):
    """Only constant, actionable categories; no attacker-controlled paths/text."""


def inspect_zip(path: Path, output: Path, limits=Limits()):
    seen, files, total, readme = set(), [], 0, ''
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries)>limits.entries:
                raise InvalidContent('ZIP_ENTRY_LIMIT')
            for item in entries:
                name = unicodedata.normalize('NFKC', item.filename.replace('\\','/'))
                parts = PurePosixPath(name).parts
                if (not parts or len(name)>limits.path_chars or '\x00' in item.orig_filename or name.startswith('/') or '..' in parts or ':' in name
                        or any(ord(c)<32 for c in name) or len(parts)>limits.depth
                        or any(p.endswith((' ','.')) for p in parts)):
                    raise InvalidContent('ZIP_UNSAFE_PATH')
                normalized = '/'.join(parts).casefold()
                if normalized in seen:
                    raise InvalidContent('ZIP_PATH_COLLISION')
                if any('/'.join(parts[:i]).casefold() in files for i in range(1,len(parts))):
                    raise InvalidContent('ZIP_PATH_COLLISION')
                if not item.is_dir() and any(s.startswith(normalized+'/') for s in seen):
                    raise InvalidContent('ZIP_PATH_COLLISION')
                seen.add(normalized)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)):
                    raise InvalidContent('ZIP_SPECIAL_FILE')
                if item.flag_bits & 1:
                    raise InvalidContent('ZIP_ENCRYPTED')
                suffix = PurePosixPath(name).suffix.casefold()
                if suffix in {'.zip','.jar','.7z','.rar','.gz','.bz2','.xz','.tar','.tgz'}:
                    raise InvalidContent('ZIP_NESTED_ARCHIVE')
                if suffix in {'.exe','.dll','.com','.scr','.msi','.lnk','.iso'}:
                    raise InvalidContent('ZIP_UNSUPPORTED_BINARY')
                if (item.file_size>limits.entry_bytes or total+item.file_size>limits.expanded_bytes
                        or item.file_size>max(1,item.compress_size)*limits.ratio):
                    raise InvalidContent('ZIP_EXPANSION_LIMIT')
                if item.is_dir():
                    continue
                files.append(normalized)
                actual, prefix, captured = 0, b'', bytearray()
                is_readme = parts[-1].casefold() in ('readme','readme.md','readme.txt','readme.markdown')
                with archive.open(item) as stream:
                    while chunk := stream.read(65536):
                        actual += len(chunk)
                        total += len(chunk)
                        if actual>limits.entry_bytes or actual>item.file_size or total>limits.expanded_bytes:
                            raise InvalidContent('ZIP_EXPANSION_LIMIT')
                        if not prefix:
                            prefix = chunk[:8]
                        if is_readme and len(captured)<limits.readme_bytes:
                            captured.extend(chunk[:limits.readme_bytes-len(captured)])
                if actual != item.file_size:
                    raise InvalidContent('ZIP_CORRUPT')
                if prefix.startswith((b'PK\x03\x04', b'7z\xbc\xaf\x27\x1c', b'Rar!', b'\x1f\x8b')):
                    raise InvalidContent('ZIP_NESTED_ARCHIVE')
                if prefix.startswith((b'MZ',b'\x7fELF')):
                    raise InvalidContent('ZIP_UNSUPPORTED_BINARY')
                if is_readme and not readme:
                    readme = captured.decode('utf-8',errors='replace')
    except (zipfile.BadZipFile,RuntimeError,NotImplementedError,EOFError,ValueError) as exc:
        raise InvalidContent('ZIP_CORRUPT') from None
    # Contract does not mandate README. Absence changes preview, not acceptance.
    preview = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; form-action \'none\'"><title>Archive preview</title><h1>README</h1><pre>'+html.escape(readme or 'No README found.')+'</pre><h2>File inventory</h2><pre>'+html.escape('\n'.join(files[:500]))+'</pre>'
    encoded = preview.encode()
    if len(encoded)>limits.preview_bytes:
        raise InvalidContent('ZIP_PREVIEW_LIMIT')
    (output/'preview.html').write_bytes(encoded)
    return {'format':'ZIP','entries':len(seen),'expanded_bytes':total,'readme_present':bool(readme),
            'preview':'preview.html','content_type':'text/html; charset=utf-8','artifact_type':'ZIP_PREVIEW'}


def inspect_pdf(path: Path, output: Path, limits=Limits()):
    import pymupdf
    try:
        with pymupdf.open(path) as document:
            if not document.is_pdf or document.is_repaired:
                raise InvalidContent('PDF_CORRUPT')
            if document.is_encrypted or document.needs_pass or document.metadata.get('encryption'):
                raise InvalidContent('PDF_ENCRYPTED')
            if not 1<=document.page_count<=limits.pages or document.xref_length()>limits.pdf_objects:
                raise InvalidContent('PDF_COMPLEXITY_LIMIT')
            if document.embfile_count():
                raise InvalidContent('PDF_EMBEDDED_CONTENT')
            for xref in range(1,document.xref_length()):
                obj = document.xref_object(xref,compressed=True)
                if re.search(r'/(?:JS|JavaScript|Launch|OpenAction|AA|RichMedia|EmbeddedFile|XFA)\b',obj):
                    raise InvalidContent('PDF_ACTIVE_CONTENT')
            page = document[0]
            width,height = page.rect.width,page.rect.height
            if not (0<width<=14400 and 0<height<=14400):
                raise InvalidContent('PDF_PAGE_SIZE_LIMIT')
            scale = min(1.0,1000/max(width,height))
            png = page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),colorspace=pymupdf.csRGB,
                                   alpha=False,annots=False).tobytes('png')
            if len(png)>limits.preview_bytes:
                raise InvalidContent('PDF_PREVIEW_LIMIT')
            (output/'preview.png').write_bytes(png)
            return {'format':'PDF','pages':document.page_count,'preview':'preview.png',
                    'content_type':'image/png','artifact_type':'PDF_PREVIEW'}
    except InvalidContent:
        raise
    except (pymupdf.FileDataError,RuntimeError,ValueError):
        raise InvalidContent('PDF_CORRUPT') from None


def validate(path: Path, output: Path, limits=Limits()):
    if not 0<path.stat().st_size<=limits.input_bytes:
        raise InvalidContent('FILE_SIZE_LIMIT')
    with path.open('rb') as stream:
        magic = stream.read(8)
    if magic.startswith((b'PK\x03\x04',b'PK\x05\x06')):
        return inspect_zip(path,output,limits)
    if magic.startswith(b'%PDF-'):
        return inspect_pdf(path,output,limits)
    raise InvalidContent('UNSUPPORTED_FORMAT')
