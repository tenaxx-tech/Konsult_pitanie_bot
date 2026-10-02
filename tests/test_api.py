import json
from threading import Thread
from http.server import ThreadingHTTPServer
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from nutrition.api import handler


class Tests(unittest.TestCase):
    def setUp(self):
        self.calls=[]
        owner=self
        class Remote:
            def upsert(self,*args):
                owner.calls.append(args)
                return {'saved':True}
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler('test-key',lambda:Remote()))
        self.thread=Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self,path,body,key='test-key'):
        request=Request('http://127.0.0.1:'+str(self.server.server_port)+path,
                        data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+key})
        try:
            with urlopen(request) as r:
                return r.status,json.load(r)
        except HTTPError as r:
            return r.code,json.load(r)

    def test_unauthorized(self):
        self.assertEqual(self.call('/calculate',{},'wrong')[0],401)

    def test_calculate(self):
        data={'portions':[dict(name='test',grams='180',source='fixture',basis='ready',estimated=True,
                              per100=dict(kcal='80',protein='18',fat='.5',carbs='3'))]}
        status,result=self.call('/calculate',data)
        self.assertEqual(status,200)
        self.assertEqual(result['total']['kcal'],'144.0')
        self.assertTrue(result['estimated'])

    def test_confirmation_gate(self):
        data=dict(date='2026-10-03',expected_version=0,item={'Status':'CONFIRMED'})
        self.assertEqual(self.call('/meal',data)[0],400)
        self.assertEqual(self.calls,[])
        data['user_confirmed']=True
        self.assertEqual(self.call('/meal',data)[0],200)

    def test_correction_gate(self):
        data=dict(date='2026-10-03',expected_version=0,item={'Status':'PLANNED'},correction=True)
        self.assertEqual(self.call('/meal',data)[0],400)
        self.assertEqual(self.calls,[])
