import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
from nutrition.channels import _telegram_has_image
from nutrition.accounts import Accounts
from nutrition.planning_setup import handle


class InventoryAttachmentTests(unittest.TestCase):
    def test_photo_and_document_images_are_detected(self):
        self.assertTrue(_telegram_has_image({'photo':[{'file_id':'a'}]}))
        self.assertTrue(_telegram_has_image({'document':{'mime_type':'image/jpeg','file_id':'a'}}))
        self.assertTrue(_telegram_has_image({'document':{'file_name':'food.PNG','file_id':'a'}}))
        self.assertFalse(_telegram_has_image({'caption':'Это продукты для плана, я их не ела.'}))
        self.assertFalse(_telegram_has_image({'document':{'mime_type':'application/pdf'}}))

    def test_caption_only_cannot_replace_inventory_or_make_day_plan(self):
        with tempfile.TemporaryDirectory() as d:
            users=Accounts(d+'/db');u=users.start('telegram:2');p=u['profile'];p.update(planning_setup=True,pantry='Для плана: яйца',pantry_day='2026-10-20')
            users.save('telegram:2',p,'ready');remote=Mock()
            reply=handle(users,'telegram:2','Это продукты для плана, я их не ела.','2026-10-20',lambda:remote)
            self.assertIn('нет доступного изображения',reply)
            self.assertEqual(users.get('telegram:2')['profile']['pantry'],'Для плана: яйца')
            remote.assert_not_called()

    def test_known_consumption_is_not_asked_again_and_inventory_accumulates(self):
        with tempfile.TemporaryDirectory() as d:
            users=Accounts(d+'/db');u=users.start('telegram:2');p=u['profile'];p['planning_setup']=True
            users.save('telegram:2',p,'ready');remote=Mock();remote.read.return_value={'meals':[{'fields':{'Status':'CONFIRMED'}}]}
            handle(users,'telegram:2','Для плана: яйца','2026-10-20',lambda:remote)
            reply=handle(users,'telegram:2','Для плана: творог','2026-10-20',lambda:remote)
            self.assertIn('Повторно присылать их не нужно',reply)
            self.assertIn('яйца',users.get('telegram:2')['profile']['pantry'])
            self.assertIn('творог',users.get('telegram:2')['profile']['pantry'])
            remote.upsert.assert_not_called()

    def test_document_image_downloads_and_reaches_vision_upload(self):
        import json
        from nutrition.channels import _telegram_photo
        meta=MagicMock();meta.read.return_value=json.dumps({'ok':True,'result':{'file_path':'documents/food.png'}}).encode()
        meta.__enter__.return_value=meta
        image=MagicMock();image.read.return_value=b'image-bytes';image.headers.get_content_type.return_value='application/octet-stream'
        image.__enter__.return_value=image
        with patch.dict('os.environ',TELEGRAM_BOT_TOKEN='test'),patch('nutrition.channels.urlopen',side_effect=[meta,image]),patch('nutrition.gigachat_chat.upload_image',return_value='file_12345678') as upload:
            result=_telegram_photo({'document':{'file_id':'telegram-file','file_name':'food.png','mime_type':'image/png'}})
        self.assertEqual(result,'file_12345678')
        upload.assert_called_once_with(b'image-bytes','image/png','food.png')
