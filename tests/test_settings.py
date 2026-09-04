import os
import subprocess
import sys


def test_broker_socket_settings_resolve_to_absolute_paths():
    # subprocess so the patched env can't leak into the shared settings module (#130)
    env = {
        **os.environ,
        "TASKBOY_BROKER_SOCKET": "relative/broker.sock",
        "TASKBOY_REVIEWER_BROKER_SOCKET": "relative/broker-blue.sock",
        "TASKBOY_GIT_CRED_HELPER": "relative/git-cred-helper.py",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import taskboy.settings as s; print(s.BROKER_SOCKET); print(s.REVIEWER_BROKER_SOCKET); print(s.GIT_CRED_HELPER)"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    broker_socket, reviewer_broker_socket, git_cred_helper = result.stdout.splitlines()
    assert broker_socket == os.path.abspath("relative/broker.sock")
    assert reviewer_broker_socket == os.path.abspath("relative/broker-blue.sock")
    assert git_cred_helper == os.path.abspath("relative/git-cred-helper.py")
