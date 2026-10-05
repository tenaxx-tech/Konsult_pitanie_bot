import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from nutrition import gemini_chat as chat


class GeminiTests(unittest.TestCase):
    def test_request_uses_gemini_rest_and_maps_assistant_role(self):
        response = io.BytesIO(
            b'{"candidates":[{"content":{"parts":[{"text":"Hello"},{"text":"!"}]}}]}'
        )
        with patch.dict('os.environ', GEMINI_API_KEY='secret-key', GEMINI_MODEL='gemini-3.1-flash-lite'), \
             patch.object(chat, 'urlopen', return_value=response) as call:
            result = chat.request({
                'instructions': 'system rules',
                'input': [
                    {'role': 'user', 'content': 'question'},
                    {'role': 'assistant', 'content': 'previous answer'},
                ],
                'max_output_tokens': 24,
            })
        self.assertEqual(result, 'Hello!')
        request = call.call_args.args[0]
        self.assertEqual(
            request.full_url,
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent',
        )
        self.assertEqual(request.get_header('X-goog-api-key'), 'secret-key')
        self.assertNotIn('secret-key', request.full_url)
        payload = json.loads(request.data)
        self.assertEqual(payload['system_instruction']['parts'][0]['text'], 'system rules')
        self.assertEqual(payload['generationConfig']['maxOutputTokens'], 24)
        self.assertEqual(payload['contents'], [
            {'role': 'user', 'parts': [{'text': 'question'}]},
            {'role': 'model', 'parts': [{'text': 'previous answer'}]},
        ])

    def test_missing_key_fails_without_network_call(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(chat, 'urlopen') as call:
            with self.assertRaisesRegex(chat.SyncError, 'GEMINI_API_KEY is missing'):
                chat.request({'input': 'test'})
        call.assert_not_called()

    def test_http_errors_do_not_leak_key(self):
        error = HTTPError('https://example.invalid', 429, 'secret-key', {}, None)
        with patch.dict('os.environ', GEMINI_API_KEY='secret-key'), \
             patch.object(chat, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(chat.SyncError, 'free-tier rate limit exceeded') as exc:
                chat.request({'input': 'test'})
        self.assertNotIn('secret-key', str(exc.exception))


if __name__ == '__main__':
    unittest.main()
