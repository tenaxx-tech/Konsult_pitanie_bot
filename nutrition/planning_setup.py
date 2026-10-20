"""Gather pantry separately from consumed food before the first daily plan."""
import re


def handle(accounts, account, text, day, remote_factory, attachment_ids=None):
    from .channels import _explicit_consumption
    user=accounts.get(account)
    profile=user['profile']
    lower=text.casefold().replace('ё','е').strip()
    # A bare food list during setup is ambiguous: never guess pantry vs fact.
    choices={'🥫 Есть дома':'pantry','🍽 Уже съел(а)':'eaten','🧮 Только рассчитать':'estimate'}
    pending=profile.get('food_context_question')
    if pending and pending.get('day')!=day:
        profile.pop('food_context_question',None)
        accounts.save(account,profile,user['stage'])
        pending=None
    if pending and text.strip() in choices:
        intent=choices[text.strip()]
        description=pending['text']
        profile.pop('food_context_question',None)
        accounts.save(account,profile,user['stage'])
        if intent=='pantry':
            text='Для плана: '+description
            lower=text.casefold()
        else:
            return {'intake_text':('Съел: ' if intent=='eaten' else 'Рассчитай, это не факт еды: ')+description}
    elif pending and _explicit_consumption(text):
        profile.pop('food_context_question',None)
        accounts.save(account,profile,user['stage'])
    elif pending and not text.startswith('/') and not attachment_ids and not _explicit_consumption(text):
        if not any(x in lower for x in ('не знаю','калорий','посчитай')):
            pending['text']=(pending['text']+'\nУточнение: '+text)[:8000]
            profile['food_context_question']=pending
            accounts.save(account,profile,user['stage'])
        return 'Калорийность и БЖУ рассчитаю сам. Уточните кнопкой: это продукты дома, уже съеденное или только расчёт?'
    elif (profile.get('planning_setup') and not attachment_ids and not text.startswith('/')
          and not _explicit_consumption(text) and
          re.search(r'картоф|наг[г]?ет|огур|творог|яйц|куриц|греч|банан|помид|рыб|макарон|кефир|йогур',lower) and
          not any(x in lower for x in ('для плана','для меню','дома есть','есть дома','имеющиеся продукты','в холодильнике'))):
        profile['food_context_question']={'day':day,'text':text[:8000]}
        accounts.save(account,profile,user['stage'])
        return 'Это продукты, которые есть дома, или вы уже их съели? Выберите кнопку. Калорийность и БЖУ я рассчитаю сам; нужны только продукты и количество.'
    no_food=bool(re.fullmatch(r'(?:сегодня\s+)?(?:я\s+)?(?:еще\s+)?не\s+(?:ел[аи]?|кушал[аи]?)(?:\s+сегодня)?[.!]?',lower))
    if no_food:
        profile['no_food_confirmed_day']=day
        accounts.save(account,profile,user['stage'])
        return ('Понял, сегодня ещё не ели. Пришлите фото или список имеющихся продуктов с подписью «Для плана». '
                'Если продукты уже перечислены, нажмите «🥗 План питания».')
    inventory=(bool(attachment_ids) and profile.get('planning_setup') and not _explicit_consumption(text)) or (
        not _explicit_consumption(text) and not text.startswith('/') and
        any(x in lower for x in ('для плана','для меню','дома есть','есть дома','имеющиеся продукты','в холодильнике')))
    if inventory:
        pantry=text.strip()
        if attachment_ids:
            from .gigachat_chat import extract_intake
            result=extract_intake('Это имеющиеся продукты для будущего меню, не съеденная еда. Перечисли только видимые продукты. '+text,
                                  day,{},attachment_ids)
            names=[str(item.get('name') or item.get('product') or '') for item in result.get('items',[])]
            names=[x for x in names if x]
            if not names:
                return 'Не удалось уверенно распознать продукты. Пришлите более чёткое фото или перечислите их текстом. В дневник ничего не записано.'
            pantry+='\nПо фото (проверьте названия): '+', '.join(names)
        profile['pantry']=pantry[:4000]
        profile['pantry_day']=day
        accounts.save(account,profile,user['stage'])
        return ('Продукты сохранены для подбора меню; съеденными их не отмечал.\n'+pantry[:2000]+
                '\nЧто уже сегодня съели и выпили? Напишите с примерным количеством. Если ничего — «Сегодня ещё не ел». '
                'После записи съеденного нажмите «🥗 План питания».')
    from .planning import PLAN_REQUEST
    if PLAN_REQUEST.search(text or '') and (profile.get('planning_setup') or (profile.get('pantry_day') and profile.get('pantry_day')!=day)):
        if profile.get('pantry_day')!=day:
            return 'Сначала пришлите фото или список продуктов, которые есть, с подписью «Для плана». Отдельно напишите, что уже сегодня съели и выпили.'
        snapshot=remote_factory().read(day)
        if not snapshot.get('meals') and profile.get('no_food_confirmed_day')!=day:
            return 'Перед подбором меню уточните, что уже сегодня съели и выпили, с количеством. Если ещё ничего — напишите «Сегодня ещё не ел».'
        profile['planning_setup']=False
        accounts.save(account,profile,user['stage'])
    return None
