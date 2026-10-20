import unittest
from datetime import datetime
from unittest.mock import Mock,patch
import json
from nutrition.daily_plan import validate,render,answer


def snapshot():
    return {'day':'2026-10-20','profile':{'timezone':'Asia/Yekaterinburg','pantry_day':'2026-10-20',
            'pantry':'По фото: Пицца','pantry_quantities':{'пиц':{'grams':67}}},
            'state':{'fields':{'CaloriesGoal':1810,'ProteinMin':87,'ProteinMax':97,'FatMin':60,'FatMax':70,'CarbMax':230}},
            'computed_eaten':{'kcal':343,'protein':15,'fat':17.9,'carbs':33.7},'meals':[]}


def item(**changes):
    return dict({'name':'Пицца','meal':'LUNCH','grams':67,'basis':'ready',
                 'per100':{'kcal':275,'protein':12,'fat':11,'carbs':32}},**changes)


class DailyPlanTests(unittest.TestCase):
    def test_program_computes_rows_and_forecast_and_partial_menu(self):
        text=render(validate({'items':[item()]},snapshot(),12),snapshot())
        self.assertIn('184.2 ккал',text)
        self.assertIn('527.2 ккал',text)
        self.assertIn('1282.8 ккал',text)
        self.assertIn('часть рациона',text)
        self.assertIn('съеденным ничего не отмечено',text)

    def test_no_breakfast_after_noon_and_no_lunch_after_evening(self):
        for hour,meal in ((12,'BREAKFAST'),(18,'LUNCH')):
            with self.assertRaises(ValueError):validate({'items':[item(meal=meal)]},snapshot(),hour)

    def test_unknown_food_and_excess_pantry_mass_rejected(self):
        for entry in (item(name='Миндаль'),item(grams=68)):
            with self.assertRaises(ValueError):validate({'items':[entry]},snapshot(),12)
        with self.assertRaises(ValueError):validate({'items':[item(grams=40),item(grams=40)]},snapshot(),12)

    def test_wrong_nuts_reference_cannot_pass_with_consistent_macros(self):
        data=snapshot();data['profile']['pantry']='Миндаль'
        bad=item(name='Миндаль',grams=50,per100={'kcal':180,'protein':10,'fat':10,'carbs':12.5})
        with self.assertRaises(ValueError):validate({'items':[bad]},data,12)

    def test_bad_prose_has_bounded_retry_and_never_reaches_user(self):
        request=Mock(return_value='Завтрак 300 ккал, 50 г орехов 90 ккал. Итого 1460 ккал.')
        with patch('nutrition.daily_plan.local_time',return_value=datetime(2026,10,20,12)):
            text=answer('План питания',snapshot(),request,'policy')
        self.assertEqual(request.call_count,2)
        self.assertNotIn('1460',text);self.assertNotIn('орех',text)
        self.assertIn('1467.0',text)

    def test_valid_json_once_and_no_model_totals_used(self):
        request=Mock(return_value=json.dumps({'items':[item()],'total_kcal':1460}))
        with patch('nutrition.daily_plan.local_time',return_value=datetime(2026,10,20,12)):
            text=answer('План питания',snapshot(),request,'policy')
        self.assertEqual(request.call_count,1);self.assertNotIn('1460',text)
        self.assertIn('184.2',text)

    def test_daily_route_is_used_by_real_chat_entrypoint(self):
        from nutrition.gigachat_chat import answer as chat
        with patch('nutrition.gigachat_chat.request',return_value=json.dumps({'items':[item()]})) as request, \
             patch('nutrition.daily_plan.local_time',return_value=datetime(2026,10,20,12)), \
             patch('nutrition.conversations.save'):
            text=chat('Составь план питания на остаток текущего дня',snapshot(),'/unused')
        self.assertEqual(request.call_count,1);self.assertIn('527.2',text)
