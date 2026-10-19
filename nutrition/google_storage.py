"""Owner-only durable diary and asynchronous Google Sheets mirror.

One active deployment owns its dedicated BotJournal sheet. Never target the
personal ChatGPT diary or share a sheet between independent writer instances.
"""
from datetime import date, datetime, timezone
from contextlib import contextmanager
import json
import os
import re
import sqlite3
from threading import Event, Thread
from .airtable import SyncError, totals
from .core import Portion

HEADERS = ['AccountKey', 'Day', 'Version', 'SnapshotJson', 'UpdatedAt']
PROTOCOLS = {
    'NutritionSyncProtocol': 'Local durable diary; Google mirror runs separately. Never claim remote sync without confirmation.',
    'NutritionCalculationProtocol': 'CONFIRMED is eaten; PLANNED is forecast only. Unknown is not zero. Mark estimates.'}


class LocalGoogleJournal:
    storage_label = 'локальном дневнике; передача в Google Таблицу ожидает фоновой сверки'

    def __init__(self, database=None, account=None):
        self.database = database or os.environ.get('CHANNEL_DB', '/app/data/channels.sqlite3')
        self.account = account or 'telegram:' + os.environ.get('TELEGRAM_OWNER_ID', '')
        if not self.account.split(':')[-1]:
            raise SyncError('Diary account is missing')
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS google_diary(
                id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
                day TEXT NOT NULL, version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                updated TEXT NOT NULL, synced_version INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', UNIQUE(account,day))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _empty(self, day):
        return dict(storage_notice='Сохранено локально; синхронизация с Google проверяется отдельно', day=day, state=None, meals=[], computed_eaten={k:'0' for k in ('kcal','protein','fat','carbs')},
                    computed_forecast={k:'0' for k in ('kcal','protein','fat','carbs')}, protocols=PROTOCOLS)

    def read(self, day):
        day = date.fromisoformat(day).isoformat()
        with self.connect() as db:
            row = db.execute('SELECT snapshot,synced_version,version FROM google_diary WHERE account=? AND day=?', (self.account, day)).fetchone()
        snapshot = json.loads(row[0]) if row else self._empty(day)
        snapshot['google_sync'] = ('SYNCED' if row[1] == row[2] else 'PENDING') if row else 'NO_DATA'
        return snapshot

    def upsert(self, day, item, expected_version, correction=False):
        day = date.fromisoformat(day).isoformat()
        key = item['MealItemKey']
        if not re.fullmatch(re.escape(day)+r'\|[A-Za-z0-9_-]+\|[A-Za-z0-9_-]+', key):
            raise SyncError('Meal key must belong to selected day')
        if item['Status'] not in ('PLANNED', 'CONFIRMED') or item['Meal'] not in ('BREAKFAST','LUNCH','SNACK','DINNER','EVENING','OTHER'):
            raise SyncError('Invalid meal/status')
        if item.get('Source', 'ESTIMATE') not in ('LABEL','RECIPE','ESTIMATE'):
            raise SyncError('Invalid nutrition source')
        portion = Portion.from_record(item['portion'])
        fields = dict(MealItemKey=key, Date=day, Meal=item['Meal'], Status=item['Status'],
                      Product=portion.name, Grams=float(portion.grams), Approx=portion.estimated,
                      Source=item.get('Source','ESTIMATE'), Notes=json.dumps(portion.record(),ensure_ascii=False))
        fields.update(dict(zip(('Kcal','Protein','Fat','Carbs'),map(float,portion.total.values().values()))))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT snapshot FROM google_diary WHERE account=? AND day=?', (self.account,day)).fetchone()
            snapshot = json.loads(row[0]) if row else self._empty(day)
            state = snapshot['state']['fields'] if snapshot['state'] else {}
            if state.get('Status') == 'CLOSED':
                raise SyncError('Closed day requires explicit correction flow')
            if state.get('Version',0) != expected_version:
                raise SyncError('Version conflict; reread the day')
            old = next((r for r in snapshot['meals'] if r['fields']['MealItemKey']==key), None)
            if old and all(old['fields'].get(k)==v for k,v in fields.items()):
                return snapshot
            if old and old['fields']['Status']=='CONFIRMED' and not correction:
                raise SyncError('Confirmed item requires explicit correction')
            now = datetime.now(timezone.utc).isoformat()
            fields['LastUpdate'] = now
            if old:
                old['fields'] = fields
            else:
                snapshot['meals'].append({'id':key,'fields':fields})
            eaten, forecast = totals(snapshot['meals'],day)
            snapshot['computed_eaten'], snapshot['computed_forecast'] = eaten.values(), forecast.values()
            state = dict(state or dict(Status='ACTIVE',CaloriesGoal=1957,ProteinMin=150,ProteinMax=160,FatMin=70,FatMax=80,CarbMax=170))
            state.update(DayKey=day,Date=day,Version=expected_version+1,LastUpdate=now)
            for prefix, nutrients in [('Eaten',eaten),('Forecast',forecast)]:
                state.update(dict(zip((prefix+n for n in ('Kcal','Protein','Fat','Carbs')),map(float,nutrients.values().values()))))
            snapshot['state'] = {'id':day,'fields':state}
            payload = json.dumps(snapshot,ensure_ascii=False,allow_nan=False)
            if len(payload)>44000:
                raise SyncError('Day exceeds Google mirror cell limit')
            # Diary, revision and dirty marker commit together; no Google call here.
            db.execute('''INSERT INTO google_diary(account,day,version,snapshot,updated)
                VALUES(?,?,?,?,?) ON CONFLICT(account,day) DO UPDATE SET
                version=excluded.version,snapshot=excluded.snapshot,updated=excluded.updated,last_error='' ''',
                (self.account,day,expected_version+1,payload,now))
        return snapshot

    def pending(self):
        with self.connect() as db:
            return db.execute('SELECT id,account,day,version,snapshot,updated FROM google_diary WHERE version>synced_version ORDER BY id LIMIT 20').fetchall()

    def sync_once(self, transport):
        count = 0
        for ident, account, day, version, payload, updated in self.pending():
            values = [account,day,version,payload,updated]
            try:
                # Fixed row is reused after timeout/restart: never blind append.
                transport.put_verified(ident+1, values)
            except Exception:
                with self.connect() as db:
                    db.execute("UPDATE google_diary SET last_error='Google sync failed; retry pending' WHERE id=?",(ident,))
                continue
            with self.connect() as db:
                # A newer local version stays dirty if it arrived during export.
                db.execute("UPDATE google_diary SET synced_version=?,last_error='' WHERE id=? AND version=?",(version,ident,version))
            count += 1
        return count


class GoogleMirror:
    """Explicitly provisioned dedicated sheet, no sharing/creation side effects."""
    def __init__(self):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        self.sheet = os.environ.get('GOOGLE_BOT_SPREADSHEET_ID','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,150}', self.sheet):
            raise SyncError('Set dedicated GOOGLE_BOT_SPREADSHEET_ID')
        if self.sheet in ('1qhZwREFjkHbk0mYwy6iWsXOhVfwHEMnVc0wPXm9f1ag','1SCBdbEkokcWXa2-s5ME0wzPqBSIDpM6TY0gkjhap_Zc'):
            raise SyncError('Personal ChatGPT diary is not a bot mirror')
        credentials = service_account.Credentials.from_service_account_file(
            os.environ['GOOGLE_APPLICATION_CREDENTIALS'], scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.session = AuthorizedSession(credentials)
        self.base = 'https://sheets.googleapis.com/v4/spreadsheets/'+self.sheet+'/values/'
        self.verified = False

    def get(self, cells):
        r = self.session.get(self.base+cells,timeout=15)
        r.raise_for_status()
        return r.json().get('values',[])

    def put_verified(self, row, values):
        if not self.verified:
            if self.get('BotJournal!A1:E1') != [HEADERS]:
                raise SyncError('Dedicated BotJournal header mismatch')
            self.verified = True
        cell = 'BotJournal!A'+str(row)+':E'+str(row)
        existing = self.get(cell)
        if existing and (existing[0][:2] != values[:2] or int(existing[0][2])>values[2] or (int(existing[0][2])==values[2] and existing[0][3]!=values[3])):
            raise SyncError('Mirror row conflict; do not overwrite')
        r = self.session.put(self.base+cell,params={'valueInputOption':'RAW'},json={'values':[values]},timeout=15)
        r.raise_for_status()
        expected = [str(v) for v in values]
        if [[str(v) for v in r] for r in self.get(cell)] != [expected]:
            raise SyncError('Google readback mismatch')


def start_exporter(journal, transport, interval=30):
    stop = Event()
    def run():
        while not stop.is_set():
            try:
                journal.sync_once(transport)
            except Exception:
                pass  # Pending data remains durable; no credentials in logs.
            stop.wait(interval)
    thread = Thread(target=run, name='google-diary-export', daemon=True)
    thread.start()
    return stop, thread
