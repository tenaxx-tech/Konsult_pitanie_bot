import unittest
from unittest.mock import patch
from nutrition import gigachat_chat as chat


class VisionPipelineTests(unittest.TestCase):
    def test_vision_is_plain_text_without_nutrient_schema(self):
        with patch.object(chat,'request',return_value='Творог 0%, яйца') as call:
            self.assertIn('Творог',chat.describe_products(['file_12345678'],'Для плана'))
        body=call.call_args.args[0]
        self.assertNotIn('response_format',body)
        self.assertEqual(body['input'][0]['attachments'],['file_12345678'])

    def test_empty_or_unrecognised_vision_does_not_call_nutrient_extractor(self):
        for response in (chat.SyncError('GigaChat response incomplete'),'НЕ_РАСПОЗНАНО'):
            with patch.object(chat,'request',side_effect=response if isinstance(response,Exception) else None,return_value=response) as call:
                result=chat.extract_intake('Для плана','2026-10-20',{},['file_12345678'])
            self.assertEqual(result['action'],'clarify');self.assertEqual(result['items'],[])
            self.assertEqual(call.call_count,1)

    def test_image_then_text_extraction_retains_user_weight_and_day(self):
        import json
        expected={'action':'proposal','reply':'Оценка','day':'2026-10-20','meal':'LUNCH','items':[]}
        with patch.object(chat,'request',side_effect=['Наггетсы и картофель',json.dumps(expected)]) as call:
            result=chat.extract_intake('150 г картофеля, 50 г наггетсов','2026-10-20',{},['file_12345678'])
        self.assertEqual(result,expected)
        first,second=[c.args[0] for c in call.call_args_list]
        self.assertNotIn('response_format',first)
        self.assertIn('response_format',second)
        self.assertIn('150 г',second['input'][1]['content'])
        self.assertNotIn('attachments',second['input'][1])
