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
    return has_attachment or _explicit_consumption(text) or bool(re.search(
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


def _record_items(remote, day, meal, items, event_id, status):
    from .core import Nutrients, Portion
    from .airtable import select
    snapshot=remote.read(day)
    state=snapshot['state']['fields'] if snapshot['state'] else {}
    version=state.get('Version',0)
    event_hash=hashlib.sha256(event_id.encode()).hexdigest()[:20]
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
        source='AI estimate' if estimated else 'User-provided label'
        portion=Portion(str(item['name'])[:160],grams,Nutrients(**per100),source,'ready',estimated)
        key=f'{day}|{meal}|{event_hash}_{index}'
        payload={'MealItemKey':key,'Meal':meal,'Status':status,'Source':'ESTIMATE' if estimated else 'LABEL',
                 'portion':portion.record()}
        result=remote.upsert(day,payload,version)
        state=result['state']['fields'] if result['state'] else {}
        version=state.get('Version',version+1)
    return remote.read(day)


def _intake_reply(result, items, status):
    names=', '.join(str(x['name']) for x in items)
    total={k:sum((Decimal(str(x[k])) for x in items),Decimal(0)) for k in ('kcal','protein','fat','carbs')}
    estimate=' (оценка)' if any(x.get('estimated') is not False for x in items) else ''
    action='Записал в дневник' if status=='CONFIRMED' else 'Добавил в план'
    fields=result['state']['fields'] if result.get('state') else {}
    version=fields.get('Version',0)
    return (f'{action}: {names}{estimate}. Итого: {total["kcal"]:.1f} ккал · '
            f'Б {total["protein"]:.1f} · Ж {total["fat"]:.1f} · У {total["carbs"]:.1f} г. '
            f'Проверено чтением Airtable, версия {version}.')


def answer(text, remote_factory=lambda:AirtableJournal(Client()), database=None,
           event_id='manual-event', owner_key=None, attachment_ids=None):
    database=database or os.environ.get('CHANNEL_DB','channels.sqlite3')
    command, _, payload=text.strip().partition(' ')
    command=command.lower()
    pending=_pending(database,owner_key)
    if pending and _affirmative(text):
        result=_record_items(remote_factory(),pending['day'],pending['meal'],pending['items'],pending['event_id'],'CONFIRMED')
        _clear_pending(database,owner_key)
        return _intake_reply(result,pending['items'],'CONFIRMED')
    if pending and _negative(text):
        _clear_pending(database,owner_key)
        return 'Понял, ничего не записывал.'
    if command in ('/start','/help','помощь'):
        return 'Консультант по питанию: /day — дневник; /sync — общий дневник и остатки; /calc JSON — расчёт. Можно задавать вопросы обычным текстом. Напишите, что съели, или пришлите фото и подтвердите запись словом «Съел».'
    if command in ('/day','/sync','сегодня'):
        data=remote_factory().read(today())
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
            return (reply + f"\nПодтверждено позиций: {confirmed}. Общий дневник Airtable перечитан."
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
    if _intake_candidate(text, bool(attachment_ids)) and not os.environ.get('GIGACHAT_AUTHORIZATION_KEY'):
        return 'Для автоматического разбора и записи еды нужен подключённый GigaChat. Сейчас данные в Airtable не записаны; проверьте секрет GIGACHAT_AUTHORIZATION_KEY на BotHost.'
    if os.environ.get('GIGACHAT_AUTHORIZATION_KEY'):
        from .gigachat_chat import answer as chat
        is_explicit=_explicit_consumption(text)
        if _intake_candidate(text, bool(attachment_ids)):
            from .gigachat_chat import extract_intake
            snapshot=remote_factory().read(today())
            data=extract_intake(text,snapshot['day'],snapshot.get('protocols'),attachment_ids)
            for item in data.get('items',[]):
                item['estimated']=True
            day=data['day'] or snapshot['day']
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):
                raise SyncError('GigaChat returned an invalid date')
            from datetime import date
            date.fromisoformat(day)
            if data['action']=='clarify' or not data['items']:
                return data.get('reply') or 'Уточните, пожалуйста, продукт и количество.'
            if attachment_ids and not is_explicit:
                if not owner_key:
                    return data.get('reply') or 'Напишите «съел», если это нужно подтвердить и внести в дневник.'
                _set_pending(database,owner_key,day,data['meal'],data['items'],event_id)
                return (data.get('reply') or 'Распознал еду на фото.') + '\nЕсли вы это съели, ответьте «Съел»; для отмены — «Отмена». Пока в дневник не записывал.'
            if is_explicit and data['action']=='record':
                result=_record_items(remote_factory(),day,data['meal'],data['items'],event_id,'CONFIRMED')
                return _intake_reply(result,data['items'],'CONFIRMED')
            if data['action']=='plan' and re.search(r'\b(план|запланир|в\s+план)\w*',text.casefold()):
                result=_record_items(remote_factory(),day,data['meal'],data['items'],event_id,'PLANNED')
                if owner_key:
                    _set_pending(database,owner_key,day,data['meal'],data['items'],event_id)
                return _intake_reply(result,data['items'],'PLANNED')+'\nЧтобы отметить план как съеденный, ответьте «Съел».'
            if data['action']=='proposal' and owner_key:
                _set_pending(database,owner_key,day,data['meal'],data['items'],event_id)
                return (data.get('reply') or 'Подготовил оценку.') + '\nОтветьте «Съел» для записи или «Отмена», если не нужно вносить.'
        return chat(text, remote_factory().read(today()), database)
    if os.environ.get('GEMINI_API_KEY'):
        from .gemini_chat import answer as chat
        return chat(text, remote_factory().read(today()), os.environ.get('CHANNEL_DB','channels.sqlite3'))
    if os.environ.get('GROQ_API_KEY'):
        from .groq_chat import answer as chat
        return chat(text, remote_factory().read(today()), os.environ.get('CHANNEL_DB','channels.sqlite3'))
    return 'ИИ пока не настроен: добавьте GIGACHAT_AUTHORIZATION_KEY (GigaChat), GEMINI_API_KEY (Gemini) или GROQ_API_KEY (Groq) в секреты сервера. /day и /calc доступны.'


def send(platform,peer,text,event_id):
    if platform=='telegram':
        token=os.environ['TELEGRAM_BOT_TOKEN']
        url='https://api.telegram.org/bot'+token+'/sendMessage'
        body=json.dumps({'chat_id':peer,'text':text}).encode()
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


def _telegram_photo(message):
    photos=message.get('photo') or []
    if not photos:
        return None
    token=os.environ.get('TELEGRAM_BOT_TOKEN','')
    if not token:
        raise SyncError('Telegram photo download is not configured')
    import mimetypes
    from .gigachat_chat import upload_image
    file_id=photos[-1].get('file_id')
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
    mime=content_type if content_type.startswith('image/') else mimetypes.guess_type(path)[0]
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
                    if not allowed or user!=allowed or message.get('chat',{}).get('type')!='private':
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
                has_photo=platform=='telegram' and bool(message.get('photo'))
                if not message_text and not has_photo:
                    self.reply(200,'ok');return
                # Durable duplicate protection for successful deliveries.
                with LOCK, sqlite3.connect(database) as db:
                    if db.execute('SELECT 1 FROM delivered WHERE event=?',(event,)).fetchone():
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
                    sender(platform,peer,text,event)
                    db.execute('INSERT INTO delivered VALUES (?)',(event,))
                self.reply(200,'ok')
            except (ValueError,TypeError,KeyError):
                self.reply(400,'invalid event')
            except Exception:
                self.reply(503,'temporary error')
    return Handler


def main():
    enabled=False
    for prefix,required in [('TELEGRAM',('BOT_TOKEN','WEBHOOK_SECRET','OWNER_ID')),
                            ('VK',('ACCESS_TOKEN','CALLBACK_SECRET','GROUP_ID','OWNER_ID','CONFIRMATION_CODE'))]:
        if any(os.environ.get(prefix+'_'+key) for key in required):
            if not all(os.environ.get(prefix+'_'+key) for key in required):
                raise SystemExit(prefix+' settings are incomplete')
            enabled=True
    if not enabled:
        raise SystemExit('Configure Telegram or VK credentials in the environment')
    Client()
    database=os.environ.get('CHANNEL_DB',os.path.join(os.environ.get('DATA_DIR','/app/data'),'channels.sqlite3'))
    os.makedirs(os.path.dirname(os.path.abspath(database)),exist_ok=True)
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS delivered(event TEXT PRIMARY KEY)')
    server=ThreadingHTTPServer((os.environ.get('API_HOST','127.0.0.1'),int(os.environ.get('PORT','8081'))),handler(database))
    print('Nutrition channels ready',flush=True)
    server.serve_forever()


if __name__=='__main__':
    main()
