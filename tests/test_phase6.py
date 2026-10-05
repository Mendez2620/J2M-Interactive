import unittest
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
        )
        db.session.add_all([self.business1, self.business2])
        db.session.commit()

        # Manager staff for business1
        self.manager_staff = Staff(
            business_id=self.business1.id,
            username='manager1',
            name='Manager One',
            role='manager',
        )
        self.manager_staff.set_password('secret')
        db.session.add(self.manager_staff)
        db.session.commit()

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
            query_string={'staff_id': self.manager_staff.id}
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data['status'], 'success')

        # Attempt to access other business – should be forbidden
        resp2 = self.client.get(
            f'/manager/{self.business2.id}/transactions',
            query_string={'staff_id': self.manager_staff.id}
        )
        self.assertEqual(resp2.status_code, 403)
        self.assertIn('Acceso no autorizado', resp2.get_json()['message'])

    def test_reward_notification_triggered_on_stamp_limit(self):
        # Process a transaction that reaches the stamp reward limit (5)
        payload = {
            'customer_id': self.customer.id,
            'business_id': self.business1.id,
            'action': 'earn_stamp',
            'amount': 1,
            'staff_id': self.manager_staff.id,
        }
        resp = self.client.post('/api/scanner/process', json=payload)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['reward_notified'])
        self.assertEqual(data['wallet_sync']['google_status'], 'success')

    def test_anti_fraud_cooldown_enforced(self):
        # First transaction – should succeed
        payload = {
            'customer_id': self.customer.id,
            'business_id': self.business1.id,
            'action': 'earn_stamp',
            'amount': 1,
            'staff_id': self.manager_staff.id,
        }
        resp1 = self.client.post('/api/scanner/process', json=payload)
        self.assertEqual(resp1.status_code, 200)

        # Immediate second transaction – should be blocked by cooldown
        resp2 = self.client.post('/api/scanner/process', json=payload)
        self.assertEqual(resp2.status_code, 429)
        self.assertIn('Intenta de nuevo en', resp2.get_json()['message'])

if __name__ == '__main__':
    unittest.main()
