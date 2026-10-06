"""Airtable adapter. Existing remote records are authoritative; no SQLite bulk upload."""
import argparse
from datetime import date, datetime, timezone
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from .core import Nutrients, Portion, Goals


class SyncError(RuntimeError):
    pass


class Client:
    def __init__(self, base=None, token=None):
        self.base = base or os.environ.get('AIRTABLE_BASE_ID', '')
        self.token = token or os.environ.get('AIRTABLE_TOKEN', '')
        if not re.fullmatch(r'app[A-Za-z0-9]{14}', self.base):
            raise SyncError('Set AIRTABLE_BASE_ID to the base ID')
        if not self.token:
            raise SyncError('Set AIRTABLE_TOKEN in the environment, never in source code')
        self.last_call = 0

    def request(self, method, path, query=None, body=None):
        # Fixed host; credentials are never forwarded to arbitrary URLs.
        url = 'https://api.airtable.com/v0/' + path
        if query:
            url += '?' + urlencode(query)
        delay = .22 - (time.monotonic() - self.last_call)
        if delay > 0:
            time.sleep(delay)
        request = Request(url, method=method,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'})
        self.last_call = time.monotonic()
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            raise SyncError('Airtable HTTP '+str(exc.code)+'; check access, schema and values') from None
        except (URLError, TimeoutError, OSError):
            raise SyncError('Airtable network error; read back before retrying a write') from None

    def schema(self):
        return self.request('GET', 'meta/bases/'+self.base+'/tables')['tables']

    def records(self, table, formula=None):
        records, offset = [], None
        while True:
            query = {'pageSize': 100, 'returnFieldsByFieldId': 'true'}
            if formula:
                query['filterByFormula'] = formula
            if offset:
                query['offset'] = offset
            result = self.request('GET', self.base+'/'+table, query)
            records.extend(result['records'])
            offset = result.get('offset')
            if not offset:
                return records

    def write(self, table, fields, record_id=None):
        record = {'fields': fields}
        if record_id:
            record['id'] = record_id
        return self.request('PATCH' if record_id else 'POST', self.base+'/'+table,
                            body={'records': [record], 'typecast': False})


def same_value(key, left, right):
    if key == 'Approx' and right is False and left is None:
        return True
    if key == 'LastUpdate' and left and right:
        return datetime.fromisoformat(left.replace('Z', '+00:00')) == datetime.fromisoformat(right.replace('Z', '+00:00'))
    return select(left) == right


def select(value):
    return value.get('name') if isinstance(value, dict) else value


def named_records(records, table):
    names = {f['id']: f['name'] for f in table['fields']}
    return [{'id': r['id'], 'fields': {names.get(k, k): v for k, v in
            r.get('fields', r.get('cellValuesByFieldId', {})).items()}} for r in records]


def totals(rows, day):
    date.fromisoformat(day)
    seen = set()
    eaten, planned = Nutrients(), Nutrients()
    for row in rows:
        f = row['fields']
        if f.get('Date') != day:
            raise SyncError('Snapshot contains another date')
        key = f.get('MealItemKey')
        if not key or key in seen:
            raise SyncError('Missing or duplicate MealItemKey')
        seen.add(key)
        # Empty numeric cells must not silently become zero: unresolved data blocks sync.
        try:
            value = Nutrients(**{k: f[n] for k, n in zip(
                ('kcal', 'protein', 'fat', 'carbs'), ('Kcal', 'Protein', 'Fat', 'Carbs'))})
        except (KeyError, ValueError):
            raise SyncError('Missing or invalid nutrition values') from None
        status = select(f.get('Status'))
        if status == 'CONFIRMED':
            eaten += value
        elif status == 'PLANNED':
            planned += value
        else:
            raise SyncError('Unknown meal status')
    return eaten, eaten+planned


class AirtableJournal:
    def __init__(self, client):
        self.client = client
        self.tables = {t['name']: t for t in client.schema()}
        for name in ('Config', 'Meals', 'DailyState'):
            if name not in self.tables:
                raise SyncError('Missing table: '+name)

    def read_table(self, name, formula=None):
        table = self.tables[name]
        return named_records(self.client.records(table['id'], formula), table)

    def protocols(self):
        rows = self.read_table('Config')
        active = {r['fields'].get('Key'): r['fields'].get('Value') for r in rows if r['fields'].get('Active')}
        for key in ('NutritionSyncProtocol', 'NutritionCalculationProtocol'):
            if not active.get(key):
                raise SyncError('Missing active protocol: '+key)
        return active

    def read(self, day):
        day = date.fromisoformat(day).isoformat()
        protocols = self.protocols()
        state = self.read_table('DailyState', "{DayKey}='"+day+"'")
        if len(state) > 1:
            raise SyncError('Duplicate DailyState')
        rows = self.read_table('Meals', "IS_SAME({Date}, DATETIME_PARSE('"+day+"'), 'day')")
        eaten, forecast = totals(rows, day)
        return {'day': day, 'state': state[0] if state else None, 'meals': rows,
                'computed_eaten': eaten.values(True), 'computed_forecast': forecast.values(True),
                'protocols': protocols}

    def _write_verified(self, name, fields, record_id, key, value):
        table = self.tables[name]
        mapped = {f['id']: fields[f['name']] for f in table['fields'] if f['name'] in fields}
        if len(mapped) != len(fields):
            raise SyncError('Unknown field in '+name)
        for attempt in range(2):
            error = None
            try:
                self.client.write(table['id'], mapped, record_id)
            except SyncError as exc:
                error = exc
            rows = self.read_table(name)
            matches = [r for r in rows if r['fields'].get(key) == value]
            if len(matches) > 1:
                raise SyncError('Duplicate key after write')
            if matches:
                record_id = matches[0]['id']
                if all(same_value(k, matches[0]['fields'].get(k), v) for k, v in fields.items()):
                    return
            if attempt == 1:
                raise error or SyncError('Write read-back verification failed')

    def upsert(self, day, item, expected_version, correction=False):
        """Explicit one-position event. Never upload or overwrite an entire local day."""
        snapshot = self.read(day)
        state = snapshot['state']
        current = state['fields'] if state else {}
        if select(current.get('Status')) == 'CLOSED':
            raise SyncError('Closed day requires the separate ClosingProtocol correction flow')
        version = current.get('Version', 0)
        if version != expected_version:
            raise SyncError('Version conflict; reread the day')
        key = item['MealItemKey']
        if not re.fullmatch(re.escape(day)+r'\|[A-Za-z0-9_-]+\|[A-Za-z0-9_-]+', key):
            raise SyncError('MealItemKey must belong to the selected day')
        if item['Status'] not in ('PLANNED', 'CONFIRMED'):
            raise SyncError('Invalid status')
        if item['Meal'] not in ('BREAKFAST', 'LUNCH', 'SNACK', 'DINNER', 'EVENING', 'OTHER'):
            raise SyncError('Invalid meal')
        source_type = item.get('Source', 'ESTIMATE')
        if source_type not in ('LABEL', 'RECIPE', 'ESTIMATE'):
            raise SyncError('Source must be LABEL, RECIPE or ESTIMATE')
        portion = Portion.from_record(item['portion'])
        fields = dict(MealItemKey=key, Date=day, Meal=item['Meal'], Status=item['Status'],
                      Product=portion.name, Grams=float(portion.grams), Approx=portion.estimated,
                      Source=source_type, Notes=json.dumps(portion.record(), ensure_ascii=False))
        fields.update({n: float(v) for n, v in zip(('Kcal','Protein','Fat','Carbs'), portion.total.values().values())})
        old = next((r for r in snapshot['meals'] if r['fields']['MealItemKey'] == key), None)
        if old and all(same_value(k, old['fields'].get(k), v) for k,v in fields.items()):
            # Do not return before checking/recovering aggregates after a partial write.
            changed = False
        else:
            changed = True
            if old and select(old['fields'].get('Status')) == 'CONFIRMED' and not correction:
                raise SyncError('Confirmed item requires explicit correction')
        if changed:
            fields['LastUpdate'] = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
            self._write_verified('Meals', fields, old['id'] if old else None, 'MealItemKey', key)
        after = self.read(day)
        latest = after['state']['fields'] if after['state'] else {}
        if latest.get('Version', 0) != version or select(latest.get('Status')) == 'CLOSED':
            raise SyncError('Concurrent change detected; meals may be saved, reread before retry')
        aggregate = {}
        for prefix, data in [('Eaten', after['computed_eaten']), ('Forecast', after['computed_forecast'])]:
            aggregate.update({prefix+n: float(data[k]) for k,n in zip(
                ('kcal','protein','fat','carbs'), ('Kcal','Protein','Fat','Carbs'))})
        dirty = any(latest.get(k) != v for k,v in aggregate.items())
        if changed or dirty or not after['state']:
            aggregate.update(DayKey=day, Date=day, Version=version+1,
                             LastUpdate=datetime.now(timezone.utc).isoformat(timespec='milliseconds'))
            if not after['state']:
                aggregate.update(Status='ACTIVE', CaloriesGoal=1957, ProteinMin=150,
                                 ProteinMax=160, FatMin=70, FatMax=80, CarbMax=170)
            # Preserve points, closing state, existing goals and human plan summary.
            self._write_verified('DailyState', aggregate, after['state']['id'] if after['state'] else None,
                                 'DayKey', day)
        return self.read(day)


def main():
    parser = argparse.ArgumentParser(description='Airtable: чтение и явная запись позиции')
    parser.add_argument('--day', default=datetime.now(ZoneInfo(
        os.environ.get('NUTRITION_TIMEZONE', 'Asia/Yekaterinburg'))).date().isoformat())
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('read')
    write = commands.add_parser('upsert')
    write.add_argument('file')
    write.add_argument('--expected-version', type=int, required=True)
    write.add_argument('--correction', action='store_true')
    args = parser.parse_args()
    try:
        remote = AirtableJournal(Client())
        if args.command == 'read':
            result = remote.read(args.day)
        else:
            with open(args.file, encoding='utf-8') as file:
                item = json.load(file)
            result = remote.upsert(args.day, item, args.expected_version, args.correction)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (SyncError, ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
