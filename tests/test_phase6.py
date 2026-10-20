import io
import unittest
import openpyxl
from unittest.mock import patch
from datetime import date, datetime, timedelta
import json

from app import create_app
from app.extensions import db
from app.models import Business, BusinessCategory, Customer, Staff, Transaction

class Phase6TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('testing')
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Create a category
        self.category = BusinessCategory(name='Restaurante', slug='restaurante')
        db.session.add(self.category)
        db.session.commit()

        # Create two businesses
        self.business1 = Business(
            name='Negocio Uno',
            slug='negocio-uno',
            category_id=self.category.id,
            primary_color='#000000',
            secondary_color='#FFFFFF',
            loyalty_type='stamps',
            stamps_reward_limit=5,
            cooldown_minutes=5,
            is_active=True,
            web_access_enabled=True,
            mobile_access_enabled=True,
        )
        self.business2 = Business(
            name='Negocio Dos',
            slug='negocio-dos',
            category_id=self.category.id,
            primary_color='#111111',
            secondary_color='#EEEEEE',
            loyalty_type='stamps',
            stamps_reward_limit=5,
            cooldown_minutes=5,
            is_active=True,
            web_access_enabled=True,
            mobile_access_enabled=True,
        )
        db.session.add_all([self.business1, self.business2])
        db.session.commit()

        # Manager staff for business1
        self.manager_staff = Staff(
            business_id=self.business1.id,
            username='manager1',
            name='Manager One',
            role='manager',
            web_access_enabled=True,
            mobile_access_enabled=True,
            permissions='view_stats,view_transactions,export_reports,scanner_operate',
        )
        self.manager_staff.set_password('ManagerPassword123!')
        db.session.add(self.manager_staff)
        db.session.commit()
        mobile_login = self.client.post('/api/scanner/login', json={
            'username': 'manager1', 'password': 'ManagerPassword123!', 'business_slug': 'negocio-uno'})
        web_login = self.client.post('/api/scanner/login', json={
            'username': 'manager1', 'password': 'ManagerPassword123!', 'business_slug': 'negocio-uno', 'channel': 'web'})
        self.auth = {'Authorization': f"Bearer {mobile_login.get_json()['access_token']}"}
        self.web_auth = {'Authorization': f"Bearer {web_login.get_json()['access_token']}"}

        # Customer for business1
        self.customer = Customer(
            business_id=self.business1.id,
            full_name='Cliente Uno',
            email='c1@example.com',
            birthdate=date.today(),
            current_stamps=4,
        )
        db.session.add(self.customer)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_cli_birthday_command_calls_service(self):
        runner = self.app.test_cli_runner()
        with patch('app.services.birthday_service.BirthdayService.run_daily_campaign') as mock_run:
            mock_run.return_value = 2
            result = runner.invoke(args=['check-birthdays'])
            self.assertEqual(result.exit_code, 0)
            mock_run.assert_called_once()
            self.assertIn('Sent 2 birthday greetings', result.output)

    def test_manager_data_isolation(self):
        # Access own business transactions – should succeed
        resp = self.client.get(
            f'/manager/{self.business1.id}/transactions',
            query_string={'staff_id': 999999}, headers=self.web_auth
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['status'], 'success')

        # Attempt to access other business – should be forbidden
        resp2 = self.client.get(
            f'/manager/{self.business2.id}/transactions',
            query_string={'staff_id': self.manager_staff.id}, headers=self.web_auth
        )
        self.assertEqual(resp2.status_code, 403)
        self.assertIn('no autorizado', resp2.get_json()['message'])

    def test_reward_notification_triggered_on_stamp_limit(self):
        # Process a transaction that reaches the stamp reward limit (5)
        payload = {
            'token': self.customer.qr_code_token,
            'action': 'earn_stamp',
            'amount': 1,
            'request_id': 'phase6-reward',
        }
        resp = self.client.post('/api/scanner/process', json=payload, headers=self.auth)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn(data['wallet_sync']['google_status'], ['mock', 'success'])

    def test_manager_exports_use_authenticated_business(self):
        excel = self.client.get(
            f'/manager/business/{self.business1.id}/export/excel', headers=self.web_auth)
        self.assertEqual(excel.status_code, 200)
        self.assertEqual(excel.mimetype, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        self.assertIn('negocio-uno-reporte.xlsx', excel.headers['Content-Disposition'])
        workbook = openpyxl.load_workbook(io.BytesIO(excel.data))
        self.assertIn('Clientes Registrados', workbook.sheetnames)

        pdf = self.client.get(
            f'/manager/business/{self.business1.id}/export/pdf', headers=self.web_auth)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, 'application/pdf')
        self.assertIn('negocio-uno-reporte.pdf', pdf.headers['Content-Disposition'])
        self.assertTrue(pdf.data.startswith(b'%PDF'))

        denied = self.client.get(
            f'/manager/business/{self.business2.id}/export/pdf', headers=self.web_auth)
        self.assertEqual(denied.status_code, 403)

    def test_anti_fraud_cooldown_enforced(self):
        # First transaction – should succeed
        payload = {
            'token': self.customer.qr_code_token,
            'action': 'earn_stamp',
            'amount': 1,
            'request_id': 'phase6-cooldown-1',
        }
        resp1 = self.client.post('/api/scanner/process', json=payload, headers=self.auth)
        self.assertEqual(resp1.status_code, 200)

        # Immediate second transaction – should be blocked by cooldown
        payload['request_id'] = 'phase6-cooldown-2'
        resp2 = self.client.post('/api/scanner/process', json=payload, headers=self.auth)
        self.assertEqual(resp2.status_code, 429)
        self.assertIn('cooldown', resp2.get_json()['message'])

if __name__ == '__main__':
    unittest.main()
