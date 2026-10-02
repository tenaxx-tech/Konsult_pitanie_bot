import argparse
import json
from .core import Journal, Portion, Nutrients


def main():
    parser = argparse.ArgumentParser(description='Калькулятор КБЖУ и дневной учёт')
    parser.add_argument('--db', default='nutrition.sqlite3')
    parser.add_argument('--timezone', default='Asia/Omsk')
    parser.add_argument('--day', help='Дата YYYY-MM-DD')
    commands = parser.add_subparsers(dest='command', required=True)
    calc = commands.add_parser('calculate')
    calc.add_argument('file')
    plan = commands.add_parser('plan')
    plan.add_argument('meal_id')
    plan.add_argument('file')
    plan.add_argument('--replace', action='store_true')
    confirm = commands.add_parser('confirm')
    confirm.add_argument('meal_id')
    commands.add_parser('summary')
    commands.add_parser('close')
    args = parser.parse_args()
    journal = None
    try:
        if args.command in ('calculate', 'plan'):
            with open(args.file, encoding='utf-8') as file:
                portions = [Portion.from_record(p) for p in json.load(file)]
            if not portions:
                raise ValueError('At least one portion is required')
        if args.command == 'calculate':
            output = {'total': sum((p.total for p in portions), Nutrients()).values(True),
                      'estimated': any(p.estimated for p in portions)}
        else:
            journal = Journal(args.db, args.timezone)
            if args.command == 'plan':
                output = journal.plan(args.meal_id, portions, args.day, args.replace)
            elif args.command == 'confirm':
                output = journal.confirm(args.meal_id, args.day)
            elif args.command == 'close':
                output = journal.close_day(args.day)
            else:
                output = journal.summary(args.day)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    finally:
        if journal:
            journal.close()


if __name__ == '__main__':
    main()
