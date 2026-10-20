"""Telegram/VK webhook gateway; owner-only, structured commands in this version."""
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from .api import calculate
from .airtable import AirtableJournal, Client, SyncError

LOCK=Lock()
ACCOUNT_LOCKS={}


def account_lock(account):
    with LOCK:
        return ACCOUNT_LOCKS.setdefault(account,Lock())
CALC_USAGE=('Для расчёта укажите порции в JSON. Пример:\n'
            '/calc {"portions":[{"name":"Продукт","grams":100,'
            '"per100":{"kcal":120,"protein":10,"fat":5,"carbs":8},'
            '"source":"этикетка"}]}\nДневник при расчёте не изменяется.')


def today():
    return datetime.now(ZoneInfo(os.environ.get('NUTRITION_TIMEZONE','Asia/Yekaterinburg'))).date().isoformat()


def _explicit_consumption(text):
    value=text.casefold()
    if re.search(r'\b(не\s+(съел|съела|съели|ел|ела|выпил|выпила|пил|пила)|хочу\s+(съесть|выпить)|планирую\s+(съесть|выпить)|можно\s+ли\s+(съесть|выпить))\b', value):
        return False
    return bool(re.search(r'\b(съел|съела|съели|съедено|поел|поела|поели|ел|ела|ели|выпил|выпила|выпили|пил|пила|пили)\b', value))


def _affirmative(text):
    return bool(re.fullmatch(r'\s*(да|съел(?:а|и)?|выпил(?:а|и)?|подтверждаю|верно|согласен|ок(?:ей)?)\s*[.!]?', text.casefold()))


def _negative(text):
    return bool(re.fullmatch(r'\s*(нет|не\s+ел|не\s+съел|не\s+подтверждаю|отмена|удали|неверно)\s*[.!]?', text.casefold()))


def _intake_candidate(text, has_attachment=False):
    return has_attachment or _explicit_consumption(text) or bool(re.search(r'\d+\s*(?:г|гр|грамм|мл)\b',text.casefold())) or text.startswith('Рассчитай, это не факт еды:') or bool(re.search(
        r'\b(запланируй|запланировать|в\s+план|добавь\s+в\s+план)\b',text.casefold()))


def _pending(database, owner_key):
    if not owner_key:
        return None
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS pending_meals(owner_key TEXT PRIMARY KEY, day TEXT NOT NULL, meal TEXT NOT NULL, items TEXT NOT NULL, event_id TEXT NOT NULL)')
        row=db.execute('SELECT day,meal,items,event_id FROM pending_meals WHERE owner_key=?',(owner_key,)).fetchone()
    return {'day':row[0],'meal':row[1],'items':json.loads(row[2]),'event_id':row[3]} if row else None


def _set_pending(database, owner_key, day, meal, items, event_id):
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS pending_meals(owner_key TEXT PRIMARY KEY, day TEXT NOT NULL, meal TEXT NOT NULL, items TEXT NOT NULL, event_id TEXT NOT NULL)')
        db.execute('INSERT INTO pending_meals VALUES (?,?,?,?,?) ON CONFLICT(owner_key) DO UPDATE SET day=excluded.day,meal=excluded.meal,items=excluded.items,event_id=excluded.event_id',
                   (owner_key,day,meal,json.dumps(items,ensure_ascii=False),event_id))


def _clear_pending(database, owner_key):
    with sqlite3.connect(database) as db:
        db.execute('DELETE FROM pending_meals WHERE owner_key=?',(owner_key,))


def _event_data(database, event_id, kind, value=None):
    """Persist interpretation before writes and replies before channel delivery."""
    if event_id == 'manual-event':
        return value
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS event_data(event TEXT, kind TEXT, value TEXT NOT NULL, PRIMARY KEY(event,kind))')
        if value is not None:
            db.execute('INSERT OR IGNORE INTO event_data VALUES (?,?,?)',
                       (event_id,kind,json.dumps(value,ensure_ascii=False)))
        row=db.execute('SELECT value FROM event_data WHERE event=? AND kind=?',
                       (event_id,kind)).fetchone()
    return json.loads(row[0]) if row else None


def _record_items(remote, day, meal, items, event_id, status):
    from .core import Nutrients, Portion
    from .airtable import select
    snapshot=remote.read(day)
    state=snapshot['state']['fields'] if snapshot['state'] else {}
    version=state.get('Version',0)
    event_hash=hashlib.sha256(event_id.encode()).hexdigest()[:20]
    from .google_storage import LocalGoogleJournal
    batch=[]
    for index,item in enumerate(items,1):
        try:
            grams=Decimal(str(item['grams']))
            totals={key:Decimal(str(item[key])) for key in ('kcal','protein','fat','carbs')}
        except (InvalidOperation,KeyError,TypeError):
            raise SyncError('Meal estimate is invalid') from None
        if not grams.is_finite() or grams<=0 or grams>10000 or any(not v.is_finite() or v<0 or v>100000 for v in totals.values()):
            raise SyncError('Meal estimate is outside allowed range')
        per100={key:value*Decimal(100)/grams for key,value in totals.items()}
        # Keep model-derived nutrition values explicitly marked approximate.
        estimated=True
        source=item.get('source_detail','AI estimate') if estimated else 'User-provided label'
        portion=Portion(str(item['name'])[:160],grams,Nutrients(**per100),source,item.get('basis','ready'),estimated)
        key=f'{day}|{meal}|{event_hash}_{index}'
        payload={'MealItemKey':key,'Meal':meal,'Status':status,'Source':'ESTIMATE' if estimated else 'LABEL',
                 'portion':portion.record()}
        if isinstance(remote,LocalGoogleJournal):
            batch.append(payload)
            continue
        result=remote.upsert(day,payload,version)
        state=result['state']['fields'] if result['state'] else {}
        version=state.get('Version',version+1)
    if batch:
        from .diary_service import mutate
        mutate(remote,day,'add',event_id+':'+status,version,items=batch)
    return remote.read(day)


def _intake_reply(result, items, status):
    names=', '.join(str(x['name']) for x in items)
    total={k:sum((Decimal(str(x[k])) for x in items),Decimal(0)) for k in ('kcal','protein','fat','carbs')}
    estimate=' (оценка)' if any(x.get('estimated') is not False for x in items) else ''
    action='Записал в дневник' if status=='CONFIRMED' else 'Добавил в план'
    reply = (f'{action}: {names}{estimate}.\nИтого: {total["kcal"]:.1f} ккал · '
            f'Б {total["protein"]:.1f} · Ж {total["fat"]:.1f} · У {total["carbs"]:.1f} г. '
            'Дальше можешь прислать ещё продукты или попросить подобрать следующий приём пищи.')
    notice=result.get('storage_notice','')
    if notice=='Сохранено локально; синхронизация с Google проверяется отдельно':
        reply+='\nСохранено локально.'
    elif notice:
        reply+='\nСтатус сохранения: '+notice
    return reply


def _estimate_reply(items):
    # Model text is not trusted for totals or comparisons with the user's goal.
    keys=('kcal','protein','fat','carbs')
    totals={k:sum(Decimal(str(item[k])) for item in items) for k in keys}
    lines=['Предварительная справочная оценка (≈):']
    for item in items:
        lines.append(f"{item['name']} — {Decimal(str(item['grams'])):.1f} г: "
                     f"{Decimal(str(item['kcal'])):.1f} ккал; Б {Decimal(str(item['protein'])):.1f}, "
                     f"Ж {Decimal(str(item['fat'])):.1f}, У {Decimal(str(item['carbs'])):.1f} г.")
    lines.append(f"Итого: ≈{totals['kcal']:.1f} ккал; Б {totals['protein']:.1f}, "
                 f"Ж {totals['fat']:.1f}, У {totals['carbs']:.1f} г.")
    lines.append('Для приготовленных блюд количество масла и состав влияют на оценку. Это расчёт, в дневник пока не записано.')
    return '\n'.join(lines)


def answer(text, remote_factory=None, database=None,
           event_id='manual-event', owner_key=None, attachment_ids=None):
    database=database or os.environ.get('CHANNEL_DB','channels.sqlite3')
    managed=remote_factory is None and owner_key is not None
    current_day=today()
    accounts=None
    if managed:
        from .accounts import Accounts, route, is_admin
        accounts=Accounts(database)
        direct=route(accounts,owner_key,text)
        if direct is not None:
            return direct
        user=accounts.get(owner_key)
        current_day=datetime.now(ZoneInfo(user['profile'].get('timezone','Asia/Yekaterinburg'))).date().isoformat()
        cmd=text.strip().split(' ')[0].lower()
        if not accounts.active(owner_key) and cmd not in ('/day','/history','/sync','/menu','/help','/briefing'):
            return 'Пробные 72 часа завершились. История доступна через /day и /history. Оплата ещё не подключена; новые записи пока недоступны.'
    if remote_factory is None:
        remote_factory = lambda: configured_journal(database,owner_key)
    from .telegram_menu import normalize, MENU_TEXT
    text=normalize(text)
    command, _, payload=text.strip().partition(' ')
    command=command.lower()
    from .google_storage import LocalGoogleJournal
    if managed:
        remote=remote_factory()
        from .diary_commands import route as diary_route,pending_close
        if isinstance(remote,LocalGoogleJournal):
            reply=pending_close(remote,text,event_id)
            if reply is not None:return reply
            reply=diary_route(remote,text,current_day,event_id)
            if reply is not None:
                if command in ('/correct','/confirm','/cancel') and reply.startswith('Изменение сохранено'):
                    _clear_pending(database,owner_key)
                return reply
    if managed:
        from .planning_setup import handle as setup_handle
        setup_reply=setup_handle(accounts,owner_key,text,current_day,remote_factory,attachment_ids)
        if isinstance(setup_reply,dict):
            text=setup_reply['intake_text']
            command,_,payload=text.strip().partition(' ')
            command=command.lower()
        elif setup_reply is not None:return setup_reply
    pending=_pending(database,owner_key)
    resume_intake=False
    if owner_key:
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE IF NOT EXISTS intake_questions(account TEXT PRIMARY KEY,text TEXT NOT NULL,event TEXT NOT NULL)')
            if re.search(r'не\s+уч[её]л\w*\s+пиц',text.casefold()):
                db.execute('DELETE FROM pending_meals WHERE owner_key=?',(owner_key,))
                db.execute('INSERT OR REPLACE INTO intake_questions VALUES(?,?,?)',(owner_key,'Пицца. Пользователь просит уточнить учёт порции.',event_id))
                return 'Давай уточним пиццу. Напиши вес порции и начинку; калорийность оценю сам. Если уже съела — так и напиши, чтобы отличить её от плана.'
            question=db.execute('SELECT text FROM intake_questions WHERE account=?',(owner_key,)).fetchone()
            if question and (_negative(text) or command=='/forget'):
                db.execute('DELETE FROM intake_questions WHERE account=?',(owner_key,))
                return 'Уточнение отменено. Новых продуктов не записывал.'
        if question and not command.startswith('/'):
            from .intake_checks import known_pizza_event
            if known_pizza_event(text,current_day) is not None:
                with sqlite3.connect(database) as db:db.execute('DELETE FROM intake_questions WHERE account=?',(owner_key,))
                _clear_pending(database,owner_key)
                pending=None
                question=None
        if question and not command.startswith('/'):
            if pending and not _affirmative(text):
                _clear_pending(database,owner_key)
                pending=None
            text=question[0]+'\nУточнение пользователя: '+text
            resume_intake=True
    cached_input=_event_data(database,event_id,'intake_input')
    if cached_input is not None:
        text=cached_input
        resume_intake=True
    if pending and _affirmative(text):
        from .intake_checks import references,validate
        description='; '.join(str(item.get('name','')) for item in pending['items'])
        pending['items']=references(pending['items'],description)
        problem=validate(pending['items'],description,nutrition_only=True)
        if problem:return problem+' Пока ничего не записано.'
        result=_record_items(remote_factory(),pending['day'],pending['meal'],pending['items'],pending['event_id'],'CONFIRMED')
        _clear_pending(database,owner_key)
        with sqlite3.connect(database) as db:db.execute('DELETE FROM intake_questions WHERE account=?',(owner_key,))
        return _intake_reply(result,pending['items'],'CONFIRMED')
    if pending and _negative(text):
        _clear_pending(database,owner_key)
        return 'Понял, ничего не записывал.'
    if command in ('/start','/menu','/help','помощь','меню'):
        return MENU_TEXT
    if command == '/food':
        return ('🍽 Запись еды\n\nНапишите, что уже съели или выпили, и укажите количество. '
                'Например: «Съел творог 0% 180 г». Для масла, соусов и напитков тоже укажите количество. '
                'Желаемую еду и предложения меню бот не считает съеденными.')
    if command == '/photo':
        return ('📸 Расчёт по фото\n\nПришлите фотографию еды с весом порции и составом, если он известен. '
                'Для упаковки сфотографируйте этикетку КБЖУ. Если это только оценка, бот запросит '
                'подтверждение перед записью. Если уже съели, укажите это в подписи.')
    if command in ('/day','/sync','сегодня'):
        data=remote_factory().read(current_day)
        state=data['state']['fields'] if data['state'] else {}
        status=state.get('Status')
        if isinstance(status,dict):
            status=status.get('name')
        if status=='CLOSED':
            values={k:str(state.get(n,'нет данных')) for k,n in zip(('kcal','protein','fat','carbs'),('EatenKcal','EatenProtein','EatenFat','EatenCarbs'))}
        else:
            values=data['computed_eaten']
        reply = (f"Дата: {data['day']}\nФакт: {values['kcal']} ккал · Б {values['protein']} · Ж {values['fat']} · У {values['carbs']} г\n"
                 f"Позиций: {len(data['meals'])}. Версия: {state.get('Version',0)}.")
        if command == '/sync':
            if 'google_sync' in data:
                reply += '\nGoogle: '+data['google_sync']+' (фоновая выгрузка; изменения других приложений не загружаются).'
            from .core import Goals, Nutrients
            defaults = Goals()
            goal_fields = {'kcal':'CaloriesGoal','protein_min':'ProteinMin','protein_max':'ProteinMax',
                           'fat_min':'FatMin','fat_max':'FatMax','carbs':'CarbMax'}
            goals = Goals(**{key: state.get(field, getattr(defaults,key)) for key,field in goal_fields.items()})
            try:
                remaining = goals.remaining(Nutrients(**values))
            except (ValueError, TypeError, InvalidOperation):
                return reply + '\nОстаток недоступен: в сохранённом итоге не хватает числовых данных.'
            confirmed = sum(1 for row in data['meals'] if (
                row.get('fields',{}).get('Status',{}).get('name') if isinstance(row.get('fields',{}).get('Status'),dict)
                else row.get('fields',{}).get('Status')) == 'CONFIRMED')
            if status == 'CLOSED':
                return reply + '\nДень закрыт; показан сохранённый итог, новых записей не делал.'
            return (reply + f"\nПодтверждено позиций: {confirmed}. Дневник перечитан из настроенного хранилища."
                    + f"\nОстаток калорий: {Decimal(remaining['kcal']):.1f} ккал."
                    + f"\nБелок до диапазона: {Decimal(remaining['protein'][0]):.1f}–{Decimal(remaining['protein'][1]):.1f} г."
                    + f"\nЖиры до диапазона: {Decimal(remaining['fat'][0]):.1f}–{Decimal(remaining['fat'][1]):.1f} г."
                    + f"\nУглеводы до лимита: {Decimal(remaining['carbs']):.1f} г."
                    + "\nОтрицательное значение означает превышение. Записей не менял.")
        return reply
    if command=='/calc':
        if not payload:
            return CALC_USAGE
        try:
            data=json.loads(payload)
            if not isinstance(data,dict):
                return CALC_USAGE
            result=calculate(data.get('portions'))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return 'Не удалось прочитать данные для расчёта.\n'+CALC_USAGE
        n=result['total']
        return f"{'≈ ' if result['estimated'] else ''}{n['kcal']} ккал · Б {n['protein']} · Ж {n['fat']} · У {n['carbs']} г"
    if (resume_intake or _intake_candidate(text, bool(attachment_ids))) and not os.environ.get('GIGACHAT_AUTHORIZATION_KEY'):
        return 'Для автоматического разбора и записи еды нужен подключённый GigaChat. Сейчас данные не записаны; проверьте секрет GIGACHAT_AUTHORIZATION_KEY на BotHost.'
    if os.environ.get('GIGACHAT_AUTHORIZATION_KEY'):
        from .gigachat_chat import answer as chat
        is_explicit=_explicit_consumption(text)
        if resume_intake or _intake_candidate(text, bool(attachment_ids)):
            from .gigachat_chat import extract_intake
            text=_event_data(database,event_id,'intake_input',text)
            snapshot=remote_factory().read(current_day)
            data=_event_data(database,event_id,'intake')
            if data is None:
                data=extract_intake(text,snapshot['day'],snapshot.get('protocols'),attachment_ids)
                data=_event_data(database,event_id,'intake',data)
            if data.get('action')=='record' and not is_explicit:
                data={**data,'action':'proposal'}
            for item in data.get('items',[]):
                item['estimated']=True
            day=data['day'] or snapshot['day']
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
                raise SyncError('GigaChat returned an invalid date')
            from datetime import date
            date.fromisoformat(day)
            if data['action']=='record' and day>current_day:
                return 'Дата фактически съеденной еды оказалась в будущем. Уточните дату; пока ничего не записано.'
            if data['action']=='clarify' or not data['items']:
                if owner_key:
                    with sqlite3.connect(database) as db:db.execute('INSERT OR REPLACE INTO intake_questions VALUES(?,?,?)',(owner_key,text[:12000],event_id))
                return data.get('reply') or 'Уточните, пожалуйста, продукт и количество.'
            from .intake_checks import validate,references
            data['items']=references(data['items'],text)
            question=validate(data['items'],text)
            if question:
                if owner_key:
                    with sqlite3.connect(database) as db:db.execute('INSERT OR REPLACE INTO intake_questions VALUES(?,?,?)',(owner_key,text[:12000],event_id))
                return question
            if owner_key and (is_explicit and data['action']=='record' or data['action']=='plan'):
                with sqlite3.connect(database) as db:db.execute('DELETE FROM intake_questions WHERE account=?',(owner_key,))
            if attachment_ids and not is_explicit:
                if not owner_key:
                    return data.get('reply') or 'Напишите «съел», если это нужно подтвердить и внести в дневник.'
                _set_pending(database,owner_key,day,data['meal'],data['items'],event_id)
                return _estimate_reply(data['items']) + '\nЕсли вы это съели, ответьте «Съел»; для отмены — «Отмена». Пока в дневник не записывал.'
            if is_explicit and data['action']=='record':
                result=_record_items(remote_factory(),day,data['meal'],data['items'],event_id,'CONFIRMED')
                return _intake_reply(result,data['items'],'CONFIRMED')
            if data['action']=='plan' and re.search(r'\b(план|запланир|в\s+план)\w*',text.casefold()):
                result=_record_items(remote_factory(),day,data['meal'],data['items'],event_id,'PLANNED')
                if owner_key:
                    _set_pending(database,owner_key,day,data['meal'],data['items'],event_id)
                return _intake_reply(result,data['items'],'PLANNED')+'\nЧтобы отметить план как съеденный, ответьте «Съел».'
            if data['action']=='proposal' and owner_key:
                with sqlite3.connect(database) as db:db.execute('INSERT OR REPLACE INTO intake_questions VALUES(?,?,?)',(owner_key,text[:12000],event_id))
                _set_pending(database,owner_key,day,data['meal'],data['items'],event_id)
                return _estimate_reply(data['items']) + '\nОтветьте «Съел» для записи или «Отмена», если не нужно вносить.'
        return chat(text, remote_factory().read(current_day), database)
    if os.environ.get('GEMINI_API_KEY'):
        from .gemini_chat import answer as chat
        return chat(text, remote_factory().read(current_day), database)
    if os.environ.get('GROQ_API_KEY'):
        from .groq_chat import answer as chat
        return chat(text, remote_factory().read(current_day), database)
    return 'ИИ пока не настроен: добавьте GIGACHAT_AUTHORIZATION_KEY (GigaChat), GEMINI_API_KEY (Gemini) или GROQ_API_KEY (Groq) в секреты сервера. /day и /calc доступны.'


def send(platform,peer,text,event_id):
    if platform=='telegram':
        token=os.environ['TELEGRAM_BOT_TOKEN']
        url='https://api.telegram.org/bot'+token+'/sendMessage'
        from .onboarding_ui import reply_keyboard
        body=json.dumps({'chat_id':peer,'text':text,'reply_markup':reply_keyboard(peer)},ensure_ascii=False).encode()
        headers={'Content-Type':'application/json'}
    else:
        url='https://api.vk.com/method/messages.send'
        random_id=int(hashlib.sha256(event_id.encode()).hexdigest()[:7],16)
        body=urlencode({'peer_id':peer,'message':text,'random_id':random_id,
                        'access_token':os.environ['VK_ACCESS_TOKEN'],'v':'5.199'}).encode()
        headers={'Content-Type':'application/x-www-form-urlencoded'}
    try:
        with urlopen(Request(url,data=body,headers=headers),timeout=15) as response:
            data=json.load(response)
        if (platform=='telegram' and not data.get('ok')) or 'error' in data:
            raise SyncError('Channel send failed')
    except Exception:
        # HTTP exception URLs can contain Telegram's token; never expose them.
        raise SyncError('Channel send failed; check channel access') from None


def _telegram_has_image(message):
    document=message.get('document') or {}
    return bool(message.get('photo')) or (isinstance(document,dict) and (
        str(document.get('mime_type','')).startswith('image/') or
        bool(re.search(r'\.(?:jpe?g|png|bmp|tiff?)$',str(document.get('file_name','')),re.I))))


def _telegram_photo(message):
    photos=message.get('photo') or []
    document=message.get('document') or {}
    if not _telegram_has_image(message):
        return None
    if document.get('file_size',0)>15*1024*1024:
        raise SyncError('Изображение больше 15 МБ. Отправьте его как обычное фото.')
    token=os.environ.get('TELEGRAM_BOT_TOKEN','')
    if not token:
        raise SyncError('Telegram photo download is not configured')
    import mimetypes
    from .gigachat_chat import upload_image
    file_id=photos[-1].get('file_id') if photos else document.get('file_id')
    if not file_id:
        raise SyncError('Telegram photo metadata is missing')
    def telegram_api(method,data=None):
        url='https://api.telegram.org/bot'+token+'/'+method
        req=Request(url,data=json.dumps(data).encode() if data is not None else None,
                    headers={'Content-Type':'application/json'})
        try:
            with urlopen(req,timeout=20) as response:
                return json.load(response)
        except Exception:
            raise SyncError('Telegram photo service unavailable') from None
    result=telegram_api('getFile',{'file_id':file_id})
    path=result.get('result',{}).get('file_path') if result.get('ok') else None
    if not path or '..' in path or not re.fullmatch(r'[A-Za-z0-9_./-]{1,512}',path):
        raise SyncError('Telegram photo path is invalid')
    url='https://api.telegram.org/file/bot'+token+'/'+path
    try:
        with urlopen(Request(url),timeout=25) as response:
            content=response.read(15*1024*1024+1)
            content_type=response.headers.get_content_type()
    except Exception:
        raise SyncError('Telegram photo download failed') from None
    if len(content)>15*1024*1024:
        raise SyncError('Telegram photo exceeds 15 MB')
    mime=content_type if content_type.startswith('image/') else (document.get('mime_type') or mimetypes.guess_type(path)[0])
    return upload_image(content,mime or 'image/jpeg',os.path.basename(path))


def handler(database, responder=None, sender=send):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def reply(self,code,text):
            body=text.encode()
            self.send_response(code)
            self.send_header('Content-Type','text/plain; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=131072:
                    self.reply(400,'invalid body');return
                data=json.loads(self.rfile.read(size))
                if self.path=='/telegram/webhook':
                    secret=os.environ.get('TELEGRAM_WEBHOOK_SECRET','')
                    if not secret or not hmac.compare_digest(self.headers.get('X-Telegram-Bot-Api-Secret-Token','').encode(),secret.encode()):
                        self.reply(403,'forbidden');return
                    message=data.get('message',{})
                    allowed=os.environ.get('TELEGRAM_OWNER_ID','')
                    user=str(message.get('from',{}).get('id',''))
                    peer=message.get('chat',{}).get('id')
                    public=os.environ.get('NUTRITION_MULTIUSER_ENABLED')=='true'
                    if not allowed or (user!=allowed and not public) or message.get('chat',{}).get('type')!='private' or str(peer)!=user:
                        self.reply(200,'ok');return
                    if 'update_id' not in data:
                        self.reply(400,'missing event');return
                    event='telegram:'+str(data['update_id']);platform='telegram'
                elif self.path=='/vk/webhook':
                    secret=os.environ.get('VK_CALLBACK_SECRET','')
                    if not secret or not hmac.compare_digest(str(data.get('secret','')).encode(),secret.encode()) or str(data.get('group_id'))!=os.environ.get('VK_GROUP_ID'):
                        self.reply(403,'forbidden');return
                    if data.get('type')=='confirmation':
                        code=os.environ.get('VK_CONFIRMATION_CODE','')
                        self.reply(200 if code else 503,code or 'not configured');return
                    if data.get('type')!='message_new':
                        self.reply(200,'ok');return
                    message=data.get('object',{}).get('message',{})
                    user=str(message.get('from_id',''));peer=message.get('peer_id')
                    if not os.environ.get('VK_OWNER_ID') or user!=os.environ['VK_OWNER_ID'] or str(peer)!=user:
                        self.reply(200,'ok');return
                    if not data.get('event_id'):
                        self.reply(400,'missing event');return
                    event='vk:'+str(data['event_id']);platform='vk'
                else:
                    self.reply(404,'not found');return
                message_text=message.get('text') or message.get('caption') or ''
                has_photo=platform=='telegram' and _telegram_has_image(message)
                if not message_text and not has_photo:
                    self.reply(200,'ok');return
                # Durable duplicate protection for successful deliveries.
                with account_lock(platform+':'+user), sqlite3.connect(database) as db:
                    if db.execute('SELECT 1 FROM delivered WHERE event=?',(event,)).fetchone():
                        self.reply(200,'ok');return
                    cached_reply=_event_data(database,event,'reply')
                    if cached_reply is not None:
                        sender(platform,peer,cached_reply,event)
                        db.execute('INSERT INTO delivered VALUES (?)',(event,))
                        self.reply(200,'ok');return
                    try:
                        if has_photo and not os.environ.get('GIGACHAT_AUTHORIZATION_KEY'):
                            text='Для разбора фото нужен настроенный GigaChat. Фото не отправлено и в дневник ничего не записано.'
                            sender(platform,peer,text,event)
                            db.execute('INSERT INTO delivered VALUES (?)',(event,))
                            self.reply(200,'ok');return
                        image_id=_telegram_photo(message) if has_photo else None
                        owner_key=platform+':'+str(user)
                        if responder is None:
                            try:
                                text=answer(message_text,database=database,event_id=event,owner_key=owner_key,
                                            attachment_ids=[image_id] if image_id else None)
                            finally:
                                if image_id:
                                    from .gigachat_chat import delete_uploaded_file
                                    delete_uploaded_file(image_id)
                        else:
                            text=responder(message_text)
                    except SyncError as exc:
                        text='Не удалось завершить запрос: '+str(exc)+'. Дневник не считаю обновлённым; проверьте /day перед повтором.'
                    except Exception:
                        text='Не удалось обработать сообщение. Проверьте /day перед повторной отправкой, чтобы не создать дубль.'
                    _event_data(database,event,'reply',text)
                    sender(platform,peer,text,event)
                    db.execute('INSERT INTO delivered VALUES (?)',(event,))
                self.reply(200,'ok')
            except (ValueError,TypeError,KeyError):
                self.reply(400,'invalid event')
            except Exception:
                self.reply(503,'temporary error')
    return Handler


def configured_journal(database, account=None):
    storage = os.environ.get('NUTRITION_STORAGE', 'google')
    if storage == 'google':
        from .google_storage import LocalGoogleJournal
        return LocalGoogleJournal(database, account)
    if storage == 'airtable':
        if account and account!='telegram:'+os.environ.get('TELEGRAM_OWNER_ID',''):
            raise SyncError('Архивное хранилище доступно только владельцу')
        return AirtableJournal(Client())  # explicit legacy mode for rollback only
    raise SyncError('Unknown NUTRITION_STORAGE; no automatic fallback')


def main():
    if os.environ.get('NUTRITION_MULTIUSER_ENABLED')=='true' and os.environ.get('NUTRITION_STORAGE','google')!='google':
        raise SystemExit('Multi-user mode requires isolated Google/local storage')
    enabled=False
    for prefix,required in [('TELEGRAM',('BOT_TOKEN','WEBHOOK_SECRET','OWNER_ID')),
                            ('VK',('ACCESS_TOKEN','CALLBACK_SECRET','GROUP_ID','OWNER_ID','CONFIRMATION_CODE'))]:
        if any(os.environ.get(prefix+'_'+key) for key in required):
            if not all(os.environ.get(prefix+'_'+key) for key in required):
                raise SystemExit(prefix+' settings are incomplete')
            enabled=True
    if not enabled:
        raise SystemExit('Configure Telegram or VK credentials in the environment')
    database=os.environ.get('CHANNEL_DB',os.path.join(os.environ.get('DATA_DIR','/app/data'),'channels.sqlite3'))
    os.makedirs(os.path.dirname(os.path.abspath(database)),exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS delivered(event TEXT PRIMARY KEY)')
    if os.environ.get('NUTRITION_STORAGE', 'google') == 'google' and os.environ.get('NUTRITION_PERSISTENT_STORAGE_CONFIRMED') != 'true':
        raise SystemExit('Confirm a mounted persistent DATA_DIR before enabling Google storage')
    journal = configured_journal(database)
    if os.environ.get('NUTRITION_STORAGE', 'google') == 'google':
        from .google_storage import GoogleMirror, start_exporter
        start_exporter(journal, GoogleMirror())
        from .accounts import Accounts
        if os.environ.get('TELEGRAM_OWNER_ID'):
            Accounts(database).start('telegram:'+os.environ['TELEGRAM_OWNER_ID'])
        from .scheduler import start as start_scheduler
        start_scheduler(database,send)
    server=ThreadingHTTPServer((os.environ.get('API_HOST','127.0.0.1'),int(os.environ.get('PORT','8081'))),handler(database))
    if os.environ.get('TELEGRAM_BOT_TOKEN'):
        from .telegram_menu import configure
        menu_ready=configure(os.environ['TELEGRAM_BOT_TOKEN'],os.environ.get('TELEGRAM_OWNER_ID',''))
        print('Telegram menu configured' if menu_ready else 'Telegram menu setup deferred; reply buttons remain available',flush=True)
    print('Nutrition channels ready',flush=True)
    server.serve_forever()


if __name__=='__main__':
    main()
