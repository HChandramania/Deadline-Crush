"""Explicitly approved additive localhost CORS change; no permission/public changes."""
import argparse
import json
from pathlib import Path
from botocore.exceptions import ClientError
from deadline_crush_spikes.settings import Settings
from storage import create_client


def read_rules(client,bucket):
    try:
        return client.get_bucket_cors(Bucket=bucket).get('CORSRules',[])
    except ClientError as exc:
        if exc.response.get('Error',{}).get('Code')=='NoSuchCORSConfiguration':
            return []
        raise


def run(apply=False):
    settings=Settings.load(); client=create_client(settings)
    if client is None: raise RuntimeError('R2 configuration absent')
    report={'origin':'http://localhost:5173','approved_by_user':True,'changes':[]}
    try:
        before=read_rules(client,settings.R2_BUCKET)
        report['before']={'CORSRules':before}
        if any('*' in origin for rule in before for origin in rule.get('AllowedOrigins',[])):
            raise RuntimeError('Existing wildcard rule requires separate review; no policy changed')
        added={'AllowedOrigins':['http://localhost:5173'],'AllowedMethods':['PUT','HEAD'],
               'AllowedHeaders':['Content-Type'],'ExposeHeaders':['ETag'],'MaxAgeSeconds':120}
        after=before.copy()
        if added not in after: after.append(added)
        report['proposed_after']={'CORSRules':after}
        # Save the snapshot BEFORE mutation. These are origins/rules only.
        Path('docs/phase3-cors-results.json').write_text(json.dumps(report,indent=2)+'\n')
        if apply:
            # Re-read to detect intervening changes; the S3 API has no CORS CAS.
            if read_rules(client,settings.R2_BUCKET)!=before:
                raise RuntimeError('CORS changed concurrently; no policy written')
            if after!=before:
                client.put_bucket_cors(Bucket=settings.R2_BUCKET,CORSConfiguration={'CORSRules':after})
                report['changes']=['Appended exact localhost PUT/HEAD Content-Type/ETag rule']
            observed=read_rules(client,settings.R2_BUCKET)
            report['after']={'CORSRules':observed}
            assert all(rule in observed for rule in before),'Existing CORS rules changed'
            assert added in observed,'Approved localhost rule not present'
            report['configuration_verification']='PASS'
        print('R2 CORS configuration: PASS' if apply else 'R2 CORS snapshot recorded; no changes')
    except ClientError as exc:
        report['failure_category']=exc.response.get('Error',{}).get('Code','R2_ERROR')
        raise RuntimeError('R2 CORS control unavailable: '+report['failure_category']) from None
    finally:
        client.close()
        Path('docs/phase3-cors-results.json').write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--apply',action='store_true'); args=parser.parse_args()
    try: run(args.apply)
    except Exception as exc:
        print(str(exc) if isinstance(exc,RuntimeError) else 'R2 CORS verification failed')
        raise SystemExit(1) from None
