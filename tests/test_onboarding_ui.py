import json
import os
import tempfile
import unittest
from unittest.mock import patch, Mock
from zoneinfo import ZoneInfo
from nutrition.accounts import Accounts, route
from nutrition.onboarding_ui import resolve_city, stage_keyboard, reply_keyboard, CITIES
from nutrition.channels import send


class OnboardingUITests(unittest.TestCase):
    def test_buttons_finish_existing_onboarding_and_resolve_city(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'TELEGRAM_OWNER_ID':'1'}):
            accounts=Accounts(d+'/db'); account='telegram:2'
            route(accounts,account,'/start')
            route(accounts,account,'✅ Согласен')
            for value in ('175','85','35','👩 Женщина','🚶 Низкая','📉 Снижение','Нет исключений'):
                route(accounts,account,value)
            self.assertEqual(accounts.get(account)['stage'],'timezone')
            first_start=accounts.get(account)['first_start']
            self.assertIn('Не удалось',route(accounts,account,'Несуществующий город'))
            self.assertEqual(accounts.get(account)['stage'],'timezone')
            route(accounts,account,'Копейск')
            user=accounts.get(account)
            self.assertEqual(user['profile']['timezone'],'Asia/Yekaterinburg')
            self.assertEqual(user['profile']['sex'],'ж')
            self.assertEqual(user['stage'],'review')
            route(accounts,account,'✅ Принять ориентиры')
            self.assertEqual(accounts.get(account)['stage'],'ready')
            self.assertEqual(accounts.get(account)['first_start'],first_start)

    def test_city_lookup_is_explicit_and_iana_compatible(self):
        for zone in set(CITIES.values()):ZoneInfo(zone)
        self.assertEqual(resolve_city(' г. ЧЕЛЯБИНСК '),'Asia/Yekaterinburg')
        self.assertEqual(resolve_city('Москва'),'Europe/Moscow')
        self.assertEqual(resolve_city('Europe/London'),'Europe/London')
        with self.assertRaises(ValueError):resolve_city('Александровка')

    def test_keyboards_follow_account_not_other_users(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'CHANNEL_DB':d+'/db','TELEGRAM_OWNER_ID':'1'}):
            accounts=Accounts(d+'/db')
            for account, stage in [('telegram:2','sex'),('telegram:3','height')]:
                u=accounts.start(account);accounts.save(account,u['profile'],stage)
            self.assertEqual(reply_keyboard(2),stage_keyboard('sex'))
            self.assertTrue(reply_keyboard(3)['remove_keyboard'])
            self.assertEqual(len(reply_keyboard(1)['keyboard']),3)
            response=Mock(); response.read.return_value=b'{"ok":true}'
            response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
            with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'test'}), patch('nutrition.channels.urlopen',return_value=response) as request:
                send('telegram',2,'Выберите пол','event')
            body=json.loads(request.call_args.args[0].data)
            self.assertEqual(body['reply_markup'],stage_keyboard('sex'))
