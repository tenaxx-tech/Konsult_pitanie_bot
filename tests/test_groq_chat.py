import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from nutrition import groq_chat as chat


class Tests(unittest.TestCase):
    def test_request_uses_groq_and_does_not_leak_key(self):
        response = io.BytesIO(b'{"choices":[{"message":{"content":"OK"}}]}')
        with patch.dict('os.environ', GROQ_API_KEY='secret', GROQ_MODEL='openai/gpt-oss-120b'), \
             patch.object(chat, 'urlopen', return_value=response) as call:
            result = chat.request({
                'instructions': 'system rules',
                'input': [{'role': 'user', 'content': 'question'}],
                'max_output_tokens': 12,
            })
        self.assertEqual(result, 'OK')
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer secret')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], 'openai/gpt-oss-120b')
        self.assertEqual(payload['max_completion_tokens'], 12)
        self.assertEqual(payload['messages'], [
            {'role': 'system', 'content': 'system rules'},
            {'role': 'user', 'content': 'question'},
        ])

    def test_error_never_prints_key(self):
        error = HTTPError('https://example/secret', 401, 'secret', {}, None)
        with patch.dict('os.environ', GROQ_API_KEY='secret'), patch.object(chat, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(chat.SyncError, 'invalid key') as exc:
                chat.request({'input': 'test'})
            self.assertNotIn('secret', str(exc.exception))

    def test_context_and_day_isolation(self):
        calls = []
        snapshot = dict(day='2026-10-03', state=None, meals=[], computed_eaten={}, computed_forecast={}, protocols={})
        with tempfile.NamedTemporaryFile() as db, \
             patch.object(chat, 'request', side_effect=lambda body: calls.append(body) or 'Ответ'):
            chat.answer('Первый вопрос', snapshot, db.name)
            chat.answer('Второй вопрос', snapshot, db.name)
            self.assertIn('Первый вопрос', json.dumps(calls[-1], ensure_ascii=False))
            snapshot['day'] = '2026-10-04'
            chat.answer('Новый день', snapshot, db.name)
            self.assertNotIn('Первый вопрос', json.dumps(calls[-1], ensure_ascii=False))
            self.assertIn('НЕТ инструмента записи', calls[-1]['instructions'])

    def test_free_tier_limit_message_is_safe(self):
        error = HTTPError('https://example/secret', 429, 'secret', {}, None)
        with patch.dict('os.environ', GROQ_API_KEY='secret'), patch.object(chat, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(chat.SyncError, 'free-tier rate limit exceeded') as exc:
                chat.request({'input': 'test'})
            self.assertNotIn('secret', str(exc.exception))
