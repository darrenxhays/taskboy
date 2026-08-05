#!/usr/bin/env python3
"""git credential helper: fetches a fresh installation token from the agent-harness broker at use-time.

wired into each task session via GIT_CONFIG_* env vars — tokens never touch disk, git config,
or argv. stdlib only; runs under the task's (non-privileged) user.
"""

import json
import os
import socket
import sys


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "get":
        return  # store/erase are no-ops
    sys.stdin.read()  # drain the credential description git sends
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(os.environ["AGENT_HARNESS_BROKER_SOCKET"])
        sock.sendall((json.dumps({"op": "git-credentials", "nonce": os.environ["AGENT_HARNESS_TASK_NONCE"]}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    response = json.loads(data)
    if "error" in response:
        print(f"agent-harness credential helper: {response['error']}", file=sys.stderr)
        sys.exit(1)
    print(f"username={response['username']}")
    print(f"password={response['password']}")


if __name__ == "__main__":
    main()
