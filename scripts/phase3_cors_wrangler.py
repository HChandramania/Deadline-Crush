"""Operator-only approved CORS change using an existing Wrangler OAuth session.

Credentials stay in memory. No new token, permissions, or auth provider is created.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tomllib
from urllib.parse import quote

import httpx
from deadline_crush_spikes.settings import Settings


def run(apply=False):
    settings=Settings.load()
    if not re.fullmatch(r'[a-fA-F0-9]{32}',settings.R2_ACCOUNT_ID) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',settings.R2_BUCKET):
        raise RuntimeError('Invalid R2 configuration')
    # Refresh through Wrangler itself; never implement credential issuance here.
    config=tomllib.loads((Path.home()/'.wrangler/config/default.toml').read_text())
    expires=datetime.fromisoformat(str(config.get('expiration_time','2000-01-01T00:00:00Z')).replace('Z','+00:00'))
    if expires<=datetime.now(timezone.utc):
        raise RuntimeError('Existing Wrangler session expired; refresh with npx wrangler login')
    token=config.get('oauth_token')
    if not token: raise RuntimeError('No existing Wrangler OAuth session')
    endpoint=f'https://api.cloudflare.com/client/v4/accounts/{settings.R2_ACCOUNT_ID}/r2/buckets/{quote(settings.R2_BUCKET,safe="")}/cors'
    report={'origin':'http://localhost:5173','approved_by_user':True,'initial_s3_error':'AccessDenied','changes':[]}
    def record(): Path('docs/phase3-cors-results.json').write_text(json.dumps(report,indent=2)+'\n')
    with httpx.Client(headers={'Authorization':'Bearer '+token},timeout=20,follow_redirects=False) as client:
        def request(method,body=None):
            response=client.request(method,endpoint,json=body)
            if response.status_code!=200:
                try:
                    errors=response.json().get('errors',[])
                except ValueError:
                    errors=[]
                messages=' '.join(str(e.get('message','')) for e in errors)
                # Distinguish absent policy from missing bucket/account authority.
                if response.status_code==404 and method=='GET' and 'cors' in messages.lower() and any(
                    word in messages.lower() for word in ['does not exist','not found','no cors']):
                    return {'rules':[]}
                codes=','.join(str(e.get('code','')) for e in errors)
                safe=re.sub(r'https?://\S+','[URL]',messages)
                safe=re.sub(r'[A-Za-z0-9_/-]{20,}','[REDACTED]',safe)
                raise RuntimeError('Cloudflare CORS API rejected operation (HTTP '+str(response.status_code)+', code '+codes+'): '+safe[:180])
            result=response.json()
            if not result.get('success'): raise RuntimeError('Cloudflare CORS API reported failure')
            return result.get('result')
        before=request('GET')
        if not isinstance(before,dict) or not isinstance(before.get('rules'),list):
            raise RuntimeError('Unexpected CORS policy shape')
        report['before']=before
        if any('*' in origin for rule in before['rules'] for origin in rule.get('allowed',{}).get('origins',[])):
            record(); raise RuntimeError('Existing wildcard origin requires separate review; no CORS changed')
        added={'id':'deadline-crush-phase3-localhost','allowed':{'origins':['http://localhost:5173'],'methods':['PUT','HEAD'],'headers':['Content-Type']},
               'exposeHeaders':['ETag'],'maxAgeSeconds':120}
        after=before if added in before['rules'] else {**before,'rules':[added,*before['rules']]}
        report['proposed_after']=after; record()
        if not apply:
            print('Current CORS snapshot and additive proposal recorded; no change')
            return
        if request('GET')!=before: raise RuntimeError('CORS changed concurrently; no policy written')
        if after!=before:
            request('PUT',after)
            report['changes']=['Added exact localhost PUT/HEAD Content-Type/ETag rule; preserved existing rules']
        observed=request('GET'); report['after']=observed; record()
        if not all(rule in observed['rules'] for rule in before['rules']) or added not in observed['rules']:
            raise RuntimeError('CORS readback differs from approved additive configuration')
        report['configuration_verification']='PASS'; record()
        print('Approved additive R2 CORS update and preservation check: PASS')


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--apply',action='store_true'); args=parser.parse_args()
    try: run(args.apply)
    except RuntimeError as exc: raise SystemExit(str(exc)) from None
    except Exception: raise SystemExit('CORS operator failed; private dependency details omitted') from None
