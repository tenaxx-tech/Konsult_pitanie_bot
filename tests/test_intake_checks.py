from copy import deepcopy
import unittest
from nutrition.intake_checks import validate,references


class IntakeChecksTests(unittest.TestCase):
    def test_three_eggs_cannot_be_ninety_grams(self):
        items=[dict(name='Яйцо варёное',grams=90,kcal=140,protein=11.3,fat=9.5,carbs=1)]
        self.assertIn('массу яиц',validate(items,'Съел 3 варёных яйца'))
        normalized=references(items,'Съел 3 варёных яйца среднего размера')
        self.assertEqual(normalized[0]['grams'],150)
        self.assertAlmostEqual(normalized[0]['kcal'],232.5)
    def test_cooked_weight_and_sour_cream_need_clarification(self):
        dumplings=[dict(name='Пельмени',grams=360,kcal=788.4,protein=35.64,fat=50.04,carbs=48.6)]
        self.assertIn('после варки',validate(dumplings,'Съел пельмени 360 г'))
        self.assertIsNone(validate(dumplings,'Съел пельмени 360 г после варки'))
        cream=[dict(name='Сметана',grams=25,kcal=40,protein=.675,fat=3.75,carbs=.9)]
        self.assertIn('жирность',validate(cream,'Съел ложку сметаны'))
    def test_reference_does_not_claim_to_be_user_label(self):
        item=dict(name='Сметана',grams=10,kcal=35,protein=.3,fat=3.5,carbs=2.7)
        n=references([item],'Сметана 15%, ложка с горкой')[0]
        self.assertEqual(n['grams'],25)
        self.assertEqual(n['fat'],3.75)
        self.assertIn('не этикетка',n['source_detail'])
    def test_explicit_label_and_measured_mass_not_overwritten(self):
        item=dict(name='Сметана',grams=30,kcal=48,protein=.81,fat=4.5,carbs=1.08)
        self.assertEqual(references([deepcopy(item)],'Сметана 15% 30 г, ложка с горкой')[0]['grams'],30)
        self.assertEqual(references([deepcopy(item)],'Сметана по этикетке')[0],item)
