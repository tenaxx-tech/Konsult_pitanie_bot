from dataclasses import dataclass
from datetime import datetime, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import sqlite3
from zoneinfo import ZoneInfo

KEYS = ('kcal', 'protein', 'fat', 'carbs')


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('Expected a finite nonnegative number') from None
    if not result.is_finite() or result < 0:
        raise ValueError('Expected a finite nonnegative number')
    return result


@dataclass(frozen=True)
class Nutrients:
    kcal: Decimal = Decimal(0)
    protein: Decimal = Decimal(0)
    fat: Decimal = Decimal(0)
    carbs: Decimal = Decimal(0)

    def __post_init__(self):
        for key in KEYS:
            object.__setattr__(self, key, number(getattr(self, key)))

    def __add__(self, other):
        return Nutrients(**{key: getattr(self, key) + getattr(other, key) for key in KEYS})

    def scaled(self, grams):
        factor = number(grams) / Decimal(100)
        return Nutrients(**{key: getattr(self, key) * factor for key in KEYS})

    def values(self, rounded=False):
        return {key: str(getattr(self, key).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)
                         if rounded else getattr(self, key)) for key in KEYS}


@dataclass(frozen=True)
class Portion:
    name: str
    grams: Decimal
    per100: Nutrients
    source: str
    basis: str = 'ready'
    estimated: bool = False

    def __post_init__(self):
        if not self.name.strip() or not self.source.strip():
            raise ValueError('Product name and nutrition source are required')
        object.__setattr__(self, 'grams', number(self.grams))
        if self.grams == 0 or self.basis not in ('raw', 'ready'):
            raise ValueError('Positive mass and raw/ready basis are required')
        if not isinstance(self.estimated, bool):
            raise ValueError('estimated must be a boolean')

    @property
    def total(self):
        return self.per100.scaled(self.grams)

    def record(self):
        return dict(name=self.name, grams=str(self.grams), per100=self.per100.values(),
                    source=self.source, basis=self.basis, estimated=self.estimated)

    @classmethod
    def from_record(cls, record):
        return cls(**{**record, 'per100': Nutrients(**record['per100'])})


@dataclass(frozen=True)
class Goals:
    kcal: Decimal = Decimal(1957)
    protein_min: Decimal = Decimal(150)
    protein_max: Decimal = Decimal(160)
    fat_min: Decimal = Decimal(70)
    fat_max: Decimal = Decimal(80)
    carbs: Decimal = Decimal(170)

    def __post_init__(self):
        for key in self.__dataclass_fields__:
            object.__setattr__(self, key, number(getattr(self, key)))
        if self.protein_min > self.protein_max or self.fat_min > self.fat_max:
            raise ValueError('Minimum cannot exceed maximum')

    def remaining(self, total):
        # Signed values intentionally show excess, rather than hiding it at zero.
        return {key: str(getattr(self, key) - getattr(total, key)) for key in ('kcal', 'carbs')} | {
            'protein': [str(self.protein_min-total.protein), str(self.protein_max-total.protein)],
            'fat': [str(self.fat_min-total.fat), str(self.fat_max-total.fat)]}


class Journal:
    """SQLite transactions serialize writes. Meal IDs make confirmation idempotent."""
    def __init__(self, path='nutrition.sqlite3', timezone='Asia/Omsk', goals=None):
        self.zone = ZoneInfo(timezone)
        self.goals = goals or Goals()
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS days (
          day TEXT PRIMARY KEY, closed INTEGER NOT NULL DEFAULT 0,
          version INTEGER NOT NULL DEFAULT 0, updated TEXT);
        CREATE TABLE IF NOT EXISTS meals (
          day TEXT NOT NULL, id TEXT NOT NULL, status TEXT NOT NULL,
          portions TEXT NOT NULL, PRIMARY KEY(day,id));
        ''')

    def close(self):
        self.db.close()

    def day(self, value=None):
        if value is None:
            return datetime.now(self.zone).date().isoformat()
        return date.fromisoformat(value).isoformat()

    def _write(self, day, operation):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('INSERT OR IGNORE INTO days(day) VALUES (?)', (day,))
            state = self.db.execute('SELECT * FROM days WHERE day=?', (day,)).fetchone()
            if state['closed']:
                raise ValueError('Day is closed; corrections require a future explicit correction API')
            changed = operation()
            if changed:
                self.db.execute('UPDATE days SET version=version+1, updated=? WHERE day=?',
                                (datetime.now(self.zone).isoformat(), day))
            self.db.execute('COMMIT')
            return self.summary(day)
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def plan(self, meal_id, portions, day=None, replace=False):
        day = self.day(day)
        if not meal_id.strip() or not portions:
            raise ValueError('Meal ID and portions are required')
        payload = json.dumps([p.record() for p in portions], ensure_ascii=False)
        def operation():
            old = self.db.execute('SELECT * FROM meals WHERE day=? AND id=?', (day, meal_id)).fetchone()
            if old:
                if old['status'] == 'CONFIRMED' or not replace:
                    raise ValueError('Meal exists; only a planned meal can be explicitly replaced')
                if old['portions'] == payload:
                    return False
                self.db.execute('UPDATE meals SET portions=? WHERE day=? AND id=?', (payload, day, meal_id))
            else:
                if replace:
                    raise ValueError('Cannot replace a missing meal')
                self.db.execute('INSERT INTO meals VALUES (?,?,?,?)', (day, meal_id, 'PLANNED', payload))
            return True
        return self._write(day, operation)

    def confirm(self, meal_id, day=None):
        day = self.day(day)
        def operation():
            old = self.db.execute('SELECT status FROM meals WHERE day=? AND id=?', (day, meal_id)).fetchone()
            if old is None:
                raise ValueError('Meal not found')
            if old['status'] == 'CONFIRMED':
                return False
            self.db.execute("UPDATE meals SET status='CONFIRMED' WHERE day=? AND id=?", (day, meal_id))
            return True
        return self._write(day, operation)

    def close_day(self, day=None):
        day = self.day(day)
        def operation():
            self.db.execute('UPDATE days SET closed=1 WHERE day=?', (day,))
            return True
        # Repeated closure is also idempotent.
        if self.summary(day)['closed']:
            return self.summary(day)
        return self._write(day, operation)

    def summary(self, day=None):
        day = self.day(day)
        state = self.db.execute('SELECT * FROM days WHERE day=?', (day,)).fetchone()
        totals = {'CONFIRMED': Nutrients(), 'PLANNED': Nutrients()}
        meals = []
        for row in self.db.execute('SELECT * FROM meals WHERE day=? ORDER BY id', (day,)):
            portions = [Portion.from_record(p) for p in json.loads(row['portions'])]
            meal_total = sum((p.total for p in portions), Nutrients())
            totals[row['status']] += meal_total
            meals.append(dict(id=row['id'], status=row['status'], portions=[p.record() for p in portions],
                              total=meal_total.values(rounded=True)))
        return dict(day=day, closed=bool(state['closed']) if state else False,
                    version=state['version'] if state else 0, last_update=state['updated'] if state else None,
                    confirmed=totals['CONFIRMED'].values(rounded=True),
                    planned=totals['PLANNED'].values(rounded=True),
                    forecast=(totals['CONFIRMED']+totals['PLANNED']).values(rounded=True),
                    remaining=self.goals.remaining(totals['CONFIRMED']), meals=meals)
