"""D3.2b — network wall re-test. Deterministic, no LLM."""
from openhands.workspace import DockerWorkspace

PI="192.168.1.201"; GW="192.168.1.1"; RIG="192.168.1.187"
results=[]

PROBE = """python3 -c "
import socket,sys
try:
    socket.create_connection(('%s',%s),4); print('CONNECTED')
except Exception as e: print('BLOCKED', type(e).__name__)
" """

def t(ws, name, host, port, want):
    r = ws.execute_command(PROBE % (host, port), timeout=25)
    out = ((getattr(r,'stdout','') or '')+(getattr(r,'stderr','') or '')).strip()
    got = 'CONNECTED' if 'CONNECTED' in out else 'BLOCKED'
    ok = 'PASS' if got == want else ('LEAK' if want=='BLOCKED' else 'FAIL')
    results.append((ok,name,f'{got} (want {want})'))
    print(f'[{ok:>4}] {name}: {got}')

with DockerWorkspace(
    server_image="ghcr.io/openhands/agent-server:1.36.1-python",
    volumes=["/home/shak4031/projects/sandbox-test:/workspace/project:rw"],
    network="tekton-net",
) as ws:
    r = ws.execute_command("hostname -i; getent ahostsv4 hermes || echo NO-A-RECORD", timeout=20)
    print("--- container identity/DNS ---")
    print(((getattr(r,'stdout','') or '')+(getattr(r,'stderr','') or '')).strip())

    t(ws,"T7  gateway :80        ", GW, 80,   "BLOCKED")
    t(ws,"T7b gateway :443       ", GW, 443,  "BLOCKED")
    t(ws,"T8  router  :4000 (IP) ", PI, 4000, "CONNECTED")
    t(ws,"T8b hermes  :4000 (name)","hermes",4000,"CONNECTED")
    t(ws,"T9  rig Ollama :11434  ", RIG,11434,"BLOCKED")
    t(ws,"T11 Pi Hermes  :8787   ", PI, 8787, "BLOCKED")
    t(ws,"T10 internet pypi :443 ","pypi.org",443,"CONNECTED")

print("\n=== D3.2b scorecard ===")
for ok,name,detail in results: print(f"{ok:>4}  {name} {detail}")
