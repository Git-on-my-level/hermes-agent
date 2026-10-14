"""A completed worker result must survive slow interpreter shutdown."""
import sys


def test_worker_result_is_not_rejected_after_five_second_shutdown(monkeypatch):
    from pm import client
    script = ("import sys, json, time; request=json.loads(sys.stdin.readline()); "
              "print(json.dumps({'id':request['id'], 'type':'result', 'result':True}), flush=True); "
              "time.sleep(5.2)")
    monkeypatch.setattr(client, "_worker_command", lambda *a: [sys.executable, "-I", "-c", script])
    assert client._request("venv_is_current", {}) is True
