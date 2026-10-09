import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nutrition.airtable import AirtableJournal
from nutrition.channels import answer, _explicit_consumption
from tests.test_airtable import Fake


ITEMS = [{'name':'Овсяная каша','grams':200,'kcal':230,'protein':8,
          'fat':5,'carbs':38,'estimated':False}]


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / 'channels.sqlite3')
        self.remote = AirtableJournal(Fake())
        self.remote_factory = lambda: self.remote

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_clear_past_tense_counts_as_consumption(self):
        self.assertTrue(_explicit_consumption('Я съел суп'))
        self.assertFalse(_explicit_consumption('Я не съел суп'))
        self.assertFalse(_explicit_consumption('Хочу съесть суп'))
        self.assertFalse(_explicit_consumption('Можно ли съесть суп?'))

    def test_consumed_meal_upserts_as_estimate(self):
        result = {'action':'record','reply':'Оценка','day':'2026-10-02',
                  'meal':'LUNCH','items':ITEMS}
        with patch.dict(os.environ, {'GIGACHAT_AUTHORIZATION_KEY':'configured'}), \
             patch('nutrition.gigachat_chat.extract_intake', return_value=result):
            reply = answer('Я съел кашу', self.remote_factory, self.database,
                           'telegram:1', 'telegram:42')
        rows = self.remote.read('2026-10-02')['meals']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['fields']['Status'], 'CONFIRMED')
        self.assertEqual(rows[0]['fields']['Source'], 'ESTIMATE')
        self.assertIn('оценка', reply)

    def test_partial_write_retry_keeps_original_model_interpretation(self):
        from nutrition.airtable import SyncError
        from unittest.mock import Mock
        data={'action':'record','reply':'Оценка','day':'2026-10-02',
              'meal':'LUNCH','items':ITEMS}
        original=self.remote.upsert
        attempts=[]
        def interrupted(*args,**kwargs):
            result=original(*args,**kwargs)
            attempts.append(1)
            if len(attempts)==1:
                raise SyncError('connection lost after save')
            return result
        with patch.dict(os.environ,{'GIGACHAT_AUTHORIZATION_KEY':'configured'}), \
             patch('nutrition.gigachat_chat.extract_intake',return_value=data) as extract, \
             patch.object(self.remote,'upsert',side_effect=interrupted):
            with self.assertRaises(SyncError):
                answer('Съел кашу',self.remote_factory,self.database,'telegram:retry','telegram:42')
            answer('Съел кашу',self.remote_factory,self.database,'telegram:retry','telegram:42')
        extract.assert_called_once()
        self.assertEqual(len(self.remote.read('2026-10-02')['meals']),1)

    def test_photo_proposal_is_only_written_after_confirmation_once(self):
        result = {'action':'proposal','reply':'Похоже на яблоко','day':'2026-10-02',
                  'meal':'SNACK','items':ITEMS}
        with patch.dict(os.environ, {'GIGACHAT_AUTHORIZATION_KEY':'configured'}), \
             patch('nutrition.gigachat_chat.extract_intake', return_value=result):
            proposal = answer('Что на фото?', self.remote_factory, self.database,
                              'telegram:photo1', 'telegram:42', ['file_12345678'])
        self.assertIn('Пока в дневник не записывал', proposal)
        self.assertEqual(self.remote.read('2026-10-02')['meals'], [])
        answer('Съел', self.remote_factory, self.database, 'telegram:confirm1', 'telegram:42')
        answer('Съел', self.remote_factory, self.database, 'telegram:confirm2', 'telegram:42')
        meals = self.remote.read('2026-10-02')['meals']
        self.assertEqual(len(meals), 1)
        self.assertEqual(meals[0]['fields']['Status'], 'CONFIRMED')

    def test_consumption_without_gigachat_never_falls_through_to_unlogged_ai(self):
        with patch.dict(os.environ, {'GIGACHAT_AUTHORIZATION_KEY':'', 'GEMINI_API_KEY':'configured'}), \
             patch('nutrition.gemini_chat.answer') as gemini:
            reply = answer('Я съел кашу', self.remote_factory, self.database)
        gemini.assert_not_called()
        self.assertIn('не записаны', reply)
        self.assertEqual(self.remote.read('2026-10-02')['meals'], [])
