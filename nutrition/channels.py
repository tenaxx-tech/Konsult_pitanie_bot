"""Telegram/VK webhook gateway; owner-only, structured commands in this version."""
from datetime import datetime
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from .api import calculate
from .airtable import AirtableJournal, Client, SyncError

LOCK=Lock()


def today():
    return datetime.now(ZoneInfo(os.environ.get('NUTRITION_TIMEZONE','Asia/Omsk'))).date().isoformat()


def answer(text, remote_factory=lambda:AirtableJournal(Client())):
    command, _, payload=text.strip().partition(' ')
    command=command.lower()
    if command in ('/start','/help','помощь'):
        return 'Дневник питания: /day — сегодня; /calc JSON — расчёт порций. Свободный диалог и фото пока не подключены.'
    if command in ('/day','сегодня'):
        data=remote_factory().read(today())
        state=data['state']['fields'] if data['state'] else {}
        status=state.get('Status')
        if isinstance(status,dict):
            status=status.get('name')
        if status=='CLOSED':
            values={k:str(state.get(n,'нет данных')) for k,n in zip(('kcal','protein','fat','carbs'),('EatenKcal','EatenProtein','EatenFat','EatenCarbs'))}
        else:
            values=data['computed_eaten']
        return (f"Дата: {data['day']}\nФакт: {values['kcal']} ккал · Б {values['protein']} · Ж {values['fat']} · У {values['carbs']} г\n"
                f"Позиций: {len(data['meals'])}. Версия: {state.get('Version',0)}.")
    if command=='/calc':
        result=calculate(json.loads(payload)['portions'])
        n=result['total']
        return f"{'≈ ' if result['estimated'] else ''}{n['kcal']} ккал · Б {n['protein']} · Ж {n['fat']} · У {n['carbs']} г"
    return 'Свободный диалог пока не подключён. Отправьте /day для дневника или /help для команд.'


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


def handler(database, responder=answer, sender=send):
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
                if not message.get('text'):
                    self.reply(200,'ok');return
                # Durable duplicate protection for successful deliveries.
                with LOCK, sqlite3.connect(database) as db:
                    if db.execute('SELECT 1 FROM delivered WHERE event=?',(event,)).fetchone():
                        self.reply(200,'ok');return
                    try:
                        text=responder(message['text'])
                    except Exception:
                        text='Не удалось прочитать или рассчитать данные. Дневник не изменён.'
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
    database=os.environ.get('CHANNEL_DB','channels.sqlite3')
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE IF NOT EXISTS delivered(event TEXT PRIMARY KEY)')
    server=ThreadingHTTPServer((os.environ.get('API_HOST','127.0.0.1'),int(os.environ.get('PORT','8081'))),handler(database))
    print('Nutrition channels ready',flush=True)
    server.serve_forever()


if __name__=='__main__':
    main()
