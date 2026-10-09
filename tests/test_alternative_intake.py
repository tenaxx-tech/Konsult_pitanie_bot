import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nutrition.airtable import AirtableJournal, SyncError
from nutrition.channels import answer
from nutrition.intake_providers import _validated
from tests.test_airtable import Fake

MEAL = {'action': 'record', 'reply': 'Оценка', 'day': '2026-10-02',
        'meal': 'LUNCH', 'items': [{'name': 'Каша', 'grams': 200, 'kcal': 230,
                                  'protein': 8, 'fat': 5, 'carbs': 38, 'estimated': True}]}


class AlternativeIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / 'channel.sqlite3')
        self.remote = AirtableJournal(Fake())

    def tearDown(self):
        self.tmp.cleanup()

    def test_gemini_records_confirmed_and_deduplicates_retry(self):
        with patch.dict(os.environ, {'GIGACHAT_AUTHORIZATION_KEY': '', 'GEMINI_API_KEY': 'configured'}), \
             patch('nutrition.gemini_chat.request', return_value=json.dumps(MEAL, ensure_ascii=False)) as request:
            for _ in range(2):
                response = answer('Съел кашу', lambda: self.remote, self.db,
                                  'telegram:alternate-1', 'telegram:42')
        self.assertIn('Проверено чтением Airtable', response)
        self.assertEqual(len(self.remote.read('2026-10-02')['meals']), 1)
        request.assert_called_once()

    def test_groq_text_proposal_requires_confirmation(self):
        proposal = {**MEAL, 'action': 'proposal'}
        with patch.dict(os.environ, {'GIGACHAT_AUTHORIZATION_KEY': '', 'GEMINI_API_KEY': '',
                                     'GROQ_API_KEY': 'configured'}), \
             patch('nutrition.groq_chat.request', return_value=json.dumps(proposal, ensure_ascii=False)):
            response = answer('Запланируй кашу 200 г', lambda: self.remote, self.db,
                              'telegram:proposal-1', 'telegram:42')
            self.assertIn('Пока не записывал', response)
            self.assertEqual(self.remote.read('2026-10-02')['meals'], [])
            answer('Съел', lambda: self.remote, self.db, 'telegram:confirm-1', 'telegram:42')
        self.assertEqual(len(self.remote.read('2026-10-02')['meals']), 1)

    def test_invalid_json_never_writes(self):
        with patch.dict(os.environ, {'GIGACHAT_AUTHORIZATION_KEY': '', 'GEMINI_API_KEY': 'configured'}), \
             patch('nutrition.gemini_chat.request', return_value='не JSON'):
            with self.assertRaises(SyncError):
                answer('Съел кашу', lambda: self.remote, self.db, 'telegram:bad', 'telegram:42')
        self.assertEqual(self.remote.read('2026-10-02')['meals'], [])

    def test_rejects_invalid_item_and_invalid_date(self):
        for value in ({**MEAL, 'day': '2026-99-99'},
                      {**MEAL, 'items': [{**MEAL['items'][0], 'grams': -1}]}):
            with self.assertRaises(SyncError):
                _validated(json.dumps(value))
