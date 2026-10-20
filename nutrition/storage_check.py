"""Explicit live check using one reserved synthetic account and day."""
import argparse
import json
from .core import Nutrients, Portion
from .google_storage import GoogleMirror, HEADERS, LocalGoogleJournal

ACCOUNT = 'TEST_ONLY:storage-check-v1'
DAY = '2099-01-01'


class TestJournal(LocalGoogleJournal):
    def pending(self):
        # Never export another account as a side effect of this diagnostic.
        with self.connect() as db:
            return db.execute('SELECT id,account,day,version,snapshot,updated '
                              'FROM google_diary WHERE account=? AND day=? '
                              'AND version>synced_version', (ACCOUNT, DAY)).fetchall()


def run(database=None, transport=None):
    mirror = transport if transport is not None else GoogleMirror()
    if mirror.get('BotJournal!A1:E1') != [HEADERS]:
        raise RuntimeError('BotJournal headers do not match')
    journal = TestJournal(database, ACCOUNT)
    item = dict(MealItemKey=DAY+'|OTHER|storage_check_v1', Meal='OTHER',
                Status='PLANNED', Source='ESTIMATE',
                portion=Portion('TEST_ONLY storage check', 100,
                                Nutrients(100, 10, 4, 6), 'TEST_ONLY',
                                estimated=True).record())
    before = journal.read(DAY)
    version = before['state']['fields']['Version'] if before['state'] else 0
    journal.upsert(DAY, item, version)
    saved = journal.read(DAY)
    revision = saved['state']['fields']['Version']
    journal.upsert(DAY, item, revision)
    repeated = journal.read(DAY)
    if len(repeated['meals']) != 1 or repeated['state']['fields']['Version'] != revision:
        raise RuntimeError('Local duplicate check failed')
    restarted = TestJournal(journal.database, ACCOUNT)
    if len(restarted.read(DAY)['meals']) != 1:
        raise RuntimeError('Database reopen check failed')
    restarted.sync_once(mirror)
    if restarted.read(DAY)['google_sync'] != 'SYNCED':
        raise RuntimeError('Google export remains pending')
    with restarted.connect() as db:
        row = db.execute('SELECT id,account,day,version,snapshot,updated FROM '
                         'google_diary WHERE account=? AND day=?', (ACCOUNT, DAY)).fetchone()
    ident, *values = row
    # Resending exactly the same revision must reuse the same remote row.
    mirror.put_verified(ident+1, values)
    actual = mirror.get('BotJournal!A'+str(ident+1)+':E'+str(ident+1))
    if actual != [[str(v) for v in values]]:
        raise RuntimeError('Final Google readback failed')
    return dict(result='PASS', account=ACCOUNT, day=DAY, row=ident+1,
                version=revision, positions=1, google='SYNCED',
                checks=['local_save', 'local_repeat', 'database_reopen',
                        'google_write_readback', 'remote_repeat'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-test', action='store_true', required=True,
                        help='Write the reserved TEST_ONLY record; preserve it for reruns')
    parser.parse_args()
    try:
        print(json.dumps(run(), ensure_ascii=False))
    except Exception as exc:
        # Exception messages can contain server details; never print credentials.
        print(json.dumps(dict(result='FAIL', error_type=type(exc).__name__)))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
