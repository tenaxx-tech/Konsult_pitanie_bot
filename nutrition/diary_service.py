"""Versioned atomic diary operations, independent of model/network calls."""
from copy import deepcopy
from datetime import datetime, timezone, date
from decimal import Decimal
import hashlib
import json
from .airtable import SyncError, totals
from .core import Portion, number


def fields_for(day, item):
    portion=Portion.from_record(item['portion'])
    if item['Status'] not in ('PLANNED','CONFIRMED') or item['Meal'] not in ('BREAKFAST','LUNCH','DINNER','SNACK','EVENING','OTHER'):
        raise SyncError('Неверный статус или приём пищи')
    if not item['MealItemKey'].startswith(day+'|'):
        raise SyncError('Ключ порции относится к другой дате')
    n=portion.total
    energy=4*n.protein+9*n.fat+4*n.carbs
    if abs(energy-n.kcal)>max(Decimal(25),n.kcal*Decimal('.2')):
        raise SyncError('Ккал и БЖУ порции не согласуются; требуется уточнить источник')
    if n.protein+n.fat+n.carbs>portion.grams*Decimal('1.05'):
        raise SyncError('БЖУ превышает массу порции')
    f=dict(Date=day,MealItemKey=item['MealItemKey'],Meal=item['Meal'],Status=item['Status'],
           Product=portion.name,Grams=float(portion.grams),Approx=portion.estimated,
           Source=item.get('Source','ESTIMATE'),Notes=json.dumps(portion.record(),ensure_ascii=False))
    f.update(dict(zip(('Kcal','Protein','Fat','Carbs'),map(float,n.values().values()))))
    return f


def mutate(journal, day, action, event, expected_version, **params):
    date.fromisoformat(day)
    request=json.dumps(dict(day=day,action=action,expected_version=None if action=='add' else expected_version,params=params),
                       ensure_ascii=False,sort_keys=True,allow_nan=False)
    fingerprint=hashlib.sha256(request.encode()).hexdigest()
    now=datetime.now(timezone.utc).isoformat()
    with journal.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS diary_operations(
            account TEXT,event TEXT,fingerprint TEXT NOT NULL,PRIMARY KEY(account,event))''')
        db.execute('BEGIN IMMEDIATE')
        operation=db.execute('SELECT fingerprint FROM diary_operations WHERE account=? AND event=?',(journal.account,event)).fetchone()
        row=db.execute('SELECT snapshot FROM google_diary WHERE account=? AND day=?',(journal.account,day)).fetchone()
        snapshot=json.loads(row[0]) if row else journal._empty(day)
        if operation:
            if operation[0]!=fingerprint:raise SyncError('Ключ операции уже использован с другими данными')
            return snapshot
        state=snapshot['state']['fields'] if snapshot['state'] else {}
        if state.get('Version',0)!=expected_version:raise SyncError('Версия дня изменилась. Перечитайте /day и повторите выбор.')
        if state.get('Status')=='CLOSED' and action not in ('correct','replace','close'):
            raise SyncError('День закрыт. Для изменения используйте явное исправление /correct.')
        original=deepcopy(snapshot)
        meals=snapshot['meals']
        if action in ('add','replace'):
            validated=[fields_for(day,item) for item in params['items']]
            keys=[f['MealItemKey'] for f in validated]
            if len(keys)!=len(set(keys)) or not keys:raise SyncError('Повтор ключа или пустой список продуктов')
            for f in validated:
                old=next((r for r in meals if r['fields']['MealItemKey']==f['MealItemKey']),None)
                if action=='replace' and not old:raise SyncError('Исправляемая порция не найдена')
                if old:
                    same=all(old['fields'].get(k)==v for k,v in f.items())
                    confirmation=(old['fields']['Status']=='PLANNED' and f['Status']=='CONFIRMED'
                                  and all(old['fields'].get(k)==v for k,v in f.items() if k!='Status'))
                    if not same and action=='add' and not confirmation:raise SyncError('Порция уже существует; используйте исправление')
                    if not same:old['fields'].update(f,LastUpdate=now)
                else:meals.append(dict(id=f['MealItemKey'],fields=dict(f,LastUpdate=now)))
        elif action in ('correct','confirm','cancel'):
            target=next((r for r in meals if r['fields']['MealItemKey']==params['item_key']),None)
            if not target:raise SyncError('Порция не найдена')
            f=target['fields']
            if action=='cancel':
                if f['Status']!='PLANNED':raise SyncError('Отменять можно только план')
                f.update(Status='CANCELLED',LastUpdate=now)
            elif action=='confirm' and f['Status']=='CONFIRMED' and params.get('grams') is None:
                pass
            else:
                if action=='confirm' and f['Status']!='PLANNED':raise SyncError('Эта порция не является планом')
                if f.get('Grams') is None:raise SyncError('Исходный вес неизвестен; нужен пересчёт по источнику')
                old_grams=number(f['Grams'])
                grams=number(params.get('grams') if params.get('grams') is not None else old_grams)
                if grams<=0 or grams>10000:raise SyncError('Укажите массу от 0 до 10000 г')
                if action=='confirm' and grams>old_grams:raise SyncError('Съедено больше плана: подтвердите план и добавьте отдельную порцию')
                if action=='confirm' and grams<old_grams:
                    remainder=deepcopy(f)
                    key=f['MealItemKey']+'-remaining-'+hashlib.sha256(event.encode()).hexdigest()[:12]
                    remainder.update(MealItemKey=key,Grams=float(old_grams-grams),LastUpdate=now)
                    for k in ('Kcal','Protein','Fat','Carbs'):remainder[k]=float(number(f[k])*(old_grams-grams)/old_grams)
                    remainder['PreviousNotes']=remainder.get('Notes','')
                    remainder['Notes']='Остаток плана после частичного подтверждения; КБЖУ пересчитаны пропорционально массе.'
                    meals.append(dict(id=key,fields=remainder))
                if action=='confirm':f['Status']='CONFIRMED'
                for k in ('Kcal','Protein','Fat','Carbs'):f[k]=float(number(f[k])*grams/old_grams)
                f.update(Grams=float(grams),LastUpdate=now)
                # Notes may contain an old portion JSON; don't retain it as current evidence.
                f['PreviousNotes']=f.get('Notes','')
                f['Notes']='Явное изменение массы/статуса '+now+'; КБЖУ пересчитаны пропорционально массе. Источник: '+str(f.get('Source','неизвестен'))
        elif action=='close':
            if any(r['fields']['Status']=='PLANNED' for r in meals):raise SyncError('Остался план: подтвердите /confirm или отмените /cancel каждую позицию')
            if not any(r['fields']['Status']=='CONFIRMED' for r in meals):raise SyncError('В дне нет подтверждённых продуктов')
            state['Status']='CLOSED'
        else:raise SyncError('Неизвестная операция дневника')
        if snapshot==original and action!='close':
            db.execute('INSERT INTO diary_operations VALUES(?,?,?)',(journal.account,event,fingerprint))
            return snapshot
        if action=='close' and original['state'] and original['state']['fields'].get('Status')=='CLOSED':
            db.execute('INSERT INTO diary_operations VALUES(?,?,?)',(journal.account,event,fingerprint))
            return original
        eaten,forecast=totals([r for r in meals if r['fields']['Status']!='CANCELLED'],day)
        state=dict(state or dict(Status='ACTIVE',CaloriesGoal=1957,ProteinMin=150,ProteinMax=160,FatMin=70,FatMax=80,CarbMax=170))
        state.setdefault('Status','ACTIVE')
        state.update(DayKey=day,Date=day,Version=expected_version+1,LastUpdate=now)
        for prefix,n in [('Eaten',eaten),('Forecast',forecast)]:
            state.update(dict(zip((prefix+k for k in ('Kcal','Protein','Fat','Carbs')),map(float,n.values().values()))))
        snapshot.update(state=dict(id=day,fields=state),computed_eaten=eaten.values(),computed_forecast=forecast.values())
        snapshot.pop('google_sync',None)
        payload=json.dumps(snapshot,ensure_ascii=False,allow_nan=False)
        if len(payload)>44000:raise SyncError('Слишком большой дневник дня')
        db.execute('''INSERT INTO google_diary(account,day,version,snapshot,updated) VALUES(?,?,?,?,?)
            ON CONFLICT(account,day) DO UPDATE SET version=excluded.version,snapshot=excluded.snapshot,
            updated=excluded.updated,last_error='' ''',(journal.account,day,expected_version+1,payload,now))
        db.execute('INSERT INTO diary_operations VALUES(?,?,?)',(journal.account,event,fingerprint))
    return snapshot
