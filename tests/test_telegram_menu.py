import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from nutrition.channels import answer, send
from nutrition.telegram_menu import configure, keyboard, normalize


class MenuTests(unittest.TestCase):
    def test_navigation_and_guidance_never_write_or_call_ai(self):
        with tempfile.NamedTemporaryFile() as db:
            for text in ('/start','/menu','/help','🍽 Записать еду','📸 Фото еды'):
                result = answer(text, remote_factory=lambda:self.fail('Unexpected diary access'),database=db.name)
                self.assertTrue(result)

    def test_remaining_button_reads_latest_shared_diary(self):
        remote = Mock()
        remote.read.return_value = {'day':'2026-10-06','state':{'fields':{'Version':5}},
            'computed_eaten':{'kcal':1373.4,'protein':92.7,'fat':64,'carbs':108.7},'meals':[]}
        result=answer('🔄 Остаток',remote_factory=lambda:remote)
        self.assertIn('583.6 ккал',result)
        remote.read.assert_called_once()
        remote.upsert.assert_not_called()

    def test_plan_button_routes_without_recording_food(self):
        remote=Mock()
        remote.read.return_value={'day':'2026-10-06'}
        with patch.dict('os.environ',{'GIGACHAT_AUTHORIZATION_KEY':'configured'}), \
             patch('nutrition.gigachat_chat.answer',return_value='План') as chat, \
             patch('nutrition.gigachat_chat.extract_intake') as intake:
            result=answer('🥗 План питания',remote_factory=lambda:remote)
        self.assertEqual(result,'План')
        self.assertIn('остаток текущего дня',chat.call_args.args[0])
        intake.assert_not_called()
        remote.upsert.assert_not_called()
        self.assertEqual(normalize('Съел творог 180 г'),'Съел творог 180 г')

    def test_telegram_reply_has_keyboard_and_preserves_text(self):
        response=Mock()
        response.read.return_value=b'{"ok":true}'
        context=Mock()
        context.__enter__=Mock(return_value=response)
        context.__exit__=Mock(return_value=False)
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':'test'}), \
             patch('nutrition.channels.urlopen',return_value=context) as urlopen:
            send('telegram',42,'Б < 160; текст модели','test-event')
        payload=json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload['text'],'Б < 160; текст модели')
        self.assertNotIn('parse_mode',payload)
        self.assertEqual(len(payload['reply_markup']['keyboard']),3)
        self.assertTrue(payload['reply_markup']['resize_keyboard'])

    def test_dropdown_is_owner_scoped_and_has_no_message_send(self):
        response=Mock()
        response.read.return_value=b'{"ok":true}'
        context=Mock()
        context.__enter__=Mock(return_value=response)
        context.__exit__=Mock(return_value=False)
        with patch('nutrition.telegram_menu.urlopen',return_value=context) as urlopen:
            self.assertTrue(configure('test',42))
        self.assertEqual(urlopen.call_count,2)
        first=json.loads(urlopen.call_args_list[0].args[0].data)
        self.assertEqual(first['scope'],{'type':'chat','chat_id':42})
        self.assertEqual([v['command'] for v in first['commands']],
                         ['menu','day','sync','food','photo','plan','week','help'])
        self.assertFalse(configure('test',''))

    def test_menu_setup_failure_does_not_break_bot(self):
        with patch('nutrition.telegram_menu.urlopen',side_effect=OSError('secret URL')):
            self.assertFalse(configure('test',42))
        self.assertFalse(keyboard()['one_time_keyboard'])
