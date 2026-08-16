import os
from pathlib import Path

# load .env manually — no extra deps
for line in Path(".env").read_text().splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from openhands.sdk import LLM, Conversation
from openhands.tools.preset.default import get_default_agent
from openhands.workspace import DockerWorkspace

llm = LLM(
    model="litellm_proxy/tekton-fast",          # tier alias only — ADR-2
    base_url="http://192.168.1.201:4000",
    api_key=os.environ["LITELLM_API_KEY"],
)

agent = get_default_agent(llm=llm, cli_mode=True)

with DockerWorkspace(network="tekton-net",
    server_image="ghcr.io/openhands/agent-server:1.36.1-python",  # the pin
) as workspace:
    convo = Conversation(agent=agent, workspace=workspace)
    convo.send_message(
        "Create a file named hello.txt containing exactly: Tekton lives. "
        "Then print its contents."
    )
    convo.run()
    print("=== DONE ===")
