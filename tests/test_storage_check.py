import json
import tempfile
import unittest
from pathlib import Path
from nutrition.google_storage import HEADERS, LocalGoogleJournal
from nutrition.storage_check import ACCOUNT, DAY, run


class Transport:
    def __init__(self):
        self.rows = {}

    def get(self, cells):
        if cells == 'BotJournal!A1:E1':
            return [HEADERS]
        row = int(cells.split('!A')[1].split(':')[0])
        return [self.rows[row]] if row in self.rows else []

    def put_verified(self, row, values):
        self.rows[row] = [str(v) for v in values]


class StorageCheckTests(unittest.TestCase):
    def test_repeat_and_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            db = str(Path(root)/'diary.sqlite3')
            mirror = Transport()
            self.assertEqual(run(db, mirror)['result'], 'PASS')
            self.assertEqual(run(db, mirror)['version'], 1)
            self.assertEqual(len(mirror.rows), 1)
            owner = LocalGoogleJournal(db, 'telegram:owner')
            self.assertEqual(owner.read(DAY)['meals'], [])
            self.assertEqual(owner.read('2026-10-19')['meals'], [])
            self.assertEqual(json.loads(next(iter(mirror.rows.values()))[3])['meals'][0]['fields']['Status'], 'PLANNED')

    def test_failed_export_does_not_pass(self):
        class Failed(Transport):
            def put_verified(self, row, values):
                raise TimeoutError('simulated')
        with tempfile.TemporaryDirectory() as root:
            db = str(Path(root)/'diary.sqlite3')
            with self.assertRaises(RuntimeError):
                run(db, Failed())
            self.assertEqual(LocalGoogleJournal(db, ACCOUNT).read(DAY)['google_sync'], 'PENDING')
