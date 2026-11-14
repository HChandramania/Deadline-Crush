"""Real Linux child-process tests, skipped rather than emulated on Windows."""
import asyncio
import json
import sys
import zipfile
import pytest
from validation_pipeline import render,ProcessingFailure

pytestmark=pytest.mark.skipif(sys.platform!='linux',reason='Linux enforced-resource child required')


def test_real_limited_zip_child(tmp_path):
    path=tmp_path/'source'
    with zipfile.ZipFile(path,'w') as z: z.writestr('README.md','safe')
    result=asyncio.run(render(path,tmp_path,[]))
    assert result['status']=='ACCEPTED' and (tmp_path/'preview.html').exists()


def test_real_limited_pdf_child(tmp_path):
    import pymupdf
    path=tmp_path/'source'
    with pymupdf.open() as d: d.new_page(); d.save(path)
    result=asyncio.run(render(path,tmp_path,[]))
    assert result['status']=='ACCEPTED' and (tmp_path/'preview.png').read_bytes().startswith(b'\x89PNG')


def test_wall_timeout_kills_child(tmp_path):
    path=tmp_path/'source'; path.write_bytes(b'%PDF-1.7 invalid')
    with pytest.raises(TimeoutError): asyncio.run(render(path,tmp_path,[],timeout=0.00001))


def test_renderer_crash_is_infrastructure_failure(tmp_path):
    with pytest.raises(ProcessingFailure,match='RENDERER_CRASH'):
        asyncio.run(render(tmp_path/'missing',tmp_path,[]))


def test_linux_resource_enforcement():
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('feasibility',Path('scripts/validation_feasibility.py'))
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.main()
