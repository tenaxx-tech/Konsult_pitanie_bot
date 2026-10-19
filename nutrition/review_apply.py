"""Apply a reviewed, hash-bound correction from a dedicated staging tab."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from .diary_service import mutate
from .google_storage import GoogleMirror,LocalGoogleJournal


def snapshot_hash(snapshot):
    return hashlib.sha256(json.dumps(dict(state=snapshot['state'],meals=snapshot['meals']),sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def apply(journal, pack):
    if pack['account']!=journal.account:raise ValueError('Review belongs to another account')
    snapshot=journal.read(pack['day'])
    with journal.connect() as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='diary_operations'").fetchone()
        done=db.execute('SELECT 1 FROM diary_operations WHERE account=? AND event=?',(journal.account,pack['operation'])).fetchone() if exists else None
    if not done and snapshot_hash(snapshot)!=pack['before_sha256']:
        raise ValueError('Diary changed after review; correction refused')
    backup=str(Path(journal.database).with_name('review-backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3'))
    with journal.connect() as source,sqlite3.connect(backup) as target:source.backup(target)
    Path(backup).chmod(0o600)
    result=mutate(journal,pack['day'],'replace',pack['operation'],pack['expected_version'],items=pack['items'])
    return dict(result='APPLIED' if not done else 'ALREADY_APPLIED',day=pack['day'],
                version=result['state']['fields']['Version'],positions=len(result['meals']),
                totals=result['computed_eaten'],backup=backup)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply',action='store_true',required=True);p.parse_args()
    try:
        values=GoogleMirror().get('ReviewSource!A1:C2')
        if values[0]!=['Day','SHA256','ReviewJson'] or len(values)!=2:raise ValueError('Missing reviewed correction')
        day,digest,raw=values[1]
        if hashlib.sha256(raw.encode()).hexdigest()!=digest:raise ValueError('Review checksum mismatch')
        pack=json.loads(raw)
        if day!=pack['day']:raise ValueError('Review date mismatch')
        print(json.dumps(apply(LocalGoogleJournal(),pack),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(dict(result='FAIL',error_type=type(exc).__name__)))
        raise SystemExit(1) from None


if __name__=='__main__':main()
