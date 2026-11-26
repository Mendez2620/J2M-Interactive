import re,sys
from run import app
from app.extensions import db
from app.models import Business,Customer,PlatformAdmin,Transaction,LoyaltyLedgerEntry,GoogleWalletSyncJob
from app.services.loyalty_transaction_service import process_loyalty_transaction
with app.app_context():
 assert app.config['GOOGLE_WALLET_MODE']==app.config['APPLE_WALLET_MODE']=='disabled'
 stage=sys.argv[1]
 if stage=='before':
  b=Business(name='Synthetic legacy',slug='synthetic-legacy',loyalty_type='stamps',stamps_reward_limit=1,cooldown_minutes=0)
  a=PlatformAdmin(name='Synthetic',email='admin@example.test')
  a.set_password('synthetic-only-password')
  db.session.add_all([b,a]);db.session.flush()
  c=Customer(business_id=b.id,full_name='Synthetic')
  db.session.add(c);db.session.commit()
 else:
  b=db.session.get(Business,1);c=db.session.get(Customer,1)
  assert Transaction.query.count()==1 and LoyaltyLedgerEntry.query.count()==2
  assert c.current_stamps==1 and GoogleWalletSyncJob.query.filter_by(status='completed').count()==1
 client=app.test_client()
 response=client.get('/admin/login')
 assert response.status_code==200
 csrf=re.search(rb'name="csrf_token"\s+value="([^"]+)"',response.data)
 assert csrf
 response=client.post('/admin/login',data={'email':'admin@example.test','password':'synthetic-only-password','csrf_token':csrf[1].decode()})
 assert response.status_code in (302,303), response.status_code
 assert client.get('/admin/dashboard').status_code==200
 assert client.get('/api/ready').status_code==200
 action,tx=('earn_stamp','earn') if stage=='before' else ('redeem_stamps','redeem')
 process_loyalty_transaction(business=b,customer_id=c.id,staff_id=None,action=action,tx_type=tx,points_amount=0,stamps_amount=1,purchase_amount=0,request_id=stage,fingerprint=stage)
 db.session.commit()
 if stage=='before':
  txid=Transaction.query.one().id
  db.session.add(GoogleWalletSyncJob(business_id=b.id,customer_id=c.id,transaction_id=txid,status='completed'))
  db.session.commit()
 else:
  assert Transaction.query.count()==2 and LoyaltyLedgerEntry.query.count()==3
  assert db.session.get(Customer,1).current_stamps==0
  second=Customer(business_id=b.id,full_name='Synthetic next sequence')
  db.session.add(second);db.session.flush();assert second.id>c.id
  db.session.commit()
  assert GoogleWalletSyncJob.query.filter_by(status='completed').count()==1
 print('Legacy smoke verified',stage)
