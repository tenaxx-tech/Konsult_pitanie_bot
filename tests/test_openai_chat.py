import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from nutrition import openai_chat as chat

class Tests(unittest.TestCase):
    def test_error_never_prints_key(self):
        with patch.dict('os.environ',OPENAI_API_KEY='secret'),patch.object(chat,'urlopen',side_effect=HTTPError('https://example/secret',401,'secret',{},None)):
            with self.assertRaisesRegex(chat.SyncError,'invalid key') as exc:
                chat.request({'input':'test'})
            self.assertNotIn('secret',str(exc.exception))

    def test_context_and_day_isolation(self):
        calls=[]
        snapshot=dict(day='2026-10-03',state=None,meals=[],computed_eaten={},computed_forecast={},protocols={})
        with patch.object(chat,'reserve_request',return_value=None),tempfile.NamedTemporaryFile() as db,patch.object(chat,'request',side_effect=lambda body:calls.append(body) or 'Ответ'):
            chat.answer('Первый вопрос',snapshot,db.name)
            chat.answer('Второй вопрос',snapshot,db.name)
            self.assertIn('Первый вопрос',json.dumps(calls[-1],ensure_ascii=False))
            snapshot['day']='2026-10-04'
            chat.answer('Новый день',snapshot,db.name)
            self.assertNotIn('Первый вопрос',json.dumps(calls[-1],ensure_ascii=False))
            self.assertIn('НЕТ инструмента записи',calls[-1]['instructions'])

    def test_rolling_limit_and_restart(self):
        with tempfile.NamedTemporaryFile() as db, patch.dict('os.environ', OPENAI_DAILY_LIMIT='2'):
            with patch.object(chat.time, 'time', return_value=100000):
                self.assertIsNone(chat.reserve_request(db.name))
                self.assertIn('10 секунд', chat.reserve_request(db.name))
            with patch.object(chat.time, 'time', return_value=100010):
                self.assertIsNone(chat.reserve_request(db.name))
            with patch.object(chat.time, 'time', return_value=100020):
                self.assertIn('исчерпан', chat.reserve_request(db.name))
            with patch.object(chat.time, 'time', return_value=186400):
                self.assertIsNone(chat.reserve_request(db.name))

    def test_quota_error_is_safe(self):
        import io
        error = HTTPError('https://example/secret', 429, 'secret', {},
                          io.BytesIO(b'{"error":{"code":"insufficient_quota","message":"secret"}}'))
        with patch.dict('os.environ', OPENAI_API_KEY='secret'), patch.object(chat, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(chat.SyncError, 'billing quota exhausted') as exc:
                chat.request({'input': 'test'})
            self.assertNotIn('secret', str(exc.exception))
