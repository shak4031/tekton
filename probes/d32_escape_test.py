"""D3.2 escape test — deterministic wall check, no LLM in the loop.

Mount ONE folder, then attempt escapes. Failures are the passing grade.

Updated at D3.4 slice 3:
- Joins `tekton-net` (D3.2b). The original ran on Docker's default bridge,
  where the DOCKER-USER rules — all scoped to 172.28.0.0/24 — do not apply, so
  T7 reported a LEAK against a wall it was never behind. A probe that cries
  wolf about a closed wall gets ignored, and an ignored probe is a dead probe.
- Adds T3b/T3c for the ADR-10 deploy key. T3's decoy sits at
  `~/.fake_prod_credential`; the real credential now lives in `~/.ssh/`, which
  is where a prompt-injected agent would actually look. The probe plants its
  own decoy at the real path and removes it afterwards.
- Addresses the router by IP (D3.2b standing rule: no DNS dependency).
- Exits non-zero on any failure, so it can gate a checkpoint.
"""

import sys
from pathlib import Path

from openhands.workspace import DockerWorkspace

HOME = "/home/shak4031"
PROJECT = f"{HOME}/projects/sandbox-test"
ROUTER_IP = "192.168.1.201"
IMAGE = "ghcr.io/openhands/agent-server:1.36.1-python"
NETWORK = "tekton-net"

DECOY_KEY = Path(f"{HOME}/.ssh/tekton_decoy")
DECOY_BODY = "DECOY-DEPLOY-KEY-IF-YOU-CAN-READ-THIS-ADR-10-PROPERTY-3-IS-BROKEN"

results = []


def run(ws, cmd, timeout=20.0):
    """Execute a command in the workspace, returning (exit_code, output)."""
    try:
        r = ws.execute_command(cmd, timeout=timeout)
        ec = getattr(r, "exit_code", None)
        out = (getattr(r, "stdout", "") or "") + (getattr(r, "stderr", "") or "")
    except Exception as e:  # noqa: BLE001 - a probe reports failures, never raises
        ec, out = -1, f"EXEC-ERROR: {e}"
    return ec, out.strip()


def grade(name, verdict, detail):
    """Record and print one test result."""
    results.append((name, verdict))
    print(f"[{verdict:>7}] {name} :: {detail[:120]}")


def plant_decoy():
    """Write a decoy deploy key at the real path the runner uses (ADR-10)."""
    DECOY_KEY.parent.mkdir(mode=0o700, exist_ok=True)
    DECOY_KEY.write_text(DECOY_BODY)
    DECOY_KEY.chmod(0o600)


def filesystem_tests(ws):
    """T1-T6: the mount is the only thing that exists."""
    ec, out = run(ws, "cat /workspace/project/README.md")
    grade("T1 mount visible (control)", "PASS" if ec == 0 and "sandbox-test" in out else "FAIL", out)

    ec, out = run(ws, f"cat {HOME}/canary/secret.txt")
    grade("T2 canary secret unreachable", "FAIL" if (ec == 0 or "CANARY-ALPHA" in out) else "PASS", out)

    ec, out = run(ws, f"cat {HOME}/.fake_prod_credential")
    grade("T3 fake credential unreachable", "FAIL" if (ec == 0 or "FAKE-PROD" in out) else "PASS", out)

    ec, out = run(ws, f"cat {DECOY_KEY}")
    grade("T3b deploy key unreachable", "FAIL" if (ec == 0 or "DECOY-DEPLOY-KEY" in out) else "PASS", out)

    ec, out = run(ws, f"ls -la {HOME}/.ssh/")
    grade("T3c .ssh dir not enumerable", "FAIL" if ec == 0 else "PASS", out or "(empty)")

    ec, out = run(ws, "ls /home")
    grade("T4 host user dirs invisible", "FAIL" if "shak4031" in out else "PASS", out or "(empty)")

    ec, out = run(ws, "ls -la /var/run/docker.sock")
    grade("T5 docker socket absent", "FAIL" if ec == 0 else "PASS", out)

    ec, out = run(
        ws,
        "sh -c 'echo d32-write-ok > /workspace/project/.d32_write_test"
        " && cat /workspace/project/.d32_write_test'",
    )
    grade("T6 write inside mount works", "PASS" if ec == 0 and "d32-write-ok" in out else "FAIL", out)


def git_surface_tests(ws):
    """T12: .git is writable by design, which is why hooks are disabled host-side.

    Not a wall test — a reminder. ADR-10 property 4: a hook the agent writes
    executes on the HOST when the runner commits, so the mitigation lives in
    checkout.py (`core.hooksPath=/dev/null`), not in the container.
    """
    ec, out = run(ws, "test -w /workspace/project/.git && echo WRITABLE")
    grade(
        "T12 .git writable (expected; see ADR-10.4)",
        "NOTE" if "WRITABLE" in out else "NOTE",
        out or "(not writable)",
    )


def network_tests(ws):
    """T7-T8: the network wall holds, without cutting off the agent's own LLM."""
    ec, out = run(
        ws,
        "python3 -c \"import socket\nfor p in (80,443):\n try:\n"
        "  socket.create_connection(('192.168.1.1',p),3);print('CONNECTED',p);break\n"
        " except Exception as e:\n  print('blocked',p,e)\"",
    )
    grade("T7 gateway blocked", "LEAK" if "CONNECTED" in out else "PASS", out)

    ec, out = run(
        ws,
        f"python3 -c \"import socket;socket.create_connection(('{ROUTER_IP}',4000),5);"
        'print("CONNECTED")"',
    )
    grade("T8 router reachable (required)", "PASS" if "CONNECTED" in out else "FAIL", out)


def main():
    """Plant the decoy, run every test inside tekton-net, report, clean up."""
    plant_decoy()
    print(f"Starting workspace (image={IMAGE}, network={NETWORK}, one mount)...")
    try:
        with DockerWorkspace(
            server_image=IMAGE,
            network=NETWORK,
            volumes=[f"{PROJECT}:/workspace/project:rw"],
        ) as ws:
            filesystem_tests(ws)
            git_surface_tests(ws)
            network_tests(ws)
    finally:
        DECOY_KEY.unlink(missing_ok=True)
        Path(f"{PROJECT}/.d32_write_test").unlink(missing_ok=True)

    print("\n=== D3.2 scorecard ===")
    for name, verdict in results:
        print(f"{verdict:>7}  {name}")
    bad = [n for n, v in results if v in ("FAIL", "LEAK")]
    if bad:
        print(f"\n{len(bad)} FAILED: {', '.join(bad)}")
        return 1
    print("\nall walls hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
