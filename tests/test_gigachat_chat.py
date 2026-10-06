import io
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError

from nutrition import gigachat_chat as chat


def response(payload):
    result = MagicMock()
    result.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return result


class Tests(unittest.TestCase):
    def setUp(self):
        chat._clear_token()

    def tearDown(self):
        chat._clear_token()

    def test_request_exchanges_key_and_uses_chat_completion(self):
        with patch.dict('os.environ', GIGACHAT_AUTHORIZATION_KEY='private-key', GIGACHAT_MODEL='GigaChat-2'):
            with patch.object(chat, 'urlopen', side_effect=[
                response({'access_token': 'short-lived-token', 'expires_at': 4102444800}),
                response({'choices': [{'message': {'content': 'Привет'}}]}),
            ]) as call:
                self.assertEqual(chat.request({'instructions': 'policy', 'input': 'hello'}), 'Привет')
        oauth_req, api_req = [c.args[0] for c in call.call_args_list]
        self.assertEqual(oauth_req.full_url, chat.OAUTH_URL)
        self.assertEqual(oauth_req.get_header('Authorization'), 'Basic private-key')
        self.assertEqual(oauth_req.get_header('Rquid') is not None, True)
        self.assertEqual(api_req.full_url, chat.API_URL)
        self.assertEqual(api_req.get_header('Authorization'), 'Bearer short-lived-token')
        payload = json.loads(api_req.data)
        self.assertEqual(payload['model'], 'GigaChat-2')
        self.assertEqual(payload['messages'][0], {'role': 'system', 'content': 'policy'})
        self.assertEqual(payload['messages'][1], {'role': 'user', 'content': 'hello'})

    def test_extract_intake_uses_json_schema_and_photo_model(self):
        expected = {'action':'proposal','reply':'Похоже на обед','day':'2026-10-06',
                    'meal':'LUNCH','items':[]}
        with patch.dict('os.environ', GIGACHAT_AUTHORIZATION_KEY='private-key'):
            with patch.object(chat, 'request', return_value=json.dumps(expected)) as call:
                result = chat.extract_intake('Что на фото?', '2026-10-06', {}, ['file_12345678'])
        body = call.call_args.args[0]
        self.assertEqual(result, expected)
        self.assertEqual(body['model'], chat.DEFAULT_VISION_MODEL)
        self.assertEqual(body['response_format']['type'], 'json_schema')
        self.assertEqual(body['input'][1]['attachments'], ['file_12345678'])

    def test_access_token_is_cached(self):
        with patch.dict('os.environ', GIGACHAT_AUTHORIZATION_KEY='private-key'):
            with patch.object(chat, 'urlopen', side_effect=[
                response({'access_token': 'cached-token', 'expires_at': 4102444800}),
                response({'choices': [{'message': {'content': 'one'}}]}),
                response({'choices': [{'message': {'content': 'two'}}]}),
            ]) as call:
                self.assertEqual(chat.request({'input': 'one'}), 'one')
                self.assertEqual(chat.request({'input': 'two'}), 'two')
        self.assertEqual(call.call_count, 3)

    def test_missing_key_is_clear_and_does_not_make_network_call(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(chat, 'urlopen') as call:
            with self.assertRaisesRegex(chat.SyncError, 'GIGACHAT_AUTHORIZATION_KEY is missing'):
                chat.request({'input': 'test'})
        call.assert_not_called()

    def test_provider_error_never_exposes_credentials(self):
        error = HTTPError('https://example/private-key', 401, 'private-key', {}, io.BytesIO(b'{}'))
        with patch.dict('os.environ', GIGACHAT_AUTHORIZATION_KEY='private-key'):
            with patch.object(chat, 'urlopen', side_effect=error):
                with self.assertRaisesRegex(chat.SyncError, 'invalid authorization key') as caught:
                    chat._get_access_token()
        self.assertNotIn('private-key', str(caught.exception))

    def test_dialogue_keeps_context_and_separates_days(self):
        calls = []
        snapshot = dict(day='2026-10-06', state=None, meals=[], computed_eaten={},
                        computed_forecast={}, protocols={})
        with patch.object(chat, 'request', side_effect=lambda body: calls.append(body) or 'Ответ'), tempfile.NamedTemporaryFile() as db:
            chat.answer('Первый вопрос', snapshot, db.name)
            chat.answer('Второй вопрос', snapshot, db.name)
            self.assertIn('Первый вопрос', json.dumps(calls[-1], ensure_ascii=False))
            snapshot['day'] = '2026-10-07'
            chat.answer('Новый день', snapshot, db.name)
            self.assertNotIn('Первый вопрос', json.dumps(calls[-1], ensure_ascii=False))
            self.assertIn('Сам этот диалог не записывает данные', calls[-1]['instructions'])
