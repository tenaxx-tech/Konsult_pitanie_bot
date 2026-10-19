"""Local user profiles, first-start trial and explicit onboarding consent."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
import sqlite3
from zoneinfo import ZoneInfo

OWNER_GOALS = dict(CaloriesGoal=1957,ProteinMin=150,ProteinMax=160,FatMin=70,FatMax=80,CarbMax=170)


def utcnow():
    return datetime.now(timezone.utc)


def is_admin(account):
    return account == 'telegram:'+os.environ.get('TELEGRAM_OWNER_ID','') and bool(os.environ.get('TELEGRAM_OWNER_ID'))


class Accounts:
    def __init__(self, database):
        self.database=database
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS bot_users(
                account TEXT PRIMARY KEY, first_start TEXT NOT NULL, trial_end TEXT NOT NULL,
                profile TEXT NOT NULL, stage TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                synced_version INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS feedback(
                id INTEGER PRIMARY KEY,account TEXT NOT NULL,created TEXT NOT NULL,text TEXT NOT NULL)''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.database,timeout=10)
        try:
            with db:yield db
        finally:db.close()

    def get(self, account):
        with self.db() as db:
            r=db.execute('SELECT first_start,trial_end,profile,stage FROM bot_users WHERE account=?',(account,)).fetchone()
        return dict(account=account,first_start=r[0],trial_end=r[1],profile=json.loads(r[2]),stage=r[3]) if r else None

    def start(self, account, now=None):
        now=now or utcnow()
        admin=is_admin(account)
        p=dict(timezone='Asia/Yekaterinburg',notifications=admin)
        if admin:p.update(goals=OWNER_GOALS,preferences='Недорогие продукты, нормальные порции; без долгих перерывов',consent=True)
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO bot_users(account,first_start,trial_end,profile,stage,updated) VALUES(?,?,?,?,?,?)',
                       (account,now.isoformat(),(now+timedelta(hours=72)).isoformat(),json.dumps(p,ensure_ascii=False),
                        'ready' if admin else 'consent',now.isoformat()))
        return self.get(account)

    def save(self, account, profile, stage):
        with self.db() as db:
            db.execute('UPDATE bot_users SET profile=?,stage=?,version=version+1,updated=? WHERE account=?',
                       (json.dumps(profile,ensure_ascii=False,allow_nan=False),stage,utcnow().isoformat(),account))

    def active(self, account, now=None):
        u=self.get(account)
        return bool(u and u['stage']=='ready' and (is_admin(account) or (now or utcnow()) < datetime.fromisoformat(u['trial_end'])))

    def goals(self, account):
        u=self.get(account)
        return u['profile'].get('goals') if u else None


def estimate_goals(p):
    # Mifflin-St Jeor, https://pubmed.ncbi.nlm.nih.gov/2305711/ .
    # Activity multiplier and modest goal adjustment are explicit estimates.
    base=10*p['weight']+6.25*p['height']-5*p['age']+(5 if p['sex']=='м' else -161)
    factor={'низкая':1.2,'средняя':1.4,'высокая':1.6}[p['activity']]
    kcal=round(base*factor*{'снижение':.9,'поддержание':1,'набор':1.05}[p['goal']]/10)*10
    if not 1200<=kcal<=4500:
        raise ValueError('Automatic estimate outside supported range')
    protein=round(min(p['weight']*1.4,kcal*.25/4))
    fat=round(kcal*.3/9)
    carbs=round((kcal-4*protein-9*fat)/4)
    return dict(CaloriesGoal=kcal,ProteinMin=protein,ProteinMax=protein+10,
                FatMin=fat,FatMax=fat+10,CarbMax=carbs)


def profile_reply(u):
    g=u['profile']['goals']
    return (f"Предварительный ориентир: {g['CaloriesGoal']} ккал; Б {g['ProteinMin']}–{g['ProteinMax']} г; "
            f"Ж {g['FatMin']}–{g['FatMax']} г; У до {g['CarbMax']} г.\n"
            "Это оценка потребности, не индивидуальное медицинское назначение. "
            "Цели можно изменить командой /goals.\n"
            "Предпочтения: "+u['profile'].get('preferences','')+".\n"
            "Принять ориентиры и включить брифинг 08:00/вечернее напоминание 21:30 — /accept; "
            "переделать анкету — /profile. После принятия /plan рассчитает меню с граммовками "
            "на оставшуюся часть дня, /week — на неделю. Предложение меню не считается съеденным. "
            "Фото дают предварительную оценку; факт еды подтверждает пользователь.")


def route(accounts, account, text):
    """Return a direct local reply, or None when the diary may handle the message."""
    command,_,payload=text.strip().partition(' ')
    command=command.lower()
    u=accounts.get(account)
    if is_admin(account) and not u:u=accounts.start(account)
    if command=='/start':
        u=accounts.start(account)
        if is_admin(account):return 'Режим администратора: доступ без оплаты. /menu — дневник; /profile — параметры; /notifications — расписание.'
        if u['stage']=='ready':
            return ('Пробный доступ до '+u['trial_end']+' (UTC); повторный /start не продлевает срок. /menu — меню.')
        if u['stage']!='consent':return 'Продолжим анкету. '+QUESTIONS.get(u['stage'],'Используйте /profile.')
        return ('Добро пожаловать! Проба — 72 часа с первого /start. Дневник хранится на сервере и копируется в Google; '
                'текст и фото для разбора передаются провайдеру модели. Не отправляйте лишние личные данные. '
                'Продолжить с этими условиями обработки — /agree; отказаться — /decline. '
                'Бот рассчитан на взрослых; при беременности, грудном вскармливании и специальных лечебных диетах '
                'цели нужно согласовать со специалистом. Оплата пока не подключена.')
    if not u:return 'Для начала отправьте /start.'
    p=u['profile']
    if command=='/admin':
        if not is_admin(account):return 'Эта команда доступна только администратору.'
        with accounts.db() as db:
            users=db.execute('SELECT count(*) FROM bot_users').fetchone()[0]
            profiles_pending=db.execute('SELECT count(*) FROM bot_users WHERE version>synced_version').fetchone()[0]
            diary_pending=0
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='google_diary'").fetchone():
                diary_pending=db.execute('SELECT count(*) FROM google_diary WHERE version>synced_version').fetchone()[0]
            feedback=db.execute('SELECT account,text FROM feedback ORDER BY id DESC LIMIT 5').fetchall()
            jobs=[]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_deliveries'").fetchone():
                jobs=db.execute("SELECT day,kind,state FROM scheduled_deliveries WHERE state!='SENT' ORDER BY day DESC LIMIT 5").fetchall()
        return 'Пользователей: '+str(users)+'\nОжидают Google: профили '+str(profiles_pending)+', дни '+str(diary_pending)+'\nПоследние отзывы:\n'+'\n'.join(a+': '+t[:300] for a,t in feedback)+'\nНеопределённые уведомления: '+str(jobs)
    if command=='/decline':
        p['consent']=False;p['notifications']=False
        accounts.save(account,p,'consent')
        return 'Анкета и разбор сообщений отключены. Для продолжения — /start.'
    if command=='/feedback':
        if not payload:return 'Напишите /feedback и ваше пожелание одним сообщением.'
        with accounts.db() as db:db.execute('INSERT INTO feedback(account,created,text) VALUES(?,?,?)',(account,utcnow().isoformat(),payload[:4000]))
        return 'Спасибо! Отзыв сохранён для улучшения бота.'
    if command=='/notifications':
        if payload.lower() not in ('on','off'):
            return ('Брифинг 08:00, вечернее подтверждение 21:30; пояс '+p.get('timezone','Asia/Yekaterinburg')+
                    '. /notifications on — включить; /notifications off — выключить. Вечером день сам не закрывается.')
        if not p.get('consent'):return 'Сначала ознакомьтесь с условиями: /start.'
        p['notifications']=payload.lower()=='on';accounts.save(account,p,u['stage'])
        return 'Расписание включено.' if p['notifications'] else 'Расписание выключено.'
    if command=='/timezone':
        try:ZoneInfo(payload)
        except (ValueError,KeyError):return 'Укажите часовой пояс, например /timezone Asia/Yekaterinburg.'
        p['timezone']=payload;accounts.save(account,p,u['stage'])
        return 'Часовой пояс сохранён: '+payload
    if command=='/profile':
        if not p.get('consent'):return 'Сначала /start и /agree.'
        accounts.save(account,p,'height');return QUESTIONS['height']
    if command=='/agree' and u['stage']=='consent':
        p['consent']=True;accounts.save(account,p,'height');return QUESTIONS['height']
    if command=='/accept' and u['stage']=='review':
        p['notifications']=True;accounts.save(account,p,'ready')
        return 'Ориентиры приняты. /plan — рассчитать меню на остаток дня; /week — меню на неделю; /food — запись; /photo — фото. /notifications off — отключить расписание.'
    if command=='/goals':
        try:
            v=[float(x.replace(',','.')) for x in payload.split()]
            if len(v)!=6 or not all(__import__('math').isfinite(x) for x in v):raise ValueError()
            kcal,b1,b2,f1,f2,c=v
            if not (1200<=kcal<=6000 and 0<b1<=b2<=400 and 0<f1<=f2<=300 and 0<c<=1000):raise ValueError()
        except ValueError:return 'Формат: /goals ккал Б_мин Б_макс Ж_мин Ж_макс У_макс. Укажите согласованные ориентиры; автоматический расчёт — /profile.'
        p['goals']=dict(zip(OWNER_GOALS,v));accounts.save(account,p,'review' if u['stage']!='ready' else 'ready')
        return 'Ориентиры сохранены для новых дней. Для нового профиля подтвердите /accept.'
    stage=u['stage']
    if stage=='ready':return None
    if stage=='review':return profile_reply(u)
    if command.startswith('/'):return 'Продолжите анкету: '+QUESTIONS.get(stage,'/start')
    value=payload if command=='/answer' else text.strip()
    try:
        if stage in ('height','weight','age'):
            v=float(value.replace(',','.'))
            bounds={'height':(120,230),'weight':(35,250),'age':(18,80)}[stage]
            if not bounds[0]<=v<=bounds[1]:raise ValueError()
            p[stage]=v
        elif stage=='sex':
            v=value.casefold();p['sex']={'м':'м','мужской':'м','мужчина':'м','ж':'ж','женский':'ж','женщина':'ж'}[v]
        elif stage=='activity':
            v=value.casefold()
            if v not in ('низкая','средняя','высокая'):raise ValueError()
            p['activity']=v
        elif stage=='goal':
            v=value.casefold()
            if v not in ('снижение','поддержание','набор'):raise ValueError()
            p['goal']=v
        elif stage=='preferences':p['preferences']=value[:1500]
        elif stage=='timezone':ZoneInfo(value);p['timezone']=value
        else:return 'Для продолжения отправьте /start.'
    except (ValueError,KeyError):return 'Не удалось распознать ответ. '+QUESTIONS.get(stage,'/profile')
    stages=list(QUESTIONS)
    next_stage=stages[stages.index(stage)+1] if stage!=stages[-1] else 'review'
    if next_stage=='review':
        try:p['goals']=estimate_goals(p)
        except ValueError:
            accounts.save(account,p,'manual')
            return 'Автоматический ориентир вышел за рабочий диапазон расчёта. Укажите согласованные со специалистом цели через /goals или перепроверьте анкету: /profile.'
    accounts.save(account,p,next_stage)
    return profile_reply(accounts.get(account)) if next_stage=='review' else QUESTIONS[next_stage]


QUESTIONS=dict(height='Ваш рост в сантиметрах?',weight='Вес в килограммах?',age='Возраст в полных годах (18–80)?',
               sex='Пол для расчётной формулы: м или ж?',activity='Обычная активность: низкая, средняя или высокая?',
               goal='Цель: снижение, поддержание или набор?',preferences='Пищевые предпочтения, исключения и аллергии? Если нет — напишите «нет».',
               timezone='Часовой пояс, например Asia/Yekaterinburg или Europe/Moscow?')
