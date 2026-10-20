"""Deterministic checks before any model-estimated meal is committed."""
from decimal import Decimal
import re
from .core import number
from .airtable import SyncError


def egg_counts(text):
    words={'одно':1,'один':1,'два':2,'две':2,'три':3,'четыре':4,'пять':5,'шесть':6}
    for word,count in words.items():
        text=re.sub(r'\b'+word+r'(?=\s+(?:вар[её]ны[хе]\s+)?я(?:йц|иц))',str(count),text)
    return re.findall(r'(\d+)\s*(?:вар[её]ны[хе]\s+)?я(?:йц|иц)',text)


def references(items, text):
    """Use transparent reference estimates for a small, unambiguous food set."""
    lower=text.casefold()
    if re.search(r'(этикетк|на\s*100\s*г|с\s+упаковки|по\s+рецепту)',lower):return items
    for item in items:
        name=item['name'].casefold()
        per100=None;url=None;canonical=None
        if re.search(r'пицц?|пицу',name) and re.search(r'пицц?|пицу',lower):
            per100=(275,12,11,32);url=None;canonical='Пицца (средняя справочная оценка)'
            match=re.search(r'(?:вес\s*)?(\d+(?:[.,]\d+)?)\s*(?:грамм(?:а|ов)?|гр\.?|г)\b',lower)
            if match and len(items)==1:item['grams']=match[1].replace(',','.')
        elif name.strip()=='банан':
            per100=(89,1.09,.33,22.84);url='https://foods.fatsecret.com/calories-nutrition/usda/bananas';canonical='Банан, мякоть'
        elif re.search(r'яйц|яич',name) and re.search(r'(вар[её]н|отварн)',name):
            count=egg_counts(lower)
            explicit=re.search(r'(?:яйц\w*|яиц)[^;\n.]{0,35}\d+\s*(?:г|гр|грамм)\b',lower)
            if count and re.search(r'средн',lower) and not explicit:item['grams']=int(count[-1])*50
            per100=(155,12.58,10.61,1.12);url='https://foods.fatsecret.com/calories-nutrition/usda/hard-boiled-egg';canonical='Яйцо куриное варёное'
        elif 'сметан' in name and re.search(r'15\s*%',lower):
            explicit=re.search(r'сметан\w*[^;\n.]{0,35}\d+\s*(?:г|гр|грамм)\b',lower)
            if 'лож' in lower and re.search(r'с\s+горк',lower) and not explicit:item['grams']=25
            per100=(160,2.7,15,3.6);url='https://prostokvashino.ru/product/smetana-15-300-g/';canonical='Сметана 15% (справочный аналог)'
        elif name.strip() in ('пельмени','пельмени отварные','пельмени вареные','пельмени варёные') and re.search(r'(после\s+варки|отварн|вар[её]н)',lower):
            per100=(219,9.9,13.9,13.5);url='https://www.perekrestok.ru/blog/kaloriynost-produktov/8/4298';canonical='Пельмени отварные (справочная оценка)'
        if per100:
            grams=number(item['grams'])
            for k,v in zip(('kcal','protein','fat','carbs'),per100):item[k]=float(number(v)*grams/100)
            item.update(name=canonical,source_detail='Справочная оценка, не этикетка пользователя'+(': '+url if url else '; средний аналог, начинка и тесто влияют на результат'),estimated=True)
    return items


def validate(items, text, nutrition_only=False):
    """Return one clarification question rather than saving implausible estimates."""
    for item in items:
        try:
            grams=number(item['grams'])
            kcal,p,f,c=[number(item[k]) for k in ('kcal','protein','fat','carbs')]
            if not 0<grams<=10000:raise ValueError()
        except (ValueError,KeyError,TypeError):return 'Уточните вес и КБЖУ: распознанные значения некорректны.'
        if p+f+c>grams*Decimal('1.05') or abs(kcal-(4*p+9*f+4*c))>max(Decimal(25),kcal*Decimal('.2')):
            return 'Калории и БЖУ распознанной порции не согласуются. Пришлите этикетку или уточните состав.'
        if nutrition_only:continue
        name=item['name'].casefold()
        if 'пельмен' in name and not re.search(r'(после\s+варки|отварн|вар[её]н|заморож|до\s+варки|сырой\s+вес)',text.casefold()):
            return 'Пельмени взвешены после варки или замороженными? Сохраню весь приём пищи после уточнения.'
        if re.search(r'(макарон|гречк|\bрис\b|круп)',name) and not re.search(r'(вар[её]н|отварн|готов|сух|сыр|пригот)',text.casefold()):
            return 'Для крупы или макарон указан сухой вес или вес после приготовления?'
        if 'сметан' in name and 'лож' in text.casefold() and not re.search(r'\d+(?:[.,]\d+)?\s*(?:г|гр|грамм)',text.casefold().split('сметан')[0][-30:]):
            # A spoon is not a measured mass; request the missing fat content/estimate explicitly.
            if not re.search(r'\d+\s*%',text):return 'Какая жирность сметаны? Ложка ровная или с горкой? Можно указать вес в граммах.'
            if not re.search(r'(горк|ровн|без\s+горки|примерно|оцен)',text.casefold()):return 'Ложка сметаны ровная или с горкой? Вес будет оценочным.'
        if re.search(r'яйц|яич',name):
            matches=egg_counts(text.casefold())
            if matches:
                count=int(matches[-1])
                if count and not Decimal(35)*count<=grams<=Decimal(70)*count:
                    return 'Уточните массу яиц без скорлупы или их размер: число яиц не соответствует распознанной массе.'
        if 'сметан' in name:
            match=re.search(r'(\d+)\s*%',text)
            if match and abs(f/grams*100-Decimal(match[1]))>3:
                return 'Жирность сметаны не соответствует распознанному БЖУ. Уточните этикетку или вес.'
    return None
