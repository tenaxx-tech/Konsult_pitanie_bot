"""Import validated historical days from the dedicated mirror's staging tab."""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from .channels import today
from .core import number
from .google_storage import GoogleMirror, LocalGoogleJournal, PROTOCOLS

SOURCE_ID = '1qhZwREFjkHbk0mYwy6iWsXOhVfwHEMnVc0wPXm9f1ag'
HEADER = ['Day', 'SHA256', 'SourceJson']
NUTRIENTS = ('Kcal', 'Protein', 'Fat', 'Carbs')


def numeric(value):
    return number(str(value).replace(',', '.'))


def prepare(rows, account, cutoff, expected_days):
    if not rows or rows[0] != HEADER or len(rows)-1 != expected_days:
        raise ValueError('Migration manifest is incomplete')
    prepared = {}
    for row in rows[1:]:
        if len(row) != 3:
            raise ValueError('Invalid migration row')
        day, digest, raw = row
        if hashlib.sha256(raw.encode()).hexdigest() != digest:
            raise ValueError('Migration checksum mismatch')
        if date.fromisoformat(day) >= date.fromisoformat(cutoff) or day in prepared:
            raise ValueError('Current/future or duplicate day refused')
        pack = json.loads(raw)
        if pack['sourceId'] != SOURCE_ID or pack['account'] != account or pack['day'] != day:
            raise ValueError('Migration source/account mismatch')
        state = dict(pack['state'])
        if state['Date'] != day or state['DayKey'] != day or state['Status'] not in ('CLOSED', 'ACTIVE', 'OPEN'):
            raise ValueError('Invalid source day state')
        version = int(state['Version'])
        if numeric(state['Version']) != version or version < 1:
            raise ValueError('Invalid source version')
        state['Version'] = version
        for field in ('CaloriesGoal', 'ProteinMin', 'ProteinMax', 'FatMin', 'FatMax', 'CarbMax'):
            state[field] = float(numeric(state[field]))
        meals, keys = [], set()
        eaten = [Decimal(0)]*4
        forecast = [Decimal(0)]*4
        for source in pack['meals']:
            fields = dict(source)
            key = fields['MealItemKey']
            if fields['Date'] != day or not isinstance(key, str) or not key.strip() or key in keys:
                raise ValueError('Invalid/duplicate source portion')
            keys.add(key)
            if fields['Status'] not in ('CONFIRMED', 'PLANNED') or not fields['Product']:
                raise ValueError('Invalid source food/status')
            if fields['Grams'] in ('', None):
                fields['Grams'] = None  # Historical unknown is never invented or zero.
            else:
                grams = numeric(fields['Grams'])
                if not grams:
                    raise ValueError('Invalid source mass')
                fields['Grams'] = float(grams)
            values = [numeric(fields[k]) for k in NUTRIENTS]
            for index, (field, value) in enumerate(zip(NUTRIENTS, values)):
                fields[field] = float(value)
                forecast[index] += value
                if fields['Status'] == 'CONFIRMED':
                    eaten[index] += value
            approx = fields.get('Approx')
            if approx not in (True, False, 'TRUE', 'FALSE', '', None):
                raise ValueError('Invalid approximation marker')
            fields['Approx'] = None if approx in ('', None) else approx in (True, 'TRUE')
            meals.append(dict(id=key, fields=fields))
        if not meals:
            raise ValueError('Empty historical day refused')
        for prefix, values in (('Eaten', eaten), ('Forecast', forecast)):
            for field, value in zip(NUTRIENTS, values):
                if abs(numeric(state[prefix+field])-value) > Decimal('0.01'):
                    raise ValueError('Source totals require reconciliation')
                # Preserve the verified source aggregate and all historical notes/points.
                state[prefix+field] = float(numeric(state[prefix+field]))
        snapshot = dict(day=day, state=dict(id=day, fields=state), meals=meals,
                        computed_eaten=dict(zip(('kcal','protein','fat','carbs'),map(str,eaten))),
                        computed_forecast=dict(zip(('kcal','protein','fat','carbs'),map(str,forecast))),
                        protocols=PROTOCOLS, storage_notice='История импортирована; статус Google проверяется отдельно',
                        import_source=SOURCE_ID, import_sha256=digest)
        payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False)
        if len(payload) > 44000:
            raise ValueError('Imported day exceeds mirror cell limit')
        prepared[day] = (version, payload)
    return prepared


def install(database, account, prepared, cutoff, apply=False):
    journal = LocalGoogleJournal(database, account)
    backup = None
    if apply:
        backup = str(Path(database).with_name('history-backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3'))
        with journal.connect() as source, sqlite3.connect(backup) as target:
            source.backup(target)
        Path(backup).chmod(0o600)
    with journal.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        untouched = db.execute('SELECT day,version,snapshot FROM google_diary WHERE account=? AND day>=? ORDER BY day', (account, cutoff)).fetchall()
        additions = []
        skipped = 0
        for day, (version, payload) in sorted(prepared.items()):
            if day >= cutoff:
                raise ValueError('Current/future day refused')
            existing = db.execute('SELECT snapshot FROM google_diary WHERE account=? AND day=?', (account, day)).fetchone()
            if existing:
                if json.loads(existing[0]) != json.loads(payload):
                    raise ValueError('Existing historical day conflicts; nothing overwritten')
                skipped += 1
            else:
                additions.append((account, day, version, payload, datetime.now(timezone.utc).isoformat()))
        if apply:
            db.executemany('INSERT INTO google_diary(account,day,version,snapshot,updated) VALUES(?,?,?,?,?)', additions)
        after = db.execute('SELECT day,version,snapshot FROM google_diary WHERE account=? AND day>=? ORDER BY day', (account, cutoff)).fetchall()
        if after != untouched:
            raise RuntimeError('Current/future diary changed')
    return dict(result='APPLIED' if apply else 'DRY_RUN', days=len(prepared),
                added=len(additions) if apply else 0, would_add=len(additions), skipped=skipped,
                portions=sum(len(json.loads(v[1])['meals']) for v in prepared.values()),
                current_days_unchanged=True, backup=backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-days', type=int, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        journal = LocalGoogleJournal()
        rows = GoogleMirror().get('MigrationSource!A1:C100')
        cutoff = today()
        prepared = prepare(rows, journal.account, cutoff, args.expect_days)
        print(json.dumps(install(journal.database, journal.account, prepared, cutoff, args.apply), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps(dict(result='FAIL', error_type=type(exc).__name__)))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
