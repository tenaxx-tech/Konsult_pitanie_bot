import tempfile
from pathlib import Path
import unittest
from nutrition.core import Portion,Nutrients
from nutrition.diary_service import mutate
from nutrition.google_storage import LocalGoogleJournal
from nutrition.review_apply import apply,snapshot_hash


class ReviewTests(unittest.TestCase):
    def test_guarded_replacement_and_repeat(self):
        with tempfile.TemporaryDirectory() as root:
            j=LocalGoogleJournal(str(Path(root)/'db.sqlite3'),'telegram:fixture')
            day='2026-10-19'
            item=dict(MealItemKey=day+'|LUNCH|one',Meal='LUNCH',Status='CONFIRMED',Source='ESTIMATE',portion=Portion('Fixture',100,Nutrients(100,10,4,6),'Fixture',estimated=True).record())
            mutate(j,day,'add','add',0,items=[item])
            pack=dict(account=j.account,day=day,expected_version=1,operation='review',before_sha256=snapshot_hash(j.read(day)),items=[{**item,'portion':Portion('Corrected',150,Nutrients(100,10,4,6),'reviewed source',estimated=True).record()}])
            self.assertEqual(apply(j,pack)['version'],2)
            self.assertEqual(apply(j,pack)['result'],'ALREADY_APPLIED')
            self.assertEqual(len(j.read(day)['meals']),1)
            self.assertEqual(j.read(day)['meals'][0]['fields']['Grams'],150)
            self.assertEqual(apply(j,pack)['version'],2)
    def test_concurrent_day_or_other_account_refused(self):
        with tempfile.TemporaryDirectory() as root:
            j=LocalGoogleJournal(str(Path(root)/'db.sqlite3'),'telegram:fixture')
            pack=dict(account='telegram:other',day='2026-10-19',before_sha256='old')
            with self.assertRaises(ValueError):apply(j,pack)
            pack['account']=j.account;pack['operation']='not-used'
            with self.assertRaises(ValueError):apply(j,pack)
