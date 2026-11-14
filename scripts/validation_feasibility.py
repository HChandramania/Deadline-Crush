"""No credentials, uploads or network access: Linux parser isolation gate."""
import json
import resource
import subprocess
import sys
import tempfile
from pathlib import Path


def main():
    import pymupdf
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / 'fixture.pdf'
        with pymupdf.open() as document:
            document.new_page().insert_text((30, 30), 'Deadline Crush feasibility')
            document.save(path)
        child = '''import resource,sys
resource.setrlimit(resource.RLIMIT_AS,(268435456,268435456))
resource.setrlimit(resource.RLIMIT_CPU,(2,2))
resource.setrlimit(resource.RLIMIT_FSIZE,(2097152,2097152))
import pymupdf
with pymupdf.open(sys.argv[1]) as d:
 assert not d.is_encrypted and not d.is_repaired
 p=d[0].get_pixmap(matrix=pymupdf.Matrix(0.5,0.5),annots=False)
 assert p.tobytes('png').startswith(b'\\x89PNG')
'''
        subprocess.run([sys.executable, '-c', child, str(path)], check=True, timeout=8)
        cpu = subprocess.run([sys.executable, '-c',
            'import resource; resource.setrlimit(resource.RLIMIT_CPU,(1,1))\nwhile True: pass'],
            timeout=5, capture_output=True)
        assert cpu.returncode < 0
        memory = subprocess.run([sys.executable, '-c',
            'import resource; resource.setrlimit(resource.RLIMIT_AS,(67108864,67108864)); bytearray(134217728)'],
            timeout=5, capture_output=True)
        assert memory.returncode != 0 and b'MemoryError' in memory.stderr
        try:
            subprocess.run([sys.executable, '-c', 'import time; time.sleep(30)'], timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError('wall limit failed')
        print(json.dumps({'python': sys.version.split()[0], 'pymupdf': pymupdf.VersionBind,
            'render': 'passed', 'cpu_kill': 'passed', 'memory_limit': 'passed',
            'wall_kill': 'passed', 'scope': 'local Linux feasibility only'}))


if __name__ == '__main__':
    main()
