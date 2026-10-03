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
        with tempfile.NamedTemporaryFile() as db,patch.object(chat,'request',side_effect=lambda body:calls.append(body) or 'Ответ'):
            chat.answer('Первый вопрос',snapshot,db.name)
            chat.answer('Второй вопрос',snapshot,db.name)
            self.assertIn('Первый вопрос',json.dumps(calls[-1],ensure_ascii=False))
            snapshot['day']='2026-10-04'
            chat.answer('Новый день',snapshot,db.name)
            self.assertNotIn('Первый вопрос',json.dumps(calls[-1],ensure_ascii=False))
            self.assertIn('НЕТ инструмента записи',calls[-1]['instructions'])
