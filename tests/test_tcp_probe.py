import json

import pytest

from app import tcp_probe


@pytest.mark.parametrize("result,expect,exit_code", [
    ("CONNECTED", "allow", 0),
    ("TIMEOUT", "deny", 0),
    ("ENETUNREACH", "deny", 0),
    ("CONNECTED", "deny", 1),
    ("TIMEOUT", "allow", 1),
    ("ECONNREFUSED", "deny", 1),
])
def test_probe_does_not_mistake_closed_port_for_firewall(
    monkeypatch, capsys, result, expect, exit_code
):
    monkeypatch.setattr(tcp_probe, "probe", lambda *args: result)
    monkeypatch.setattr("sys.argv", ["tcp_probe", "192.0.2.1", "5432", "--expect", expect])
    assert tcp_probe.main() == exit_code
    assert json.loads(capsys.readouterr().out)["pass"] == (exit_code == 0)
