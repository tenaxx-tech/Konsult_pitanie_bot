"""Upgrade legacy owner history and isolate subsequent conversations by account."""
import os
import sqlite3


def prepare(db):
    db.execute('CREATE TABLE IF NOT EXISTS conversation(id INTEGER PRIMARY KEY,day TEXT,role TEXT,content TEXT,account TEXT NOT NULL DEFAULT \'\')')
    columns={r[1] for r in db.execute('PRAGMA table_info(conversation)')}
    if 'account' not in columns:
        db.execute("ALTER TABLE conversation ADD COLUMN account TEXT NOT NULL DEFAULT ''")
    owner='telegram:'+os.environ.get('TELEGRAM_OWNER_ID','')
    if os.environ.get('TELEGRAM_OWNER_ID'):
        db.execute("UPDATE conversation SET account=? WHERE account=''",(owner,))


def load(database, day, account):
    with sqlite3.connect(database,timeout=10) as db:
        prepare(db)
        return db.execute('SELECT role,content FROM conversation WHERE account=? AND day=? ORDER BY id DESC LIMIT 12',
                          (account,day)).fetchall()[::-1]


def save(database, day, account, text, result):
    with sqlite3.connect(database,timeout=10) as db:
        prepare(db)
        db.executemany('INSERT INTO conversation(day,role,content,account) VALUES(?,?,?,?)',
                       [(day,'user',text[:12000],account),(day,'assistant',result,account)])
