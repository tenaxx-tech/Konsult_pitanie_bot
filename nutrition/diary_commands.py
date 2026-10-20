"""Local diary UI: numbered portions, explicit corrections and final close consent."""
from datetime import date, datetime, timezone, timedelta
import json
from .airtable import SyncError
from .core import number
from .diary_service import mutate


def render(snapshot):
    f=snapshot['state']['fields'] if snapshot['state'] else {}
    lines=[f"Дата: {snapshot['day']}; статус: {f.get('Status','нет записей')}; версия: {f.get('Version',0)}."]
    for i,row in enumerate(snapshot['meals'],1):
        m=row['fields'];weight=str(m.get('Grams'))+' г' if m.get('Grams') is not None else 'вес неизвестен'
        lines.append(f"{i}. {m['Product']} — {weight}; {m['Kcal']:.1f} ккал; {m['Status']}; "+('≈оценка' if m.get('Approx') else str(m.get('Source','источник неизвестен'))))
    n=snapshot['computed_eaten']
    lines.append(f"Факт: ≈{float(n['kcal']):.1f} ккал · Б {float(n['protein']):.1f} · Ж {float(n['fat']):.1f} · У {float(n['carbs']):.1f} г.")
    if 'CaloriesGoal' in f:
        lines.append(f"Ориентир: {f['CaloriesGoal']} ккал; остаток {float(f['CaloriesGoal'])-float(n['kcal']):.1f} ккал.")
    lines.append('Google: '+snapshot.get('google_sync','не проверен')+'.')
    return '\n'.join(lines)[:3800]


def pending_close(journal, text, event):
    with journal.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS pending_closes(account TEXT PRIMARY KEY,day TEXT,version INTEGER,event TEXT,expires TEXT)')
        r=db.execute('SELECT day,version,event,expires FROM pending_closes WHERE account=?',(journal.account,)).fetchone()
        if not r:return None
        if datetime.fromisoformat(r[3])<datetime.now(timezone.utc):
            db.execute('DELETE FROM pending_closes WHERE account=?',(journal.account,));return None
        if text.casefold().strip() in ('нет','отмена'):
            db.execute('DELETE FROM pending_closes WHERE account=?',(journal.account,));return 'Закрытие отменено.'
    if text.casefold().strip().rstrip('.!') not in ('да','закрыть','подтверждаю'):return None
    result=mutate(journal,r[0],'close',r[2],r[1])
    with journal.connect() as db:db.execute('DELETE FROM pending_closes WHERE account=?',(journal.account,))
    return 'День закрыт и сохранён локально. Спасибо за последовательность!\n'+render(journal.read(r[0]))+'\nБаллы автоматически не начислялись.'


def route(journal, text, current_day, event):
    command,_,payload=text.strip().partition(' ')
    if command not in ('/day','/history','/correct','/confirm','/cancel','/close','/briefing'):return None
    if command=='/history':
        with journal.connect() as db:
            rows=db.execute("SELECT day,version FROM google_diary WHERE account=? AND day<? ORDER BY day DESC LIMIT 31",(journal.account,'2099-01-01')).fetchall()
        return '\n'.join(f'{d} — версия {v}' for d,v in rows)+'\nПросмотр: /day YYYY-MM-DD.' if rows else 'История пока пуста.'
    args=payload.split()
    day=args.pop(0) if args and len(args[0])==10 and args[0][4]=='-' else current_day
    if args and args[0].casefold() in ('вчера','сегодня'):
        relative=args.pop(0).casefold()
        day=(date.fromisoformat(current_day)-timedelta(days=1)).isoformat() if relative=='вчера' else current_day
    try:date.fromisoformat(day)
    except ValueError:return 'Укажите дату YYYY-MM-DD.'
    snapshot=journal.read(day)
    state=snapshot['state']['fields'] if snapshot['state'] else {}
    if command=='/day':return render(snapshot) if not args else 'Просмотр: /day YYYY-MM-DD, /day вчера или /day сегодня.'
    if command=='/briefing':
        return 'Утренний ориентир\n'+render(snapshot)+'\nФокус: регулярные полноценные приёмы пищи. Активность и вес показываем только при наличии отдельных данных.'
    if command=='/close':
        if state.get('Status')=='CLOSED':return 'День уже закрыт.\n'+render(snapshot)
        if any(r['fields']['Status']=='PLANNED' for r in snapshot['meals']):
            return 'Остался план. Подтвердите /confirm номер [съеденные_граммы] или отмените /cancel номер.\n'+render(snapshot)
        if not snapshot['meals']:return 'Дневник пуст. Сначала внесите фактически съеденное.'
        with journal.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pending_closes(account TEXT PRIMARY KEY,day TEXT,version INTEGER,event TEXT,expires TEXT)')
            db.execute('INSERT OR REPLACE INTO pending_closes VALUES(?,?,?,?,?)',
                       (journal.account,day,state.get('Version',0),event,(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat()))
        return render(snapshot)+'\nВсё съеденное внесено и день завершён? Ответьте «Да» для закрытия или «Отмена». Баллы не начисляются автоматически.'
    try:
        index=int(args[0])-1
        if index<0:raise ValueError()
        item=snapshot['meals'][index]['fields']
        grams=float(number(args[1].replace(',','.'))) if len(args)>1 else None
        if command=='/correct' and grams is None:raise ValueError()
        if len(args)>2:raise ValueError()
    except (ValueError,IndexError):return 'Сначала /day. Затем /correct номер граммы, /confirm номер [граммы] или /cancel номер. Для другой даты добавьте YYYY-MM-DD перед номером.'
    mutate(journal,day,command[1:],event,state.get('Version',0),item_key=item['MealItemKey'],grams=grams)
    return 'Изменение сохранено локально; Google обновится в фоне.\n'+render(journal.read(day))
