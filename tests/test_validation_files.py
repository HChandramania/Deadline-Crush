import io
import json
import stat
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from file_validation import Limits,InvalidContent,validate


def archive(tmp_path,entries,compression=zipfile.ZIP_DEFLATED):
    path = tmp_path/'source'
    with zipfile.ZipFile(path,'w',compression=compression) as z:
        for name,data in entries:
            z.writestr(name,data)
    return path


def test_zip_preview_escapes_untrusted_markdown(tmp_path):
    path = archive(tmp_path,[('README.md','<script>alert(1)</script> [x](javascript:alert(1))'),('src/app.py','print(1)')])
    result = validate(path,tmp_path)
    assert result['format']=='ZIP' and result['readme_present']
    preview = (tmp_path/'preview.html').read_text()
    assert '<script>' not in preview and '&lt;script&gt;' in preview and '<a ' not in preview


def test_zip_readme_not_required_by_contract(tmp_path):
    assert not validate(archive(tmp_path,[('app.py','pass')]),tmp_path)['readme_present']


@pytest.mark.parametrize('name',['../secret','/absolute','C:/foo','a\\..\\foo','\\\\server\\share',
    'folder/file.exe','a.jar','a.zip','a.tar.gz','a.lnk','folder./file','a\x01b'])
def test_unsafe_archive_entries(tmp_path,name):
    with pytest.raises(InvalidContent):
        validate(archive(tmp_path,[(name,b'abc')]),tmp_path)


@pytest.mark.parametrize('names',[['A.txt','a.txt'],['a\\b','a/b'],['x','x/y'],['x/y','x'],['Ａ','A']])
def test_zip_path_collisions(tmp_path,names):
    with pytest.raises(InvalidContent,match='COLLISION'):
        validate(archive(tmp_path,[(name,'x') for name in names]),tmp_path)


def test_zip_symlink(tmp_path):
    info = zipfile.ZipInfo('link'); info.create_system=3; info.external_attr=(stat.S_IFLNK|0o777)<<16
    with pytest.raises(InvalidContent,match='SPECIAL_FILE'):
        validate(archive(tmp_path,[(info,'/etc/passwd')]),tmp_path)


@pytest.mark.parametrize('limits,entries',[
    (replace(Limits(),entries=1),[('a','x'),('b','y')]),
    (replace(Limits(),expanded_bytes=3),[('a','1234')]),
    (replace(Limits(),entry_bytes=3),[('a','1234')]),
    (replace(Limits(),ratio=1),[('a','x'*1000)]),
    (replace(Limits(),input_bytes=1),[('a','x')]),
    (replace(Limits(),depth=1),[('a/b','x')]),
])
def test_zip_resource_limits(tmp_path,limits,entries):
    with pytest.raises(InvalidContent):
        validate(archive(tmp_path,entries),tmp_path,limits)


def test_disguised_nested_archive(tmp_path):
    with pytest.raises(InvalidContent,match='NESTED'):
        validate(archive(tmp_path,[('data.bin',b'PK\x03\x04'+b'x'*100)]),tmp_path)


def test_corrupt_zip(tmp_path):
    path = archive(tmp_path,[('a','x')]); path.write_bytes(path.read_bytes()[:-20])
    with pytest.raises(InvalidContent,match='CORRUPT'):
        validate(path,tmp_path)


def test_zip_encrypted(tmp_path):
    path = archive(tmp_path,[('a','x')]); data=bytearray(path.read_bytes())
    data[6]|=1; central=data.index(b'PK\x01\x02'); data[central+8]|=1; path.write_bytes(data)
    with pytest.raises(InvalidContent,match='ENCRYPTED'):
        validate(path,tmp_path)


def test_zip_crc_mismatch(tmp_path):
    path=archive(tmp_path,[('a',b'abcd')],zipfile.ZIP_STORED)
    data=path.read_bytes().replace(b'abcd',b'efgh'); path.write_bytes(data)
    with pytest.raises(InvalidContent,match='CORRUPT'):
        validate(path,tmp_path)


def pdf(tmp_path,pages=1,encrypted=False):
    import pymupdf
    path=tmp_path/'source'
    with pymupdf.open() as d:
        for _ in range(pages): d.new_page().insert_text((30,30),'hello')
        d.save(path,encryption=pymupdf.PDF_ENCRYPT_AES_256 if encrypted else pymupdf.PDF_ENCRYPT_NONE,
               owner_pw='owner',user_pw='user')
    return path


def test_pdf_preview(tmp_path):
    result=validate(pdf(tmp_path),tmp_path)
    assert result['pages']==1 and (tmp_path/'preview.png').read_bytes().startswith(b'\x89PNG')


def test_pdf_encrypted(tmp_path):
    with pytest.raises(InvalidContent,match='ENCRYPTED'):
        validate(pdf(tmp_path,encrypted=True),tmp_path)


def test_pdf_page_limit(tmp_path):
    with pytest.raises(InvalidContent,match='COMPLEXITY'):
        validate(pdf(tmp_path,pages=2),tmp_path,replace(Limits(),pages=1))


def test_pdf_corrupt(tmp_path):
    path=tmp_path/'source'; path.write_bytes(b'%PDF-1.7\ninvalid')
    with pytest.raises(InvalidContent,match='CORRUPT'):
        validate(path,tmp_path)


def test_pdf_attachments_rejected(tmp_path):
    import pymupdf
    path=tmp_path/'source'
    with pymupdf.open() as d:
        d.new_page(); d.embfile_add('a',b'data'); d.save(path)
    with pytest.raises(InvalidContent,match='EMBEDDED'):
        validate(path,tmp_path)


def test_pdf_javascript_rejected(tmp_path):
    import pymupdf
    path=pdf(tmp_path)
    with pymupdf.open(path) as d:
        xref=d.get_new_xref(); d.update_object(xref,'<< /S /JavaScript /JS (alert\\(1\\)) >>')
        d.xref_set_key(d.pdf_catalog(),'OpenAction',f'{xref} 0 R')
        d.save(tmp_path/'active.pdf')
    with pytest.raises(InvalidContent,match='ACTIVE'):
        validate(tmp_path/'active.pdf',tmp_path)


@pytest.mark.parametrize('data',[b'',b'hello',b'MZevil'])
def test_unsupported_file(tmp_path,data):
    path=tmp_path/'source'; path.write_bytes(data)
    with pytest.raises(InvalidContent): validate(path,tmp_path)
