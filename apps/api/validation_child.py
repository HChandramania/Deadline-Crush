"""Killable Linux execution boundary. No application secrets passed to child."""
import json
import sys
from pathlib import Path


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_AS,(384*1024*1024,384*1024*1024))
    resource.setrlimit(resource.RLIMIT_CPU,(10,10))
    resource.setrlimit(resource.RLIMIT_FSIZE,(1024*1024,1024*1024))
    resource.setrlimit(resource.RLIMIT_NOFILE,(32,32))
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from file_validation import InvalidContent, validate
    output = Path(sys.argv[2])
    try:
        result = {'status':'ACCEPTED','findings':[],**validate(Path(sys.argv[1]),output)}
    except InvalidContent as exc:
        result = {'status':'NEEDS_FIX','findings':[str(exc)]}
    except MemoryError:
        result = {'status':'NEEDS_FIX','findings':['FILE_RESOURCE_LIMIT']}
    if len(sys.argv)>3:
        from link_validation import check_link
        links = json.loads(Path(sys.argv[3]).read_text(encoding='utf-8'))
        if len(links)>10:
            raise ValueError('Invalid link count')
        result['links'] = [{'index':i,'outcome':check_link(url)} for i,url in enumerate(links)]
        if any(link['outcome']=='TEMPORARILY_UNAVAILABLE' for link in result['links']):
            result['transient_error'] = 'LINK_TEMPORARY'
        for link in result['links']:
            if link['outcome'] in ('BLOCKED_SECURITY','INVALID','UNREACHABLE'):
                result['status'] = 'NEEDS_FIX'
                result['findings'].append(f"LINK_{link['index']}_{link['outcome']}")
    (output/'result.json').write_text(json.dumps(result),encoding='utf-8')


if __name__=='__main__':
    main()
