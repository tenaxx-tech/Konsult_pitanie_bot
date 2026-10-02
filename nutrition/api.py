"""Small authenticated API for GPT Actions; deploy behind HTTPS."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Lock
from urllib.parse import parse_qs, urlsplit
from .core import Portion, Nutrients
from .airtable import AirtableJournal, Client, SyncError

WRITE_LOCK = Lock()
MAX_BODY = 131072


def calculate(portions):
    if not isinstance(portions, list) or not 1 <= len(portions) <= 100:
        raise ValueError('Expected 1 to 100 portions')
    items = [Portion.from_record(p) for p in portions]
    return {'total': sum((p.total for p in items), Nutrients()).values(True),
            'estimated': any(p.estimated for p in items),
            'portions': [dict(name=p.name,total=p.total.values(True)) for p in items]}


def handler(api_key, remote_factory=lambda: AirtableJournal(Client())):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            # Never log bodies, authorization headers or personal diary data.
            pass

        def reply(self, status, payload):
            body=json.dumps(payload,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Cache-Control','no-store')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            value=self.headers.get('Authorization','')
            return hmac.compare_digest(value.encode(), ('Bearer '+api_key).encode())

        def body(self):
            size=int(self.headers.get('Content-Length','0'))
            if not 0 < size <= MAX_BODY:
                raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(size))
            if not isinstance(data,dict):
                raise ValueError('Expected JSON object')
            return data

        def dispatch(self, method):
            if not self.authorized():
                self.reply(401,{'error':'Unauthorized'})
                return
            try:
                url=urlsplit(self.path)
                if method=='POST' and url.path=='/calculate':
                    result=calculate(self.body()['portions'])
                elif method=='GET' and url.path=='/day':
                    day=parse_qs(url.query).get('date',[''])[0]
                    result=remote_factory().read(day)
                elif method=='POST' and url.path=='/meal':
                    data=self.body()
                    if not isinstance(data.get('expected_version'),int) or isinstance(data['expected_version'],bool) or data['expected_version']<0:
                        raise ValueError('expected_version must be a nonnegative integer')
                    item=data['item']
                    if item.get('Status')=='CONFIRMED' and data.get('user_confirmed') is not True:
                        raise ValueError('CONFIRMED requires explicit user confirmation')
                    correction=data.get('correction',False)
                    if not isinstance(correction,bool):
                        raise ValueError('correction must be boolean')
                    if correction and data.get('user_requested_correction') is not True:
                        raise ValueError('Correction requires explicit user request')
                    with WRITE_LOCK:
                        result=remote_factory().upsert(data['date'],item,data['expected_version'],correction)
                else:
                    self.reply(404,{'error':'Not found'})
                    return
                self.reply(200,result)
            except SyncError as exc:
                self.reply(409,{'error':str(exc),'instruction':'Reread the day before retrying; a meal may have been saved.'})
            except (ValueError,KeyError,TypeError):
                self.reply(400,{'error':'Invalid input; check the action schema and explicit confirmations'})
            except Exception:
                self.reply(500,{'error':'Internal error; reread before retrying a write'})

        def do_GET(self):
            self.dispatch('GET')

        def do_POST(self):
            self.dispatch('POST')
    return Handler


def main():
    key=os.environ.get('NUTRITION_API_KEY','')
    if len(key)<32:
        raise SystemExit('Set NUTRITION_API_KEY to a random secret of at least 32 characters')
    Client()  # Validate required environment settings before serving requests.
    server=ThreadingHTTPServer((os.environ.get('API_HOST','127.0.0.1'),int(os.environ.get('PORT','8080'))),handler(key))
    print('Nutrition API ready',flush=True)
    server.serve_forever()


if __name__=='__main__':
    main()
