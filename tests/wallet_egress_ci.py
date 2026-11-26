"""Exercise the exact Squid policy with synthetic TLS peers on internal networks."""
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from nginx_recovery_ci import NGINX_IMAGE, run, write_certificates

ROOT = Path(__file__).resolve().parents[1]
SQUID = "ubuntu/squid@sha256:d9c34c5ab74c6dd41cf1070d9b4221aac072e67e983527ca2d41512958f77506"
TARGETS = ("oauth2.googleapis.com", "walletobjects.googleapis.com")


def inspect(kind, name):
    return json.loads(run("docker", kind, "inspect", name))[0]


def main():
    prefix = "j2m-egress-" + uuid.uuid4().hex[:8]
    inbound, outward = prefix + "-in", prefix + "-out"
    proxy, target = prefix + "-proxy", prefix + "-target"
    try:
        for network in (inbound, outward):
            run("docker", "network", "create", "--internal", network)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            folder.chmod(0o755)  # Allow the unprivileged client to read the synthetic public CA.
            ca, _, _ = write_certificates(folder, hostnames=TARGETS)
            (folder / "policy.conf").write_bytes((ROOT / "tests/fixtures/wallet-egress-squid.conf").read_bytes())
            (folder / "nginx.conf").write_text("""
events {}
http {
  server {
    listen 443 ssl;
    ssl_certificate /proof/server.crt;
    ssl_certificate_key /proof/server.key;
    location / { return 200 'synthetic TLS peer'; }
  }
}
""", encoding="utf-8")
            mount = ("--mount", f"type=bind,src={folder},dst=/proof,readonly")
            run("docker", "run", "-d", "--name", target, "--network", outward, *mount,
                NGINX_IMAGE, "nginx", "-c", "/proof/nginx.conf", "-g", "daemon off;")
            ip = inspect("container", target)["NetworkSettings"]["Networks"][outward]["IPAddress"]
            host_map = [value for host in TARGETS for value in ("--add-host", f"{host}:{ip}")]
            run("docker", "run", "-d", "--name", proxy, "--network", inbound, "--network-alias", "proxy",
                *mount, *host_map, "--entrypoint", "/usr/sbin/squid", SQUID, "-N", "-f", "/proof/policy.conf")
            run("docker", "network", "connect", outward, proxy)
            for network, names in ((inbound, {proxy}), (outward, {proxy, target})):
                info = inspect("network", network)
                assert info["Internal"] and {item["Name"] for item in info["Containers"].values()} == names
            proxy_info = inspect("container", proxy)
            assert not proxy_info["HostConfig"].get("PortBindings")
            assert set(proxy_info["NetworkSettings"]["Networks"]) == {inbound, outward}
            client = f"""
import http.client, socket, ssl, time
context=ssl.create_default_context(cafile='/proof/{ca.name}')
for attempt in range(20):
    try:
        with socket.create_connection(('proxy', 3128), 1): pass
        break
    except OSError:
        if attempt == 19: raise
        time.sleep(.25)
for host in {TARGETS!r}:
    conn=http.client.HTTPSConnection('proxy', 3128, timeout=5, context=context)
    conn.set_tunnel(host, 443)
    conn.request('GET','/proof')
    reply=conn.getresponse()
    assert reply.status==200 and reply.read()==b'synthetic TLS peer'
    conn.close()
for host,port in [('example.invalid',443),('dev.oauth2.googleapis.com',443),
                  ('walletobjects.googleapis.com.example.invalid',443),
                  ('oauth2.googleapis.com',444),({ip!r},443),('synthetic-supabase.invalid',443)]:
    conn=http.client.HTTPConnection('proxy', 3128, timeout=5)
    conn.set_tunnel(host,port)
    try:
        conn.connect()
        raise AssertionError('CONNECT unexpectedly accepted')
    except OSError as exc:
        assert '403' in str(exc)
    finally:
        conn.close()
conn=http.client.HTTPConnection('proxy', 3128, timeout=5)
conn.request('GET','https://oauth2.googleapis.com/')
assert conn.getresponse().status==403
try:
    with socket.create_connection(({ip!r},443), 1): pass
except OSError:
    pass
else:
    raise AssertionError('direct connection bypassed the proxy')
"""
            # Use a separate container so no client process runs in the proxy namespace.
            subprocess.run(["docker", "run", "--rm", "--network", inbound, *mount,
                            "j2m-interactive:wallet-demo-synthetic", "python", "-c", client],
                           check=True, timeout=30)
        print("Squid exact CONNECT ACL, end-to-end TLS and direct-route isolation verified.")
    finally:
        for name in (proxy, target):
            run("docker", "rm", "-f", name, check=False)
        for name in (outward, inbound):
            run("docker", "network", "rm", name, check=False)


if __name__ == "__main__":
    main()
