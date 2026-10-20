import os
import tempfile
import unittest
from unittest.mock import patch
from nutrition.channels import answer
from nutrition.google_storage import LocalGoogleJournal
from nutrition.intake_checks import references,validate


class PizzaFollowups(unittest.TestCase):
    def test_complete_real_message_records_without_model_and_retry_is_idempotent(self):
        text='Съела 67 г домашней пиццы: варёная колбаса, маринованные огурцы, сыр, майонез и кетчуп. Рассчитай приблизительно.'
        with tempfile.TemporaryDirectory() as d:
            db=d+'/db';journal=LocalGoogleJournal(db,'telegram:42')
            with patch.dict(os.environ,{'GIGACHAT_AUTHORIZATION_KEY':'test'}),patch('nutrition.channels.today',return_value='2026-10-20'),patch('nutrition.gigachat_chat.request',side_effect=AssertionError('Model should not be called')):
                # Reproduce the stale incomplete question left by the failed model.
                import sqlite3
                with sqlite3.connect(db) as conn:
                    conn.execute('CREATE TABLE intake_questions(account TEXT PRIMARY KEY,text TEXT,event TEXT)')
                    conn.execute('INSERT INTO intake_questions VALUES(?,?,?)',('telegram:42','вес 67 грамм\nсъела','old'))
                first=answer(text,lambda:journal,db,'t:pizza-full','telegram:42')
                second=answer(text,lambda:journal,db,'t:pizza-full','telegram:42')
            self.assertIn('Записал',first);self.assertIn('Записал',second)
            data=journal.read('2026-10-20');self.assertEqual(len(data['meals']),1)
            self.assertAlmostEqual(float(data['computed_eaten']['kcal']),184.25)

    def test_shortcut_does_not_discard_other_food_or_label_or_negation(self):
        from nutrition.intake_checks import known_pizza_event
        for text in ('Не съела 67 г пиццы','Съела 67 г пиццы и яблоко','Съела 67 г пиццы и 100 г огурцов','Съела 67 г пиццы по этикетке'):
            self.assertIsNone(known_pizza_event(text,'2026-10-20'))

    def test_weight_ingredients_and_confirmation_stay_one_event(self):
        with tempfile.TemporaryDirectory() as d:
            db=d+'/db';journal=LocalGoogleJournal(db,'telegram:42');seen=[]
            def extract(text,day,*args):
                seen.append(text)
                return dict(action='record' if 'съела' in text else 'proposal',day=day,meal='SNACK',reply='ok',
                            items=[dict(name='Пицца',grams=67,kcal=90,protein=20,fat=30,carbs=40,estimated=True,basis='ready')])
            with patch.dict(os.environ,{'GIGACHAT_AUTHORIZATION_KEY':'test'}),patch('nutrition.channels.today',return_value='2026-10-20'),patch('nutrition.gigachat_chat.extract_intake',side_effect=extract):
                call=lambda text,event:answer(text,lambda:journal,db,event,'telegram:42')
                self.assertIn('калорийность оценю сам',call('не учел пицу','t:1'))
                call('вес 67 грамм','t:2')
                call('колбаса вареная, маринованные огурцы, сыр, майонез, кетчуп','t:3')
                result=call('съела','t:4')
            self.assertIn('колбаса',seen[-1]);self.assertIn('67 грамм',seen[-1])
            self.assertIn('Записал',result)
            data=journal.read('2026-10-20');self.assertEqual(len(data['meals']),1)
            self.assertAlmostEqual(float(data['computed_eaten']['kcal']),184.25)

    def test_pizza_estimate_is_consistent_and_label_not_replaced(self):
        bad=dict(name='Пицца',grams=67,kcal=90,protein=20,fat=30,carbs=40)
        fixed=references([bad.copy()],'Пицца, вес 67 грамм, колбаса и сыр')
        self.assertIsNone(validate(fixed,'Пицца 67 г'))
        self.assertEqual(references([bad.copy()],'Пицца, по этикетке'),[bad])
