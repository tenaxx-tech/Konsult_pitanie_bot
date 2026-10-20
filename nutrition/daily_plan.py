"""Daily suggestions: model chooses portions, code validates and renders all totals."""
import json
import re
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .airtable import SyncError
from .planning import PLAN_REQUEST

KEYS=('kcal','protein','fat','carbs')


def applies(text):
    return bool(PLAN_REQUEST.search(text or '') and not re.search(r'недел|7\s*дн|семь\s*дн',text,re.I))


def local_time(snapshot):
    return datetime.now(ZoneInfo((snapshot.get('profile') or {}).get('timezone','Asia/Yekaterinburg')))


def _decimal(value):
    if isinstance(value,bool):raise ValueError('Некорректное число')
    n=Decimal(str(value))
    if not n.is_finite() or n<0:raise ValueError('Некорректное число')
    return n


def _stems(text):
    ignored={'продукты','продуктов','плана','меню','фото','проверьте','названия','сегодня','есть','дома','грамм','готовый','готовые','готовая','имеются','меня','пищи','съела','съел','порция','упаковка'}
    return {w[:4] for w in re.findall(r'[а-яёa-z]{3,}',text.casefold()) if w not in ignored}


def validate(data,snapshot,hour):
    if not isinstance(data,dict) or not isinstance(data.get('items'),list) or len(data['items'])>8:
        raise ValueError('Нужен список порций')
    profile=snapshot.get('profile') or {}
    pantry=profile.get('pantry','')
    available=_stems(pantry)
    quantities=profile.get('pantry_quantities') or {}
    used={};rows=[]
    allowed={'LUNCH','SNACK','DINNER'} if hour>=11 else {'BREAKFAST','LUNCH','SNACK','DINNER'}
    if hour>=17:allowed={'SNACK','DINNER'}
    for item in data['items']:
        if not isinstance(item,dict):raise ValueError('Некорректная порция')
        name=item.get('name')
        if not isinstance(name,str) or not 2<=len(name)<=100:raise ValueError('Нет названия продукта')
        if not (_stems(name)&available):raise ValueError('В меню появился продукт вне запасов')
        meal=item.get('meal')
        if meal not in allowed:raise ValueError('Приём пищи уже прошёл')
        grams=_decimal(item.get('grams'))
        if not 0<grams<=2000:raise ValueError('Некорректная масса')
        if item.get('basis') not in ('raw','ready'):raise ValueError('Не указан сырой/готовый вес')
        source=item.get('per100')
        if not isinstance(source,dict):raise ValueError('Нет КБЖУ на 100 г')
        values=tuple(_decimal(source.get(k)) for k in KEYS)
        kcal,p,f,c=values
        if kcal>950 or p+f+c>105 or abs(kcal-(p*4+f*9+c*4))>max(Decimal(20),kcal*Decimal('.15')):
            raise ValueError('КБЖУ на 100 г не согласуются')
        # Common large errors must not pass merely because arithmetic is consistent.
        lower=name.casefold()
        if re.search(r'орех|миндал|кешью',lower) and not 450<=kcal<=800:
            raise ValueError('Неверная калорийность орехов')
        if 'пицц' in lower and not 150<=kcal<=450:raise ValueError('Неверная оценка пиццы')
        for key,q in quantities.items():
            if key in lower:
                used[key]=used.get(key,Decimal(0))+grams
                if used[key]>_decimal(q['grams']):raise ValueError('Порция превышает имеющуюся массу')
        rows.append(dict(name=name,meal=meal,grams=grams,basis=item['basis'],
                         totals=tuple(v*grams/100 for v in values)))
    return rows


def render(rows,snapshot):
    eaten=snapshot.get('computed_eaten') or {}
    fields=(snapshot.get('state') or {}).get('fields',{})
    if any(eaten.get(k) is None for k in KEYS) or fields.get('CaloriesGoal') is None:
        return 'Для плана не хватает подтверждённых итогов или цели. Посмотри /day — данные не менял.'
    fact=tuple(_decimal(eaten[k]) for k in KEYS)
    goal=_decimal(fields['CaloriesGoal']);remaining=goal-fact[0]
    total=tuple(sum((r['totals'][i] for r in rows),Decimal(0)) for i in range(4))
    if rows and total[0]>max(Decimal(0),remaining)+Decimal('.1'):
        raise ValueError('Предложение превышает остаток калорий')
    labels={'BREAKFAST':'Завтрак','LUNCH':'Обед','SNACK':'Перекус','DINNER':'Ужин'}
    lines=[f'Сегодня уже учтено {fact[0]:.1f} ккал. Остаток до ориентира — {remaining:.1f} ккал.']
    if not rows:
        return '\n'.join(lines+['Чтобы подобрать меню из твоих продуктов, дополни список запасов: что ещё есть дома и примерно сколько? Съеденное повторять не нужно.'])
    lines.append('Из имеющихся продуктов можно запланировать (≈ справочная оценка):')
    for r in rows:
        k,p,f,c=r['totals'];basis='готовый вес' if r['basis']=='ready' else 'сырой/сухой вес'
        lines.append(f"• {labels[r['meal']]}: {r['name']} — {r['grams']:g} г ({basis}): ≈{k:.1f} ккал · Б {p:.1f} · Ж {f:.1f} · У {c:.1f} г.")
    lines.append(f'Новое меню: ≈{total[0]:.1f} ккал · Б {total[1]:.1f} · Ж {total[2]:.1f} · У {total[3]:.1f} г.')
    forecast=tuple(a+b for a,b in zip(fact,total))
    lines.append(f'День с этим меню: ≈{forecast[0]:.1f} ккал · Б {forecast[1]:.1f} · Ж {forecast[2]:.1f} · У {forecast[3]:.1f} г.')
    gap=remaining-total[0]
    if gap>180:
        lines.append(f'Это часть рациона: до ориентира останется {gap:.1f} ккал. Какие ещё продукты есть дома? Подберём дополнение, чтобы не оставлять день недоеденным.')
    else:
        deviations=[]
        for index,low,high,label in ((1,'ProteinMin','ProteinMax','белка'),(2,'FatMin','FatMax','жиров')):
            if fields.get(low) is not None and forecast[index]<_decimal(fields[low]):
                deviations.append(f"{label} меньше нижней границы на {_decimal(fields[low])-forecast[index]:.1f} г")
            elif fields.get(high) is not None and forecast[index]>_decimal(fields[high]):
                deviations.append(f"{label} больше верхней границы на {forecast[index]-_decimal(fields[high]):.1f} г")
        if fields.get('CarbMax') is not None and forecast[3]>_decimal(fields['CarbMax']):
            deviations.append(f"углеводов больше лимита на {forecast[3]-_decimal(fields['CarbMax']):.1f} г")
        if deviations:lines.append('По целям: '+ '; '.join(deviations)+'. Можно подобрать замену.')
    lines.append('Это предложение, съеденным ничего не отмечено.')
    return '\n'.join(lines)


def answer(text,snapshot,request,policy):
    profile=snapshot.get('profile') or {}
    if profile.get('pantry_day')!=snapshot['day'] or not profile.get('pantry'):
        return 'Давай соберём меню из того, что есть. Пришли список или фото продуктов с подписью «Для плана». Уже съеденное повторять не нужно.'
    hour=local_time(snapshot).hour
    instructions=policy+'''\nДля этого запроса верни только JSON, без Markdown и пояснений.
Формат: {"items":[{"name":"название продукта из pantry","meal":"LUNCH",
"grams":100,"basis":"ready","per100":{"kcal":100,"protein":10,"fat":2,"carbs":10}}]}.
per100 — справочная оценка на 100 г; массы и КБЖУ не смешивать.
Только имеющиеся продукты из pantry, без выдуманных покупок и ингредиентов.
Соблюдай pantry_quantities: это максимальное количество на всё новое меню.
После 11:00 не предлагай BREAKFAST, после 17:00 оставь SNACK/DINNER.
Если есть только пицца, предложи имеющуюся порцию, а не полноценный выдуманный рацион.
Не заполняй весь остаток одним продуктом, если масса неизвестна: items=[] допустим.
Итоги и текст для пользователя сформирует программа. Максимум 8 простых порций.
Учитывай исключения, предпочтения, цели и уже съеденное; не добавляй съеденное снова.'''
    context={k:snapshot.get(k) for k in ('day','profile','state','computed_eaten','meals')}
    messages=[{'role':'user','content':json.dumps({'local_hour':hour,'context':context,'request':text},ensure_ascii=False)}]
    for attempt in range(2):
        try:
            raw=request({'instructions':instructions,'input':messages,'max_output_tokens':1800})
            raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw.strip(),flags=re.I)
            rows=validate(json.loads(raw),snapshot,hour)
            return render(rows,snapshot)
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            messages.append({'role':'user','content':'Исправь JSON: '+str(exc)+'. Соблюдай исходные данные и формат.'})
        except SyncError:
            break
    return render([],snapshot)
