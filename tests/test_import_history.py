import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from nutrition.import_history import HEADER, SOURCE_ID, install, prepare


class ImportHistoryTests(unittest.TestCase):
    def rows(self):
        day='2026-10-01'
        state=dict(Date=day,DayKey=day,Status='CLOSED',Version=7,CaloriesGoal=1957,
                   ProteinMin=150,ProteinMax=160,FatMin=70,FatMax=80,CarbMax=170,
                   PointsToday='',PointsCumulative='75')
        for prefix in ('Eaten','Forecast'):
            state.update({prefix+k:v for k,v in dict(Kcal=100,Protein=10,Fat=4,Carbs=6).items()})
        food=dict(Date=day,MealItemKey='legacy-key',Status='CONFIRMED',Product='Fixture',
                  Grams='',Approx='',Kcal=100,Protein=10,Fat=4,Carbs=6)
        raw=json.dumps(dict(sourceId=SOURCE_ID,account='telegram:test',day=day,state=state,meals=[food]))
        return [HEADER,[day,hashlib.sha256(raw.encode()).hexdigest(),raw]]

    def test_invalid_manifest_and_current_day_refused(self):
        rows=self.rows()
        with self.assertRaises(ValueError):prepare(rows,'telegram:test','2026-10-01',1)
        with self.assertRaises(ValueError):prepare(rows,'telegram:other','2026-10-19',1)
        rows[1][1]='bad'
        with self.assertRaises(ValueError):prepare(rows,'telegram:test','2026-10-19',1)

    def test_preserves_unknowns_closed_points_and_legacy_key(self):
        p=prepare(self.rows(),'telegram:test','2026-10-19',1)
        s=json.loads(p['2026-10-01'][1])
        self.assertIsNone(s['meals'][0]['fields']['Grams'])
        self.assertIsNone(s['meals'][0]['fields']['Approx'])
        self.assertEqual(s['meals'][0]['id'],'legacy-key')
        self.assertEqual(s['state']['fields']['PointsToday'],'')
        self.assertEqual(s['state']['fields']['Status'],'CLOSED')

    def test_dry_run_backup_repeat_and_current_day_preserved(self):
        p=prepare(self.rows(),'telegram:test','2026-10-19',1)
        with tempfile.TemporaryDirectory() as root:
            path=root+'/database.sqlite3'
            from nutrition.google_storage import LocalGoogleJournal
            j=LocalGoogleJournal(path,'telegram:test')
            with j.connect() as db:
                db.execute('INSERT INTO google_diary(account,day,version,snapshot,updated) VALUES(?,?,?,?,?)',
                           ('telegram:test','2026-10-19',5,'{"fixture":"current"}','now'))
            self.assertEqual(install(path,'telegram:test',p,'2026-10-19')['would_add'],1)
            with j.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM google_diary').fetchone()[0],1)
            result=install(path,'telegram:test',p,'2026-10-19',True)
            self.assertTrue(Path(result['backup']).is_file())
            with sqlite3.connect(result['backup']) as db:self.assertEqual(db.execute('SELECT count(*) FROM google_diary').fetchone()[0],1)
            self.assertEqual(install(path,'telegram:test',p,'2026-10-19',True)['skipped'],1)
            with j.connect() as db:
                self.assertEqual(db.execute('SELECT snapshot FROM google_diary WHERE day=?',('2026-10-19',)).fetchone()[0],'{"fixture":"current"}')
            changed=dict(p)
            changed['2026-09-30']=(1,'{}')
            changed['2026-10-01']=(1,'{}')
            with self.assertRaises(ValueError):install(path,'telegram:test',changed,'2026-10-19',True)
            with j.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM google_diary').fetchone()[0],2)
