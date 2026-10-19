import tempfile
import unittest
from unittest.mock import patch, Mock
from nutrition.accounts import Accounts, route
from nutrition.channels import answer
from nutrition.planning_setup import handle


class PlanningSetupTests(unittest.TestCase):
    def test_accept_gathers_data_without_model_menu(self):
        with tempfile.TemporaryDirectory() as d, patch.dict('os.environ',{'TELEGRAM_OWNER_ID':'1','GIGACHAT_AUTHORIZATION_KEY':'test'}):
            db=d+'/db';users=Accounts(db);u=users.start('telegram:2')
            p=u['profile'];p.update(consent=True,goals={'CaloriesGoal':1810,'ProteinMin':87,'ProteinMax':97,'FatMin':60,'FatMax':70,'CarbMax':230})
            users.save('telegram:2',p,'review')
            with patch('nutrition.gigachat_chat.answer') as model:
                reply=answer('/accept',database=db,owner_key='telegram:2')
            model.assert_not_called();self.assertIn('Фото',reply)
            remote=Mock();remote.read.return_value={'meals':[]}
            day='2026-10-20'
            self.assertIn('Сначала',handle(users,'telegram:2','Составь план',day,lambda:remote))
            reply=handle(users,'telegram:2','Для плана: яйца, творог, гречка',day,lambda:remote)
            self.assertIn('съеденными их не отмечал',reply)
            remote.upsert.assert_not_called()
            self.assertIn('Перед подбором',handle(users,'telegram:2','Составь план',day,lambda:remote))
            handle(users,'telegram:2','Сегодня ещё не ел',day,lambda:remote)
            self.assertIsNone(handle(users,'telegram:2','Составь план',day,lambda:remote))
            self.assertIn('Сначала',handle(users,'telegram:2','Составь план','2026-10-21',lambda:remote))

    def test_inventory_photo_cannot_create_fact_or_pending_meal(self):
        with tempfile.TemporaryDirectory() as d:
            users=Accounts(d+'/db');u=users.start('telegram:2');p=u['profile'];p['planning_setup']=True
            users.save('telegram:2',p,'ready');remote=Mock()
            with patch('nutrition.gigachat_chat.extract_intake',return_value={'action':'record','items':[{'name':'Яйца'}]}):
                reply=handle(users,'telegram:2','Для плана','2026-10-20',lambda:remote,['photo'])
            self.assertIn('Яйца',reply);remote.assert_not_called()
            self.assertIn('Яйца',users.get('telegram:2')['profile']['pantry'])
            self.assertIsNone(handle(users,'telegram:2','Съел яйца 100 г','2026-10-20',lambda:remote,['photo']))
