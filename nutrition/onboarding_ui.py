"""Local onboarding buttons and explicit city-to-timezone lookup; no model calls."""
import os
import sqlite3
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ACTIONS = {'✅ Согласен': '/agree', '❌ Не согласен': '/decline',
           '✅ Принять ориентиры': '/accept', '✏️ Заполнить заново': '/profile',
           '👨 Мужчина': 'м', '👩 Женщина': 'ж',
           '🚶 Низкая': 'низкая', '🏃 Средняя': 'средняя', '🏋️ Высокая': 'высокая',
           '📉 Снижение': 'снижение', '⚖️ Поддержание': 'поддержание', '📈 Набор': 'набор',
           'Нет исключений': 'нет'}

# Only explicit matches are accepted. Unknown/ambiguous cities are never guessed.
CITY_GROUPS = {
 'Europe/Kaliningrad': 'Калининград',
 'Europe/Moscow': 'Москва|Санкт-Петербург|Петербург|СПб|Казань|Нижний Новгород|Ростов-на-Дону|Краснодар|Сочи|Воронеж|Волгоград|Тула|Белгород|Ярославль|Мурманск|Петрозаводск|Киров|Сыктывкар|Грозный|Махачкала',
 'Europe/Samara': 'Самара|Тольятти|Ижевск',
 'Europe/Saratov': 'Саратов',
 'Europe/Ulyanovsk': 'Ульяновск',
 'Europe/Astrakhan': 'Астрахань',
 'Asia/Yekaterinburg': 'Екатеринбург|Челябинск|Копейск|Уфа|Пермь|Тюмень|Курган|Оренбург|Магнитогорск|Сургут|Нижневартовск|Ханты-Мансийск|Салехард',
 'Asia/Omsk': 'Омск',
 'Asia/Novosibirsk': 'Новосибирск',
 'Asia/Barnaul': 'Барнаул',
 'Asia/Tomsk': 'Томск',
 'Asia/Krasnoyarsk': 'Красноярск|Кемерово|Новокузнецк|Абакан|Кызыл',
 'Asia/Irkutsk': 'Иркутск|Улан-Удэ|Братск',
 'Asia/Chita': 'Чита',
 'Asia/Yakutsk': 'Якутск|Благовещенск',
 'Asia/Vladivostok': 'Владивосток|Хабаровск',
 'Asia/Magadan': 'Магадан',
 'Asia/Sakhalin': 'Южно-Сахалинск',
 'Asia/Kamchatka': 'Петропавловск-Камчатский',
 'Asia/Anadyr': 'Анадырь',
 'Europe/Minsk': 'Минск', 'Asia/Almaty': 'Алматы|Астана',
 'Asia/Tashkent': 'Ташкент', 'Asia/Bishkek': 'Бишкек',
 'Asia/Yerevan': 'Ереван', 'Asia/Tbilisi': 'Тбилиси',
 'Europe/Istanbul': 'Стамбул|Анталья', 'Asia/Dubai': 'Дубай',
 'Europe/Berlin': 'Берлин', 'Europe/Amsterdam': 'Амстердам',
 'Europe/London': 'Лондон', 'Europe/Paris': 'Париж',
}
CITIES = {city.casefold(): zone for zone, names in CITY_GROUPS.items()
          for city in names.split('|')}


def resolve_city(value):
    value=value.strip()
    normalized=value.casefold().replace('ё','е')
    if normalized.startswith('г.'):normalized=normalized[2:].strip()
    zone=CITIES.get(normalized)
    if zone:return zone
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError,ValueError):
        raise ValueError('Unknown or ambiguous city') from None
    return value


def stage_keyboard(stage):
    rows={
      'consent': [['✅ Согласен'], ['❌ Не согласен']],
      'sex': [['👨 Мужчина','👩 Женщина']],
      'activity': [['🚶 Низкая','🏃 Средняя'],['🏋️ Высокая']],
      'goal': [['📉 Снижение','⚖️ Поддержание'],['📈 Набор']],
      'preferences': [['Нет исключений']],
      'timezone': [['Москва','Санкт-Петербург'],['Екатеринбург','Челябинск'],['Копейск','Новосибирск']],
      'review': [['✅ Принять ориентиры'],['✏️ Заполнить заново']],
    }.get(stage)
    if rows:
        return {'keyboard': [[{'text': x} for x in row] for row in rows],
                'resize_keyboard': True,'one_time_keyboard':False,
                'input_field_placeholder':'Выберите кнопку или напишите ответ'}
    if stage in ('height','weight','age','manual'):
        return {'remove_keyboard':True}
    from .telegram_menu import keyboard
    return keyboard()


def reply_keyboard(peer):
    database=os.environ.get('CHANNEL_DB',os.path.join(os.environ.get('DATA_DIR','/app/data'),'channels.sqlite3'))
    if os.path.isfile(database):
        try:
            with sqlite3.connect(database,timeout=2) as db:
                row=db.execute('SELECT stage FROM bot_users WHERE account=?',('telegram:'+str(peer),)).fetchone()
            if row:return stage_keyboard(row[0])
        except sqlite3.Error:
            pass
    return stage_keyboard('ready')
