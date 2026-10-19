from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from nutrition.accounts import Accounts,route,estimate_goals
from nutrition.airtable import SyncError
from nutrition.core import Portion,Nutrients
from nutrition.diary_service import mutate
from nutrition.diary_commands import route as diary_route,pending_close
from nutrition.google_storage import LocalGoogleJournal
from nutrition.scheduler import tick


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=str(Path(self.tmp.name)/'test.sqlite3')
        self.env=patch.dict('os.environ',{'TELEGRAM_OWNER_ID':'42'});self.env.start()
        self.users=Accounts(self.db);self.users.start('telegram:42')
        self.j=LocalGoogleJournal(self.db,'telegram:42');self.day='2026-10-19'
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def item(self,key='one',status='PLANNED',grams=200):
        return dict(MealItemKey=self.day+'|LUNCH|'+key,Meal='LUNCH',Status=status,Source='ESTIMATE',
                    portion=Portion('Fixture',grams,Nutrients(100,10,4,6),'Fixture',estimated=True).record())
    def add(self,status='PLANNED'):
        return mutate(self.j,self.day,'add','add:1',0,items=[self.item(status=status)])
    def test_bulk_invalid_item_is_atomic(self):
        bad=self.item('two');bad['portion']['per100']['kcal']='900'
        with self.assertRaises(SyncError):mutate(self.j,self.day,'add','invalid',0,items=[self.item(),bad])
        self.assertEqual(self.j.read(self.day)['meals'],[])
    def test_add_repeat_uses_original_identity_despite_new_version(self):
        self.add();mutate(self.j,self.day,'add','add:1',1,items=[self.item()])
        self.assertEqual(self.j.read(self.day)['state']['fields']['Version'],1)
        with self.assertRaises(SyncError):mutate(self.j,self.day,'add','add:1',1,items=[self.item('different')])
    def test_partial_confirmation_keeps_remainder(self):
        self.add();mutate(self.j,self.day,'confirm','confirm',1,item_key=self.item()['MealItemKey'],grams=80)
        s=self.j.read(self.day)
        self.assertEqual(len(s['meals']),2)
        self.assertEqual(float(s['computed_eaten']['kcal']),80)
        self.assertEqual(float(s['computed_forecast']['kcal']),200)
        self.assertEqual(s['meals'][1]['fields']['Grams'],120)
    def test_confirm_above_plan_and_stale_version_rejected(self):
        self.add()
        with self.assertRaises(SyncError):mutate(self.j,self.day,'confirm','c',1,item_key=self.item()['MealItemKey'],grams=300)
        with self.assertRaises(SyncError):mutate(self.j,self.day,'correct','f',0,item_key=self.item()['MealItemKey'],grams=100)
        self.assertEqual(self.j.read(self.day)['state']['fields']['Version'],1)
    def test_close_refuses_planned_and_cancel_preserves_history(self):
        self.add()
        with self.assertRaises(SyncError):mutate(self.j,self.day,'close','close',1)
        mutate(self.j,self.day,'cancel','cancel',1,item_key=self.item()['MealItemKey'])
        self.assertEqual(self.j.read(self.day)['meals'][0]['fields']['Status'],'CANCELLED')
    def test_close_consent_then_correct_without_reopening(self):
        self.add('CONFIRMED')
        q=diary_route(self.j,'/close',self.day,'close:request')
        self.assertIn('Ответьте «Да»',q)
        self.assertEqual(self.j.read(self.day)['state']['fields']['Status'],'ACTIVE')
        self.assertIn('День закрыт',pending_close(self.j,'Да','close:yes'))
        mutate(self.j,self.day,'correct','correct',2,item_key=self.item()['MealItemKey'],grams=100)
        s=self.j.read(self.day)
        self.assertEqual(s['state']['fields']['Status'],'CLOSED')
        self.assertEqual(float(s['computed_eaten']['kcal']),100)
        self.assertEqual(mutate(self.j,self.day,'close','close:again',3)['state']['fields']['Version'],3)
    def test_close_does_not_accept_stale_confirmation(self):
        self.add('CONFIRMED');diary_route(self.j,'/close',self.day,'close:request')
        mutate(self.j,self.day,'correct','c',1,item_key=self.item()['MealItemKey'],grams=150)
        with self.assertRaises(SyncError):pending_close(self.j,'Да','answer')
        self.assertEqual(self.j.read(self.day)['state']['fields']['Status'],'ACTIVE')
    def test_other_user_cannot_read_or_change_owner(self):
        self.add('CONFIRMED')
        other=LocalGoogleJournal(self.db,'telegram:99')
        self.assertEqual(other.read(self.day)['meals'],[])
        with self.assertRaises(SyncError):mutate(other,self.day,'correct','x',0,item_key=self.item()['MealItemKey'],grams=150)
        self.assertEqual(self.j.read(self.day)['state']['fields']['Version'],1)
    def test_profile_goals_apply_to_new_diary(self):
        u=self.users.start('telegram:99');p=u['profile'];p.update(goals=dict(CaloriesGoal=2200,ProteinMin=100,ProteinMax=110,FatMin=60,FatMax=70,CarbMax=250))
        self.users.save('telegram:99',p,'ready')
        s=LocalGoogleJournal(self.db,'telegram:99').read(self.day)
        self.assertEqual(s['state']['fields']['CaloriesGoal'],2200)
        self.assertEqual(self.j.read(self.day)['state']['fields']['CaloriesGoal'],1957)
    def test_trial_never_resets_and_admin_is_free(self):
        now=datetime(2026,10,1,tzinfo=timezone.utc)
        u=self.users.start('telegram:99',now);p=u['profile'];p.update(consent=True,goals={})
        self.users.save('telegram:99',p,'ready')
        self.assertTrue(self.users.active('telegram:99',now+timedelta(hours=71)))
        self.assertFalse(self.users.active('telegram:99',now+timedelta(hours=72)))
        self.assertEqual(self.users.start('telegram:99',now+timedelta(days=5))['trial_end'],u['trial_end'])
        self.assertTrue(self.users.active('telegram:42',now+timedelta(days=10000)))
    def test_onboarding_requires_consent_and_completes_separately(self):
        self.assertIn('/agree',route(self.users,'telegram:99','/start'))
        route(self.users,'telegram:99','/agree')
        for text in ('180','85','35','м','средняя','снижение','без орехов','Europe/Moscow'):
            reply=route(self.users,'telegram:99',text)
        self.assertIn('/accept',reply)
        route(self.users,'telegram:99','/accept')
        self.assertEqual(self.users.get('telegram:99')['stage'],'ready')
        self.assertEqual(self.users.get('telegram:99')['profile']['timezone'],'Europe/Moscow')
        self.assertEqual(self.users.get('telegram:42')['profile']['goals']['CaloriesGoal'],1957)
    def test_schedules_use_local_time_and_do_not_double_send(self):
        now=datetime(2026,10,19,3,tzinfo=timezone.utc);sent=[]
        self.assertEqual(tick(self.db,lambda *x:sent.append(x),now),1)
        self.assertEqual(tick(self.db,lambda *x:sent.append(x),now),0)
        self.assertIn('Доброе утро',sent[0][2])
        tick(self.db,lambda *x:sent.append(x),now.replace(hour=16,minute=30))
        self.assertIn('/close',sent[1][2])
        self.assertEqual(self.j.read(self.day)['meals'],[])
    def test_uncertain_notification_not_retried_and_off_is_respected(self):
        now=datetime(2026,10,19,3,tzinfo=timezone.utc)
        def fail(*args):raise OSError('offline')
        tick(self.db,fail,now)
        self.assertEqual(tick(self.db,lambda *x:self.fail('duplicate'),now),0)
        route(self.users,'telegram:42','/notifications off')
        self.assertEqual(tick(self.db,lambda *x:self.fail('disabled'),now+timedelta(days=1)),0)
    def test_legacy_conversation_belongs_only_to_owner(self):
        from nutrition.conversations import load,save
        with sqlite3.connect(self.db) as db:
            db.execute('CREATE TABLE conversation(id INTEGER PRIMARY KEY,day TEXT,role TEXT,content TEXT)')
            db.execute('INSERT INTO conversation(day,role,content) VALUES(?,?,?)',(self.day,'user','private owner text'))
        self.assertEqual(load(self.db,self.day,'telegram:99'),[])
        self.assertEqual(load(self.db,self.day,'telegram:42'),[('user','private owner text')])
        save(self.db,self.day,'telegram:99','private other','reply')
        self.assertEqual(len(load(self.db,self.day,'telegram:42')),1)
    def test_mass_correction_and_history_commands_are_local(self):
        self.add('CONFIRMED')
        with patch('nutrition.google_storage.GoogleMirror',side_effect=AssertionError('network')):
            self.assertIn('100.0 ккал',diary_route(self.j,'/correct 1 100',self.day,'c'))
            self.assertIn(self.day,diary_route(self.j,'/history',self.day,'h'))

    def test_saved_plan_can_be_confirmed_by_short_reply_once(self):
        from nutrition.channels import answer
        data=dict(action='plan',reply='План',day=self.day,meal='LUNCH',items=[dict(name='Fixture',grams=200,kcal=200,protein=20,fat=8,carbs=12)])
        with patch.dict('os.environ',{'GIGACHAT_AUTHORIZATION_KEY':'fixture'}),patch('nutrition.gigachat_chat.extract_intake',return_value=data):
            answer('Запланируй еду',lambda:self.j,self.db,'tg:plan','telegram:42')
        answer('Съел',lambda:self.j,self.db,'tg:confirm','telegram:42')
        snapshot=self.j.read(self.day)
        self.assertEqual(len(snapshot['meals']),1)
        self.assertEqual(snapshot['meals'][0]['fields']['Status'],'CONFIRMED')
        self.assertEqual(snapshot['state']['fields']['Version'],2)

    def test_clarification_resumes_original_intake_without_duplicate(self):
        from nutrition.channels import answer
        clarification=dict(action='clarify',reply='После варки?',day=self.day,meal='LUNCH',items=[])
        ready=dict(action='record',reply='Оценка',day=self.day,meal='LUNCH',items=[dict(name='Пельмени',grams=360,kcal=432,protein=18,fat=10.8,carbs=54)])
        with patch.dict('os.environ',{'GIGACHAT_AUTHORIZATION_KEY':'fixture'}),patch('nutrition.gigachat_chat.extract_intake',side_effect=[clarification,ready]) as extract:
            answer('Съел пельмени 360 г',lambda:self.j,self.db,'tg:food','telegram:42')
            answer('после варки',lambda:self.j,self.db,'tg:clarify','telegram:42')
        self.assertIn('Съел пельмени 360 г',extract.call_args.args[0])
        self.assertAlmostEqual(float(self.j.read(self.day)['computed_eaten']['kcal']),788.4)
        with patch.dict('os.environ',{'GIGACHAT_AUTHORIZATION_KEY':'fixture'}),patch('nutrition.gigachat_chat.extract_intake') as extract:
            answer('после варки',lambda:self.j,self.db,'tg:clarify','telegram:42')
            extract.assert_not_called()
        self.assertEqual(len(self.j.read(self.day)['meals']),1)

    def test_trial_expired_can_read_but_cannot_add(self):
        from nutrition.channels import answer
        u=self.users.start('telegram:99',datetime(2020,1,1,tzinfo=timezone.utc))
        p=u['profile'];p.update(goals=dict(CaloriesGoal=2000,ProteinMin=100,ProteinMax=110,FatMin=60,FatMax=70,CarbMax=200),consent=True)
        self.users.save('telegram:99',p,'ready')
        with patch('nutrition.gigachat_chat.extract_intake',side_effect=AssertionError('expired access')):
            self.assertIn('72 часа завершились',answer('Съел еду',database=self.db,event_id='tg:x',owner_key='telegram:99'))
            self.assertIn('Дата:',answer('/day',database=self.db,event_id='tg:y',owner_key='telegram:99'))
