from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from nutrition import Journal, Nutrients, Portion, Goals


def portion(grams=180):
    return Portion('Test', grams, Nutrients(80, 18, '0.5', 3), 'fixture')


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/'test.sqlite3')
        self.j = Journal(self.path)
        self.day = '2026-10-02'

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def test_exact_calculation(self):
        self.assertEqual(portion().total.protein, Decimal('32.4'))
        p = Nutrients('0.04')
        self.assertEqual((p+p+p).values(True)['kcal'], '0.1')

    def test_invalid_values(self):
        for v in ('NaN', 'Infinity', '-1', 'abc'):
            with self.assertRaises(ValueError):
                Nutrients(v)
        with self.assertRaises(ValueError):
            portion(0)
        with self.assertRaises(ValueError):
            Goals(protein_min=170, protein_max=160)

    def test_excess(self):
        self.assertEqual(Goals().remaining(Nutrients(2000))['kcal'], '-43')

    def test_confirmation_retry(self):
        plan = self.j.plan('meal', [portion()], self.day)
        self.assertEqual(plan['confirmed']['kcal'], '0.0')
        self.assertEqual(plan['forecast']['kcal'], '144.0')
        confirmed = self.j.confirm('meal', self.day)
        self.assertEqual(confirmed, self.j.confirm('meal', self.day))
        self.assertEqual(confirmed['version'], 2)
        self.assertEqual(confirmed['planned']['kcal'], '0.0')

    def test_replace(self):
        self.j.plan('meal', [portion()], self.day)
        result = self.j.plan('meal', [portion(100)], self.day, replace=True)
        self.assertEqual(len(result['meals']), 1)
        self.assertEqual(result['planned']['kcal'], '80.0')
        self.j.confirm('meal', self.day)
        with self.assertRaises(ValueError):
            self.j.plan('meal', [portion()], self.day, replace=True)

    def test_new_day_and_closed(self):
        self.j.plan('meal', [portion()], self.day)
        self.j.confirm('meal', self.day)
        closed = self.j.close_day(self.day)
        self.assertEqual(closed, self.j.close_day(self.day))
        with self.assertRaises(ValueError):
            self.j.plan('other', [portion()], self.day)
        self.assertEqual(self.j.summary('2026-10-03')['confirmed']['kcal'], '0.0')

    def test_persistence(self):
        expected = self.j.plan('meal', [portion()], self.day)
        self.j.close()
        self.j = Journal(self.path)
        self.assertEqual(expected, self.j.summary(self.day))

    def test_rollback(self):
        self.j.plan('meal', [portion()], self.day)
        with self.assertRaises(ValueError):
            self.j.confirm('missing', self.day)
        self.assertEqual(self.j.summary(self.day)['version'], 1)


if __name__ == '__main__':
    unittest.main()
