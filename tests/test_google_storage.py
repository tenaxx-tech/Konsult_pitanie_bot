import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from nutrition.airtable import SyncError
from nutrition.core import Portion, Nutrients
from nutrition.google_storage import LocalGoogleJournal
from nutrition.channels import answer, configured_journal

class Mirror:
    def __init__(self): self.rows={}; self.fail=False; self.hook=None
    def put_verified(self,row,values):
        self.rows[row]=values
        if self.hook: self.hook(); self.hook=None
        if self.fail: raise TimeoutError('after remote write')

class GoogleStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=str(Path(self.tmp.name)/'diary.sqlite3')
        self.j=LocalGoogleJournal(self.db,'telegram:42');self.day='2026-10-19';self.m=Mirror()
    def tearDown(self): self.tmp.cleanup()
    def item(self,status='CONFIRMED',suffix='one'):
        return dict(MealItemKey=self.day+'|BREAKFAST|'+suffix,Meal='BREAKFAST',Status=status,Source='ESTIMATE',
                    portion=Portion('Food',100,Nutrients(100,10,3,8),'reference',estimated=True).record())
    def add(self,status='CONFIRMED',suffix='one',version=0): return self.j.upsert(self.day,self.item(status,suffix),version)
    def test_local_commit_without_network(self):
        self.add();self.assertEqual(len(self.j.pending()),1);self.assertEqual(self.j.read(self.day)['computed_eaten']['kcal'],'100.0')
    def test_restart_preserves_pending(self):
        self.add();new=LocalGoogleJournal(self.db,'telegram:42');self.assertEqual(len(new.pending()),1);self.assertEqual(len(new.read(self.day)['meals']),1)
    def test_repeat_no_version_increase(self):
        self.add();s=self.j.upsert(self.day,self.item(),1);self.assertEqual(s['state']['fields']['Version'],1);self.assertEqual(len(s['meals']),1)
    def test_stale_version_no_partial_change(self):
        self.add()
        with self.assertRaises(SyncError):self.add(suffix='two')
        self.assertEqual(len(self.j.read(self.day)['meals']),1)
    def test_confirm_plan_reuses_row(self):
        self.add('PLANNED');s=self.j.upsert(self.day,self.item(),1);self.assertEqual(len(s['meals']),1);self.assertEqual(s['computed_eaten']['kcal'],'100.0')
    def test_plan_not_eaten(self):
        s=self.add('PLANNED');self.assertEqual(s['computed_eaten']['kcal'],'0');self.assertEqual(s['computed_forecast']['kcal'],'100.0')
    def test_other_account_isolated(self):
        self.add();other=LocalGoogleJournal(self.db,'telegram:99');self.assertEqual(other.read(self.day)['meals'],[])
        other.upsert(self.day,self.item(),0);self.assertEqual(len(self.j.pending()),2)
    def test_response_loss_retry_same_row(self):
        self.add();self.m.fail=True;self.assertEqual(self.j.sync_once(self.m),0);self.assertEqual(len(self.j.pending()),1)
        self.m.fail=False;self.assertEqual(self.j.sync_once(self.m),1);self.assertEqual(len(self.m.rows),1);self.assertEqual(self.j.pending(),[])
    def test_new_version_during_sync_stays_pending(self):
        self.add();self.m.hook=lambda:self.add(suffix='two',version=1);self.j.sync_once(self.m);self.assertEqual(len(self.j.pending()),1)
        self.j.sync_once(self.m);self.assertEqual(self.j.pending(),[]);self.assertEqual(len(json.loads(self.m.rows[2][3])['meals']),2)
    def test_invalid_data_not_saved(self):
        item=self.item();item['portion']['grams']='NaN'
        with self.assertRaises(ValueError):self.j.upsert(self.day,item,0)
        self.assertEqual(self.j.pending(),[])
    def test_google_never_constructs_airtable(self):
        with patch.dict(os.environ,{'NUTRITION_STORAGE':'google','TELEGRAM_OWNER_ID':'42'}),patch('nutrition.channels.Client',side_effect=AssertionError('Airtable called')):
            self.assertIsInstance(configured_journal(self.db),LocalGoogleJournal)
    def test_intake_pipeline_without_network_storage(self):
        data=dict(action='record',reply='ok',day=self.day,meal='BREAKFAST',items=[dict(name='Food',grams=100,kcal=100,protein=10,fat=3,carbs=8)])
        with patch.dict(os.environ,{'GIGACHAT_AUTHORIZATION_KEY':'test'}),patch('nutrition.gigachat_chat.extract_intake',return_value=data):
            reply=answer('Съел еду',lambda:self.j,self.db,'telegram:123','telegram:42')
        self.assertIn('Сохранено локально',reply);self.assertNotIn('Проверено чтением Airtable',reply);self.assertEqual(len(self.j.pending()),1)

class GoogleTransportTests(unittest.TestCase):
    def mirror(self, responses):
        from nutrition.google_storage import GoogleMirror
        from unittest.mock import Mock
        transport=GoogleMirror.__new__(GoogleMirror)
        transport.base='https://sheets.googleapis.com/v4/spreadsheets/test/values/'
        transport.verified=False
        transport.session=Mock()
        transport.get=Mock(side_effect=responses)
        return transport
    def test_headers_must_match_before_write(self):
        t=self.mirror([[['wrong']]])
        with self.assertRaises(SyncError):t.put_verified(2,['owner','day',1,'{}','now'])
        t.session.put.assert_not_called()
    def test_conflicting_owner_not_overwritten(self):
        from nutrition.google_storage import HEADERS
        t=self.mirror([[HEADERS],[['other','day',1,'{}','now']]])
        with self.assertRaises(SyncError):t.put_verified(2,['owner','day',1,'{}','now'])
        t.session.put.assert_not_called()
    def test_same_revision_other_payload_not_overwritten(self):
        from nutrition.google_storage import HEADERS
        t=self.mirror([[HEADERS],[['owner','day',1,'different','now']]])
        with self.assertRaises(SyncError):t.put_verified(2,['owner','day',1,'{}','now'])
        t.session.put.assert_not_called()
    def test_raw_write_and_exact_readback(self):
        from nutrition.google_storage import HEADERS
        values=['owner','day',1,'=not_a_formula','now']
        t=self.mirror([[HEADERS],[],[['owner','day',1,'=not_a_formula','now']]])
        t.put_verified(2,values)
        self.assertEqual(t.session.put.call_args.kwargs['params'],{'valueInputOption':'RAW'})
    def test_readback_failure_is_not_success(self):
        from nutrition.google_storage import HEADERS
        t=self.mirror([[HEADERS],[],[['owner','day',0,'{}','now']]])
        with self.assertRaises(SyncError):t.put_verified(2,['owner','day',1,'{}','now'])
