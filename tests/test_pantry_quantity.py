import json
import tempfile
import unittest
from unittest.mock import Mock,patch
from nutrition.accounts import Accounts
from nutrition.planning_setup import handle
from nutrition import gigachat_chat as chat


class PantryQuantityTests(unittest.TestCase):
    def test_pizza_weight_updates_pantry_without_diary_or_model(self):
        with tempfile.TemporaryDirectory() as d:
            users=Accounts(d+'/db');u=users.start('telegram:2');p=u['profile'];p.update(pantry='По фото (проверьте названия): Пицца',pantry_day='2026-10-20')
            users.save('telegram:2',p,'ready');remote=Mock()
            result=handle(users,'telegram:2','вес 67 грамм пиццы','2026-10-20',lambda:remote)
            self.assertIn('67 г',result);self.assertIn('съеденным не отмечал',result)
            self.assertEqual(users.get('telegram:2')['profile']['pantry_quantities']['пиц']['grams'],67)
            remote.assert_not_called()
            self.assertIsNone(handle(users,'telegram:2','Съела пиццу 67 г','2026-10-20',lambda:remote))

    def test_empty_schema_response_has_one_validated_text_fallback(self):
        data={'action':'proposal','reply':'Оценка','day':'2026-10-20','meal':'LUNCH','items':[]}
        with patch.object(chat,'request',side_effect=[chat.SyncError('GigaChat response incomplete'),json.dumps(data)]) as call:
            self.assertEqual(chat.extract_intake('67 г пиццы','2026-10-20'),data)
        self.assertEqual(call.call_count,2)
        self.assertNotIn('response_format',call.call_args_list[1].args[0])

    def test_two_empty_responses_return_clarification_not_success(self):
        with patch.object(chat,'request',side_effect=chat.SyncError('GigaChat response incomplete')) as call:
            result=chat.extract_intake('67 г пиццы','2026-10-20')
        self.assertEqual(call.call_count,2);self.assertEqual(result['action'],'clarify');self.assertEqual(result['items'],[])
