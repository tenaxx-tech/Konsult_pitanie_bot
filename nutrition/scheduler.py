"""Local-time reminders with durable claims; never close a day automatically."""
from datetime import datetime, timezone
import json
from threading import Event, Thread
from zoneinfo import ZoneInfo
from .accounts import Accounts
from .google_storage import LocalGoogleJournal
from .diary_commands import render


def tick(database, sender, now=None):
    now=now or datetime.now(timezone.utc)
    accounts=Accounts(database)
    with accounts.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS scheduled_deliveries(
            account TEXT,day TEXT,kind TEXT,state TEXT,updated TEXT,PRIMARY KEY(account,day,kind))''')
        users=db.execute("SELECT account,profile FROM bot_users WHERE stage='ready'").fetchall()
    count=0
    for account,raw in users:
        p=json.loads(raw)
        if not account.startswith('telegram:') or not p.get('notifications') or not accounts.active(account,now):continue
        local=now.astimezone(ZoneInfo(p.get('timezone','Asia/Yekaterinburg')))
        minute=local.hour*60+local.minute
        day=local.date().isoformat()
        for kind,target in [('morning',480),('evening',1290)]:
            if not target<=minute<target+90:continue
            with accounts.db() as db:
                inserted=db.execute('INSERT OR IGNORE INTO scheduled_deliveries VALUES(?,?,?,?,?)',
                                    (account,day,kind,'CLAIMED',now.isoformat())).rowcount
            if not inserted:continue
            # Reserve before network send. Uncertain sends aren't automatically repeated.
            try:
                snapshot=LocalGoogleJournal(database,account).read(day)
                if kind=='morning':
                    text=('Доброе утро!\n'+render(snapshot)+'\nПланируйте регулярные полноценные приёмы пищи. '
                          '/plan — меню на остаток дня. Данных активности и веса может не быть; отсутствие не означает ноль.')
                else:
                    text=('Вечерняя проверка\n'+render(snapshot)+'\nВсё съеденное учтено? Для проверки и закрытия — /close '+day+
                          '. Если ещё будете есть, закроем позже. Последовательность важнее идеального дня.')
                sender('telegram',int(account.split(':')[1]),text,'schedule:'+account+':'+day+':'+kind)
                state='SENT';count+=1
            except Exception:state='UNCERTAIN'
            with accounts.db() as db:db.execute('UPDATE scheduled_deliveries SET state=?,updated=? WHERE account=? AND day=? AND kind=?',
                                                (state,now.isoformat(),account,day,kind))
    return count


def start(database, sender, interval=30):
    stop=Event()
    def run():
        while not stop.is_set():
            try:tick(database,sender)
            except Exception:pass
            stop.wait(interval)
    thread=Thread(target=run,name='nutrition-reminders',daemon=True)
    thread.start()
    return stop,thread
