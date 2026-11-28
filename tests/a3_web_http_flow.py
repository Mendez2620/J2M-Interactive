"""Exercise previously unproven HTTP operations under the restricted web login."""
import re
import sys

from sqlalchemy import text

from app.extensions import db
from app.models import Business, Customer, PlatformAdmin, Promoter, ReferralParticipant, Staff
from app.staff_auth import issue_token_pair
from app.routes.business import _referral_context
from run import app


def seed():
    a = Business(name="Synthetic points", slug="a3-tenant-one", loyalty_type="points",
                 web_access_enabled=True, referrals_enabled=True)
    b = Business(name="Synthetic other", slug="a3-tenant-two", loyalty_type="stamps",
                 web_access_enabled=True, referrals_enabled=True)
    db.session.add_all([a,b])
    db.session.flush()
    admin = PlatformAdmin(name="Synthetic admin", email="admin@example.test")
    admin.set_password("SyntheticPassword123!")
    manager = Staff(business_id=a.id, username="manager-a3", email="manager@example.test", role="manager",
                    is_active=True, web_access_enabled=True,
                    permissions="manage_cashiers,create_cashiers,edit_cashiers,export_reports,view_stats,manage_referral_incentives")
    manager.set_password("SyntheticPassword123!")
    promoter = Promoter(business_id=a.id, name="Synthetic promoter", username="promoter-a3", is_active=True)
    promoter.set_password("SyntheticPassword123!")
    db.session.add_all([admin,manager,promoter])
    db.session.flush()
    db.session.add(ReferralParticipant(business_id=a.id, promoter_id=promoter.id, participant_type="external_promoter",
                                       invitation_code="synthetic-a3-invitation", is_enabled=True))
    db.session.commit()


def csrf(client, route):
    response = client.get(route)
    match = re.search(rb'name="csrf_token"\s+value="([^"]+)"', response.data)
    assert match, response.status_code
    return match[1].decode()


def check(response, expected):
    assert response.status_code == expected, (response.request.path, response.status_code,
                                              response.get_json(silent=True))
    return response


def exercise():
    with app.app_context():
        assert db.session.execute(text("SELECT current_user,session_user")).one() == ("j2m_web","j2m_web")
        manager=Staff.query.filter_by(username="manager-a3").one()
        tenant, other=manager.business_id, Business.query.filter_by(slug="a3-tenant-two").one().id
        access,_=issue_token_pair(manager,channel="web")
        participant=ReferralParticipant.query.filter_by(invitation_code="synthetic-a3-invitation").one()
        context=_referral_context(participant)

    public=app.test_client()
    check(public.get("/join/a3-tenant-one?ref=synthetic-a3-invitation"),200)
    with public.session_transaction() as session:
        token=session["csrf_token"]
    registration=dict(full_name="Synthetic referred",phone="5511239913",
                      terms_accepted=True,privacy_accepted=True,referral_context=context)
    check(public.post("/join/a3-tenant-one",json=registration,headers={"X-CSRFToken":token}),201)
    check(public.post("/join/a3-tenant-one",json=registration,headers={"X-CSRFToken":token}),409)
    other_tenant=app.test_client()
    check(other_tenant.get("/join/a3-tenant-two?ref=synthetic-a3-invitation"),200)
    with other_tenant.session_transaction() as session:
        other_csrf=session["csrf_token"]
    check(other_tenant.post("/join/a3-tenant-two",
                           json={**registration,"phone":"5511239924"},
                           headers={"X-CSRFToken":other_csrf}),201)

    promoter=app.test_client()
    path="/promoter/login"
    check(promoter.post(path,data={"identity":"promoter-a3","password":"SyntheticPassword123!",
                                  "business_slug":"a3-tenant-one","csrf_token":csrf(promoter,path)}),302)
    check(promoter.get("/promoter/portal"),200)
    with promoter.session_transaction() as session:
        token=session["csrf_token"]
    check(promoter.post("/promoter/logout",data={"csrf_token":token}),302)

    staff=app.test_client()
    with staff.session_transaction() as session:
        session["csrf_token"]="synthetic-a3-csrf"
    headers={"Authorization":f"Bearer {access}","X-CSRFToken":"synthetic-a3-csrf"}
    route=f"/manager/business/{tenant}"
    check(staff.get(f"/manager/business/{other}/export/excel",headers=headers),403)
    check(staff.get(route+"/export/excel",headers=headers),200)
    check(staff.get(route+"/export/pdf",headers=headers),200)
    cashier=check(staff.post(route+"/staff",headers=headers,
                  json={"username":"cashier-a3","password":"SyntheticPassword123!"}),201).get_json()["cashier"]
    check(staff.patch(route+f"/staff/{cashier['id']}",headers=headers,json={"name":"Renamed"}),200)

    rules=route+"/referral-incentive-rules"
    rule_payload=dict(beneficiary_type="external_promoter",loyalty_program="points",reward_type="points",
                      reward_amount="5",eligible_condition="first_verified_purchase",status="draft",
                      reason="Configuración sintética para revisión.")
    rule=check(staff.post(rules,headers={**headers,"Idempotency-Key":"a3-create-rule"},
                         json=rule_payload),201).get_json()["rule"]
    check(staff.get(rules,headers=headers),200)
    check(staff.patch(rules+f"/{rule['id']}",headers={**headers,"Idempotency-Key":"a3-update-rule"},
                      json={"version":rule["version"],"reward_amount":"7",
                            "reason":"Ajuste sintético de prueba."}),200)

    admin=app.test_client()
    path="/admin/login"
    check(admin.get("/admin/dashboard",headers={"Accept":"application/json"}),401)
    check(admin.post(path,data={"email":"admin@example.test","password":"SyntheticPassword123!",
                               "csrf_token":csrf(admin,path)}),302)
    check(admin.get("/admin/dashboard",headers={"Accept":"application/json"}),200)
    with admin.session_transaction() as session:
        token=session["csrf_token"]
    check(admin.post("/admin/categories",headers={"X-CSRFToken":token},json={"name":"Synthetic category"}),201)
    check(admin.post("/admin/business/create",headers={"X-CSRFToken":token},
                     json={"name":"Synthetic created","slug":"a3-created"}),201)

    with app.app_context():
        assert db.session.scalar(text("SELECT count(*) FROM referral_eligibilities WHERE status='pending'")) == 1
        assert db.session.scalar(text("SELECT count(*) FROM transactions")) == 0
        assert db.session.scalar(text("SELECT count(*) FROM loyalty_ledger_entries")) == 0
        assert db.session.scalar(text("SELECT count(*) FROM google_wallet_sync_jobs")) == 0
        assert all((c.current_points,c.current_stamps)==(0,0) for c in Customer.query.all())


app.config.update(TESTING=True, CSRF_ENABLED=True)
assert app.config["GOOGLE_WALLET_MODE"] == app.config["APPLE_WALLET_MODE"] == "disabled"
if sys.argv[1]=="seed":
    with app.app_context():
        seed()
else:
    exercise()
