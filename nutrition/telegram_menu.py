"""Telegram navigation uses existing actions; button presses never imply eating."""
import json
from urllib.request import Request, urlopen

BUTTON_ACTIONS = {
    '📊 Мой день': '/day',
    '🔄 Остаток': '/sync',
    '🍽 Записать еду': '/food',
    '📸 Фото еды': '/photo',
    '🥗 План питания': '/plan',
    '🛒 Меню на неделю': '/week',
}
COMMANDS = [
    {'command':'menu','description':'Главное меню'},
    {'command':'day','description':'Что съедено сегодня'},
    {'command':'sync','description':'Общий дневник и остаток КБЖУ'},
    {'command':'food','description':'Записать съеденное'},
    {'command':'photo','description':'Рассчитать еду по фото'},
    {'command':'plan','description':'План на оставшуюся часть дня'},
    {'command':'week','description':'Гибкое меню и покупки на неделю'},
    {'command':'help','description':'Как пользоваться консультантом'},
]
MENU_TEXT = ('🍽 Персональный консультант по питанию\n\n'
             'Выберите действие кнопками ниже или напишите обычное сообщение.\n'
             '📊 Мой день — уже съеденное\n'
             '🔄 Остаток — калории, БЖУ и версия общего дневника\n'
             '🍽 Записать еду — сообщить о приёме пищи\n'
             '📸 Фото еды — прислать фото и уточнить порцию\n'
             '🥗 План питания — подобрать оставшиеся приёмы\n'
             '🛒 Меню на неделю — план и список покупок\n\n'
             'План не считается съеденным. Сообщите «съел» или «выпил», когда это произошло.\n'
             'Меню команд также доступно рядом с полем сообщения.')


def keyboard():
    labels = list(BUTTON_ACTIONS)
    return {'keyboard': [[{'text': label} for label in labels[i:i+2]]
                         for i in range(0, len(labels), 2)],
            'resize_keyboard': True, 'is_persistent': True,
            'one_time_keyboard': False,
            'input_field_placeholder': 'Выберите действие или напишите сообщение'}


def normalize(text):
    text = BUTTON_ACTIONS.get(text.strip(), text)
    command, _, payload = text.strip().partition(' ')
    command = command.lower()
    if command == '/plan':
        return 'Составь план питания на остаток текущего дня с учётом уже съеденного. ' + payload
    if command == '/week':
        return 'Составь гибкое меню на 7 дней и список покупок по WeeklyPlanningProtocol. ' + payload
    return text


def configure(token, owner):
    """Configure the command dropdown for the owner, without sending chat messages."""
    if not token or not str(owner).isdigit():
        return False
    def call(method, body):
        req = Request('https://api.telegram.org/bot'+token+'/'+method,
                      data=json.dumps(body,ensure_ascii=False).encode(),
                      headers={'Content-Type':'application/json'})
        with urlopen(req,timeout=15) as response:
            return bool(json.load(response).get('ok'))
    try:
        commands_ok = call('setMyCommands', {'commands':COMMANDS,
                           'scope':{'type':'chat','chat_id':int(owner)}})
        button_ok = call('setChatMenuButton', {'chat_id':int(owner),
                         'menu_button':{'type':'commands'}})
        return commands_ok and button_ok
    except Exception:
        # Do not leak exceptions containing credential-bearing URLs; startup continues.
        return False
