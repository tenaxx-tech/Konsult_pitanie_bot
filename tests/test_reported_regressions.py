import io
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError
from nutrition import gigachat_chat as chat
from nutrition.accounts import Accounts
from nutrition.planning_setup import handle
from nutrition.channels import _estimate_reply


def response(payload):
    import json
    r=MagicMock();r.__enter__.return_value=io.BytesIO(json.dumps(payload).encode());return r


class ReportedRegressions(unittest.TestCase):
    def tearDown(self):chat._clear_token()

    def test_epoch_milliseconds_expire_in_seconds(self):
        chat._clear_token()
        with patch.dict('os.environ',GIGACHAT_AUTHORIZATION_KEY='test'), patch.object(chat.time,'time',return_value=1800000000), patch.object(chat,'urlopen',return_value=response({'access_token':'one','expires_at':1800001800000})):
            chat._get_access_token()
            self.assertEqual(chat._token_expires_at,1800001800)
        with patch.dict('os.environ',GIGACHAT_AUTHORIZATION_KEY='test'), patch.object(chat.time,'time',return_value=1800001801), patch.object(chat,'urlopen',return_value=response({'access_token':'two','expires_at':1800003601000})) as call:
            self.assertEqual(chat._get_access_token(),'two');self.assertEqual(call.call_count,1)

    def test_upload_refreshes_401_once_with_same_file(self):
        with patch.object(chat,'_get_access_token',side_effect=['old','new']),patch.object(chat,'_clear_token') as clear,patch.object(chat,'urlopen',side_effect=[HTTPError('private',401,'private',{},None),response({'id':'file_12345678'})]) as call:
            self.assertEqual(chat.upload_image(b'fake-image'),'file_12345678')
            self.assertEqual(clear.call_count,1)
            self.assertEqual(call.call_args_list[0].args[0].data,call.call_args_list[1].args[0].data)
            self.assertEqual(call.call_args_list[1].args[0].get_header('Authorization'),'Bearer new')

    def test_second_401_stops_without_loop_or_secret(self):
        with patch.object(chat,'_get_access_token',return_value='secret'),patch.object(chat,'_clear_token'),patch.object(chat,'urlopen',side_effect=HTTPError('secret',401,'secret',{},None)) as call:
            with self.assertRaises(chat.SyncError) as caught:chat.upload_image(b'fake-image')
            self.assertEqual(call.call_count,2);self.assertNotIn('secret',str(caught.exception))

    def test_bare_food_list_and_followup_keep_context_until_intent_chosen(self):
        with tempfile.TemporaryDirectory() as d:
            users=Accounts(d+'/db');u=users.start('telegram:2');p=u['profile'];p['planning_setup']=True
            users.save('telegram:2',p,'ready')
            remote=MagicMock()
            question=handle(users,'telegram:2','картофель печеный дольками с нагетсами,огурцы','2026-10-20',lambda:remote)
            self.assertIn('уже их съели',question)
            self.assertIn('рассчитаю сам',handle(users,'telegram:2','не знаю калорийность, посчитай сам','2026-10-20',lambda:remote))
            handle(users,'telegram:2','150 г картофеля, 50 г наггетсов, огурец 100 г','2026-10-20',lambda:remote)
            result=handle(users,'telegram:2','🍽 Уже съел(а)','2026-10-20',lambda:remote)
            self.assertIn('Съел:',result['intake_text']);self.assertIn('150 г',result['intake_text'])
            remote.assert_not_called()
            self.assertNotIn('food_context_question',users.get('telegram:2')['profile'])

    def test_estimate_totals_are_not_model_prose(self):
        items=[dict(name='Картофель',grams=150,kcal=180,protein=3,fat=4,carbs=33),
               dict(name='Наггетсы',grams=50,kcal=150,protein=8,fat=9,carbs=9),
               dict(name='Огурец',grams=100,kcal=15,protein=1,fat=0,carbs=3)]
        text=_estimate_reply(items)
        self.assertIn('345.0 ккал',text);self.assertIn('Б 12.0',text);self.assertNotIn('половин',text)
