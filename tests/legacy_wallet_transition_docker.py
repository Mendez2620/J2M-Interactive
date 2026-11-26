import importlib.util,json,subprocess,tempfile,time,uuid
from pathlib import Path
root=Path(__file__).resolve().parents[1]; spec=importlib.util.spec_from_file_location("h",root/"tests/nginx_recovery_ci.py"); h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)
r=h.run; before=h.production_ids(); p="j2m-legacy-tls-"+uuid.uuid4().hex[:8]
n={i:p+"-"+i for i in ("db","web","worker","proxy")}
img=r("docker","image","inspect","--format","{{.Id}}","j2m-interactive:dae140e3684be29c534be3e1725598d226b68e03")
new=r("docker","image","inspect","--format","{{.Id}}","j2m-interactive:wallet-demo-synthetic")
probe=(root/"tests/legacy_wallet_fixture.py").read_text(encoding="utf-8")
def exercise(stage):
    subprocess.run(["docker","run","--rm","-i",*cfg,img,"python","-",stage],input=probe,text=True,check=True,timeout=60)
cfg=["--network",p,"--env-file",str(root/".env.wallet-demo.example"),"-e","DATABASE_URL=postgresql://j2m_test:synthetic@db/j2m_test","-e","GOOGLE_WALLET_MODE=disabled","-e","APPLE_WALLET_MODE=disabled"]
def start(role):
    cmd=() if role=="web" else ("flask","--app","run.py","run-google-wallet-sync-worker")
    process="web" if role=="web" else "google-wallet-worker"
    r("docker","run","-d","--name",n[role],"--network-alias",role,"--label",f"com.docker.compose.project={p}","--label",f"com.docker.compose.service={role}",*cfg,"-e",f"J2M_PROCESS_TYPE={process}",img,*cmd)
def attest(folder):
    ids={role:json.loads(r("docker","inspect",name))[0] for role,name in n.items()}
    net=json.loads(r("docker","network","inspect",p))[0]
    assert net["Internal"] and set(net["Containers"])=={obj["Id"] for obj in ids.values()}
    for role in ("web","worker"):
        obj=ids[role]; labels=obj["Config"]["Labels"]
        assert obj["Image"]==img and obj["State"]["Running"] and set(obj["NetworkSettings"]["Networks"])=={p}
        assert labels["com.docker.compose.project"]==p and labels["com.docker.compose.service"]==role
        r("docker","exec",n[role],"python","scripts/container_healthcheck.py")
    ip=ids["web"]["NetworkSettings"]["Networks"][p]["IPAddress"]
    for path in ("health","ready"):
        nonce=uuid.uuid4().hex
        client="import socket, ssl; c=ssl.create_default_context(cafile='/proof/ca.crt'); s=c.wrap_socket(socket.create_connection(('proxy',8443),3),server_hostname='localhost'); s.sendall("+repr(f"GET /api/{path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nX-J2M-Probe: {nonce}\r\n\r\n".encode())+"); b=s.makefile('rb'); assert b.readline().split()[1]==b'200'; assert b'x-j2m-revision:' not in b.read().lower()"
        r("docker","run","--rm","--network",p,"--mount",f"type=bind,src={folder},dst=/proof,readonly",new,"python","-c",client)
        assert [line for line in r("docker","logs",n["proxy"]).splitlines() if line.startswith(nonce+" ")]==[f"{nonce} {ip}:8000 200"]
    return ids["web"]["Id"]
r("docker","network","create","--internal",p)
try:
    r("docker","run","-d","--name",n["db"],"--network",p,"--network-alias","db","--tmpfs","/var/lib/postgresql/data","-e","POSTGRES_USER=j2m_test","-e","POSTGRES_DB=j2m_test","-e","POSTGRES_PASSWORD=synthetic","postgres:16-alpine")
    time.sleep(4)
    r("docker","run","--rm",*cfg,new,"flask","--app","run.py","db","upgrade","9d0e1f2a3b4c")
    exercise("before")
    r("docker","run","--rm",*cfg,new,"flask","--app","run.py","db","upgrade","c3d4e5f6a7b8")
    schema_check="from run import app; from app.extensions import db; from scripts.check_wallet_referral_schema import validate_schema; a=app.app_context(); a.push(); assert not validate_schema(db.session.connection());"
    r("docker","run","--rm",*cfg,new,"python","-c",schema_check)
    reserve = """
INSERT INTO google_wallet_write_reservations
(operation_id, owner_token, issuer, kind, resource_id, business_id, customer_id, state, active_slot, updated_at)
VALUES (repeat('a',32), repeat('b',32), 'synthetic', 'loyaltyObject', 'synthetic.object', 1, 1, 'uncertain', 1, now());
"""
    r("docker","exec",n["db"],"psql","-U","j2m_test","-d","j2m_test","-v","ON_ERROR_STOP=1","-c",reserve)
    exercise("after")
    persisted = r("docker","exec",n["db"],"psql","-U","j2m_test","-d","j2m_test","-Atc",
                  "SELECT count(*) FROM google_wallet_write_reservations "
                  "WHERE operation_id=repeat('a',32) AND state='uncertain' AND active_slot=1")
    assert persisted == "1"
    start("web");start("worker")
    with tempfile.TemporaryDirectory() as tmp:
        folder=Path(tmp);h.write_certificates(folder)
        (folder/"nginx.conf").write_text(h.proxy_config().replace("access_log off;", "log_format proof '$http_x_j2m_probe $upstream_addr $upstream_status'; access_log /dev/stdout proof;"),encoding="utf-8")
        r("docker","run","-d","--name",n["proxy"],"--network",p,"--network-alias","proxy","--mount",f"type=bind,src={folder},dst=/etc/nginx/tls,readonly",h.NGINX_IMAGE,"nginx","-c","/etc/nginx/tls/nginx.conf","-g","daemon off;")
        time.sleep(5); first=attest(folder)
        r("docker","rm","-f",n["web"]);start("web")
        r("docker","exec",n["proxy"],"nginx","-c","/etc/nginx/tls/nginx.conf","-s","reload")
        time.sleep(5); assert attest(folder)!=first
        r("docker","rm","-f",n["proxy"])
    print("Legacy TLS: exact request-to-upstream correlation verified on initial and recreated web; one isolated worker.")
finally:
    for name in n.values():r("docker","rm","-f",name,check=False)
    r("docker","network","rm",p)
    assert h.production_ids()==before
