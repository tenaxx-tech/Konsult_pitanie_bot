import json
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from nutrition.channels import handler


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=str(Path(self.tmp.name)/'events.sqlite3')
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE delivered(event TEXT PRIMARY KEY)')
        self.sent=[]
        self.env=patch.dict(os.environ,dict(TELEGRAM_WEBHOOK_SECRET='secret',TELEGRAM_OWNER_ID='42',
                      VK_CALLBACK_SECRET='vksecret',VK_GROUP_ID='10',VK_OWNER_ID='42',VK_CONFIRMATION_CODE='confirm'))
        self.env.start()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.db,lambda text:'answer',lambda *args:self.sent.append(args)))
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.env.stop();self.tmp.cleanup()

    def call(self,path,data,secret='secret'):
        req=Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=json.dumps(data).encode(),
                    headers={'X-Telegram-Bot-Api-Secret-Token':secret})
        try:
            with urlopen(req) as r:return r.status,r.read().decode()
        except HTTPError as r:return r.code,r.read().decode()

    def event(self,user=42):
        return dict(update_id=1,message={'from':{'id':user},'chat':{'id':user,'type':'private'},'text':'/day'})


    def test_telegram_secret_and_owner(self):
        self.assertEqual(self.call('/telegram/webhook',self.event(),secret='wrong')[0],403)
        self.call('/telegram/webhook',self.event(99))
        self.assertEqual(self.sent,[])

    def test_telegram_retry(self):
        self.call('/telegram/webhook',self.event());self.call('/telegram/webhook',self.event())
        self.assertEqual(len(self.sent),1)

    def test_vk_confirmation(self):
        self.assertEqual(self.call('/vk/webhook',dict(type='confirmation',group_id=10,secret='vksecret')),(200,'confirm'))
        self.assertEqual(self.sent,[])

    def test_vk_retry(self):
        data=dict(type='message_new',group_id=10,secret='vksecret',event_id='test',object={'message':{'from_id':42,'peer_id':42,'text':'/day'}})
        self.call('/vk/webhook',data);self.call('/vk/webhook',data)
        self.assertEqual(len(self.sent),1)



class DialogueConfigurationTests(unittest.TestCase):
    def test_dialogue_reports_missing_groq_key(self):
        from nutrition.channels import answer
        with patch.dict(os.environ, {'GROQ_API_KEY': ''}):
            result = answer('Подскажи меню', remote_factory=lambda: self.fail('Airtable should not be read'))
        self.assertIn('GROQ_API_KEY', result)
        self.assertIn('/day', result)

    def test_empty_calc_returns_usage_without_reading_airtable(self):
        from nutrition.channels import answer
        result = answer('/calc', remote_factory=lambda: self.fail('Airtable should not be read'))
        self.assertIn('Для расчёта укажите порции в JSON', result)
        self.assertIn('/calc {"portions"', result)
        self.assertIn('Дневник при расчёте не изменяется', result)

    def test_calc_with_invalid_json_returns_usage_without_reading_airtable(self):
        from nutrition.channels import answer
        result = answer('/calc {not json}', remote_factory=lambda: self.fail('Airtable should not be read'))
        self.assertIn('Не удалось прочитать данные', result)
        self.assertIn('/calc {"portions"', result)

    def test_calc_with_portion_json_returns_total_without_reading_airtable(self):
        from nutrition.channels import answer
        payload = ('/calc {"portions":[{"name":"Продукт","grams":100,'
                   '"per100":{"kcal":120,"protein":10,"fat":5,"carbs":8},'
                   '"source":"этикетка"}]}')
        result = answer(payload, remote_factory=lambda: self.fail('Airtable should not be read'))
        self.assertEqual(result, '120.0 ккал · Б 10.0 · Ж 5.0 · У 8.0 г')


class SharedDiaryTests(unittest.TestCase):
    def test_sync_rereads_external_updates_without_ai_or_writes(self):
        from nutrition.channels import answer
        from unittest.mock import Mock
        data = {'day':'2026-10-06','state':{'fields':{'Version':5,'CaloriesGoal':1957}},
                'computed_eaten':{'kcal':'1373.4','protein':'92.7','fat':'64','carbs':'108.7'},
                'meals':[{'fields':{'Status':'CONFIRMED'}}]}
        remote = Mock()
        remote.read.side_effect = [data, {**data, 'state':{'fields':{'Version':6,'CaloriesGoal':1957}},
            'computed_eaten':{**data['computed_eaten'],'kcal':'1473.4'}}]
        with patch('nutrition.channels.today',return_value='2026-10-06'), \
             patch('nutrition.gigachat_chat.answer') as ai:
            first = answer('/sync',remote_factory=lambda:remote)
            second = answer('/sync',remote_factory=lambda:remote)
        self.assertIn('583.6 ккал',first)
        self.assertIn('57.3–67.3 г',first)
        self.assertIn('6.0–16.0 г',first)
        self.assertIn('483.6 ккал',second)
        self.assertIn('Версия: 6',second)
        self.assertEqual(remote.read.call_count,2)
        remote.upsert.assert_not_called()
        ai.assert_not_called()

    def test_closed_sync_uses_preserved_totals(self):
        from nutrition.channels import answer
        from unittest.mock import Mock
        remote = Mock()
        remote.read.return_value = {'day':'2026-10-06','state':{'fields':{'Version':7,'Status':'CLOSED',
            'EatenKcal':1800,'EatenProtein':150,'EatenFat':75,'EatenCarbs':130}},
            'computed_eaten':{'kcal':0},'meals':[]}
        result = answer('/sync',remote_factory=lambda:remote)
        self.assertIn('1800 ккал',result)
        self.assertIn('День закрыт',result)
        self.assertNotIn('Остаток калорий',result)
        remote.upsert.assert_not_called()
