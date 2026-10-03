import copy
import unittest
from nutrition.airtable import AirtableJournal, SyncError, totals
from nutrition import Portion, Nutrients


FIELDS = {
 'Config': ['Key','Value','Active'],
 'Meals': ['MealItemKey','Date','Meal','Status','Product','Grams','Kcal','Protein','Fat','Carbs','Approx','Source','Notes','LastUpdate'],
 'DailyState': ['DayKey','Date','Status','Version','EatenKcal','EatenProtein','EatenFat','EatenCarbs',
                'ForecastKcal','ForecastProtein','ForecastFat','ForecastCarbs','LastUpdate',
                'CaloriesGoal','ProteinMin','ProteinMax','FatMin','FatMax','CarbMax']}


class Fake:
    def __init__(self):
        self.rows = {k: [] for k in FIELDS}
        for key in ('NutritionSyncProtocol','NutritionCalculationProtocol'):
            self.rows['Config'].append({'id': key, 'fields': {'Key':key,'Value':'test','Active':True}})
        self.fail_after_write = False

    def schema(self):
        return [{'name':k,'id':k,'fields':[{'id':f,'name':f} for f in v]} for k,v in FIELDS.items()]

    def records(self, table, formula=None):
        rows = self.rows[table]
        if formula:
            if formula.startswith('IS_SAME('):
                field, value = 'Date', formula.split("'")[1]
            else:
                field, value = formula.split('=')
                field, value = field.strip('{}'), value.strip("'")
            rows = [r for r in rows if r['fields'].get(field) == value]
        return copy.deepcopy(rows)

    def write(self, table, fields, record_id=None):
        if record_id:
            row = next(r for r in self.rows[table] if r['id']==record_id)
            row['fields'].update(fields)
        else:
            self.rows[table].append({'id':'rec'+str(len(self.rows[table])), 'fields':dict(fields)})
        if self.fail_after_write:
            self.fail_after_write = False
            raise SyncError('simulated lost response')


class Tests(unittest.TestCase):
    def setUp(self):
        self.client = Fake()
        self.remote = AirtableJournal(self.client)
        self.day = '2026-10-02'
        self.item = dict(MealItemKey=self.day+'|BREAKFAST|001', Meal='BREAKFAST', Status='PLANNED',
                         Source='LABEL', portion=Portion('test',180,Nutrients(80,18,.5,3),'fixture').record())

    def test_create_confirm_and_retry(self):
        self.remote.upsert(self.day,self.item,0)
        self.item['Status']='CONFIRMED'
        result=self.remote.upsert(self.day,self.item,1)
        self.assertEqual(result['computed_eaten']['kcal'],'144.0')
        self.assertEqual(result['state']['fields']['Version'],2)
        retry=self.remote.upsert(self.day,self.item,2)
        self.assertEqual(retry['state']['fields']['Version'],2)
        self.assertEqual(len(self.client.rows['Meals']),1)

    def test_conflict(self):
        with self.assertRaises(SyncError):
            self.remote.upsert(self.day,self.item,42)
        self.assertEqual(self.client.rows['Meals'],[])

    def test_closed(self):
        self.remote.upsert(self.day,self.item,0)
        self.client.rows['DailyState'][0]['fields']['Status']='CLOSED'
        with self.assertRaises(SyncError):
            self.remote.upsert(self.day,self.item,1)

    def test_confirmed_protection(self):
        self.item['Status']='CONFIRMED'
        self.remote.upsert(self.day,self.item,0)
        self.item['portion']['grams']='100'
        with self.assertRaises(SyncError):
            self.remote.upsert(self.day,self.item,1)
        self.remote.upsert(self.day,self.item,1,correction=True)

    def test_unknown_numeric_and_other_day(self):
        with self.assertRaises(SyncError):
            totals([{'fields':dict(Date=self.day,MealItemKey='x',Status='CONFIRMED')}],self.day)
        with self.assertRaises(SyncError):
            totals([{'fields':{'Date':'2026-10-01'}}],self.day)

    def test_lost_response_no_duplicate(self):
        self.client.fail_after_write=True
        self.remote.upsert(self.day,self.item,0)
        self.assertEqual(len(self.client.rows['Meals']),1)

    def test_aggregate_recovery(self):
        self.remote.upsert(self.day,self.item,0)
        self.client.rows['DailyState'][0]['fields']['ForecastKcal']=0
        result=self.remote.upsert(self.day,self.item,1)
        self.assertEqual(result['state']['fields']['ForecastKcal'],144)

    def test_missing_protocol(self):
        self.client.rows['Config']=[]
        with self.assertRaises(SyncError):
            self.remote.read(self.day)

class VerificationTests(unittest.TestCase):
    def test_unchecked_checkbox_omitted_by_airtable(self):
        from nutrition.airtable import same_value
        self.assertTrue(same_value('Approx',None,False))
        self.assertFalse(same_value('Approx',None,True))

    def test_timestamp_timezone_normalization(self):
        from nutrition.airtable import same_value
        self.assertTrue(same_value('LastUpdate','2026-10-02T18:00:00.123Z',
                                  '2026-10-02T18:00:00.123+00:00'))
