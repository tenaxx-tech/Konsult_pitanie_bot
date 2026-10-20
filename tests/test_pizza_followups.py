import os
import tempfile
import unittest
from unittest.mock import patch
from nutrition.channels import answer
from nutrition.google_storage import LocalGoogleJournal
from nutrition.intake_checks import references,validate


class PizzaFollowups(unittest.TestCase):
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
