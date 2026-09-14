"""PROBE_SESSION_20260914 (ordem do Maikol): a sonda http usa a sessão guardada do projeto (probe_session) só no destino ou em
probe_hosts, com cookies válidos para a URL; o valor nunca aparece no resultado; 401/403 fora de expect 401/403 é acesso
(INDETERMINADO), nunca FAIL do produto."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime


def _project(**extra):
    def config(board, config=None):
        return dict({"enabled": True, "board": board, "project_id": "pilot", "delivery_environment": "production"}, **extra)
    return config


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    monkeypatch.setattr(delivery, "_resolve_host", lambda host: [])
    (tmp_path / "secrets").mkdir()
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def _card(conn, n):
    rid = delivery.receive_request(conn,
        source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": str(700 + n)},
        text=f"Conferir a lista de cargos {n}.",
        project={"board": "pilot", "profile": "default", "delivery_type": "report"},
        attachments=[])
    request = delivery.reserve_request(conn, capacity=16)
    task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
    return kb.get_task(conn, task.id)


def _state_file(tmp_path, cookies):
    path = tmp_path / "storage-state.json"
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
    return path


class _Server:
    def __init__(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cookie = self.headers.get("Cookie") or ""
                seen.append(cookie)
                ok = "sid=segredo-da-sessao" in cookie
                body = json.dumps({"lista": ["Professor Classe I [área: Língua Portuguesa]"] if ok else []}).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def _probe(port, **extra):
    return dict({"kind": "http", "url": f"http://127.0.0.1:{port}/api/trpc/admin.lista", "json_path": "lista",
                 "expect": {"status": 200, "contains_all": ["Professor Classe I [área: Língua Portuguesa]"]}}, **extra)


def test_http_probe_sends_only_the_valid_session_cookies_for_the_url(board, server, monkeypatch):
    state = _state_file(board, [
        {"name": "sid", "value": "segredo-da-sessao", "domain": "127.0.0.1", "path": "/", "expires": -1, "secure": False},
        {"name": "velho", "value": "x", "domain": "127.0.0.1", "path": "/", "expires": time.time() - 60, "secure": False},
        {"name": "seguro", "value": "y", "domain": "127.0.0.1", "path": "/", "expires": -1, "secure": True},
        {"name": "outro", "value": "z", "domain": "example.org", "path": "/", "expires": -1, "secure": False},
        {"name": "caminho", "value": "w", "domain": "127.0.0.1", "path": "/outra", "expires": -1, "secure": False},
    ])
    monkeypatch.setattr(runtime, "project_config", _project(probe_hosts=["127.0.0.1"], probe_session=str(state)))
    with kb.connect_closing() as conn:
        task = _card(conn, 1)
        result, observed, error = delivery._execute_probe(conn, task.id, _probe(server.port))
    assert (result, error) == ("PASS", None)
    assert server.seen == ["sid=segredo-da-sessao"]
    assert "segredo-da-sessao" not in json.dumps(observed)


def test_session_stays_home_out_of_scope_with_own_credential_or_auth_none(board, monkeypatch):
    state = _state_file(board, [{"name": "sid", "value": "segredo", "domain": "example.net", "path": "/", "expires": -1, "secure": True}])
    monkeypatch.setattr(runtime, "project_config", _project(probe_hosts=["admin.example.net"], probe_session=str(state)))
    sent = []
    monkeypatch.setattr(delivery, "_run_http_probe",
                        lambda probe, env, **kw: sent.append((dict(probe.get("headers") or {}), env.get("NFOS_PROBE_SESSION_COOKIE"))) or
                        {"status": 200, "mitigated": None, "value": ["ok"]})
    base = {"kind": "http", "json_path": "lista", "expect": {"contains_all": ["ok"]}}
    with kb.connect_closing() as conn:
        task = _card(conn, 2)
        delivery._execute_probe(conn, task.id, dict(base, url="https://coleta.example.net/api"))  # fora do escopo
        delivery._execute_probe(conn, task.id, dict(base, url="https://admin.example.net/api", auth="none"))
        delivery._execute_probe(conn, task.id, dict(base, url="https://admin.example.net/api", headers={"Authorization": "Bearer publico"}))
        delivery._execute_probe(conn, task.id, dict(base, url="https://admin.example.net/api"))  # no escopo: leva a sessão
    assert [s[1] for s in sent[:3]] == [None, None, None]
    assert "Cookie" not in sent[0][0] and "Cookie" not in sent[1][0] and sent[2][0] == {"Authorization": "Bearer publico"}
    assert sent[3] == ({"Cookie": "$env:NFOS_PROBE_SESSION_COOKIE"}, "sid=segredo")


def test_refused_access_is_indeterminate_not_a_product_fail(board, monkeypatch):
    monkeypatch.setattr(runtime, "project_config", _project(probe_hosts=["admin.example.net"]))
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: {"status": 401, "mitigated": None, "value": ""})
    probe = {"kind": "http", "url": "https://admin.example.net/api", "json_path": "lista", "expect": {"status": 200, "contains_all": ["x"]}}
    with kb.connect_closing() as conn:
        task = _card(conn, 3)
        state, _observed, error = delivery._execute_probe(conn, task.id, probe)
        assert state == "INDETERMINADO" and "HTTP 401" in error and "carried no session" in error and "not a product FAIL" in error
        assert not delivery._NETWORK_ERROR_RX.search(error)  # acesso recusado não estaciona o card como destino fora do ar
        cookie_state = _state_file(board, [{"name": "__Secure-sessao", "value": "segredo", "domain": "example.net", "path": "/", "expires": -1, "secure": True}])
        monkeypatch.setattr(runtime, "project_config", _project(probe_hosts=["admin.example.net"], probe_session=str(cookie_state)))
        state, _observed, error = delivery._execute_probe(conn, task.id, probe)
        assert state == "INDETERMINADO" and "__Secure-sessao" in error and "renew that stored login" in error and "segredo" not in error
        monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: {"status": 403, "mitigated": None, "value": ["negado"], "rows": [["negado"]]})
        state, _observed, error = delivery._execute_probe(conn, task.id, dict(probe, expect={"status": 403, "contains_all": ["negado"]}))
        assert (state, error) == ("PASS", None)  # quem espera 403 mede 403


def test_probe_auth_is_session_or_none():
    base = {"kind": "http", "url": "https://admin.example.net/api", "expect": {"contains_all": ["x"]}}
    delivery._validate_probe("C1", dict(base, auth="none"))
    delivery._validate_probe("C1", dict(base, auth="session"))
    with pytest.raises(delivery.WorkflowError, match="probe.auth"):
        delivery._validate_probe("C1", dict(base, auth="cookie"))


def test_worker_protocol_explains_the_project_session():
    assert "carries the project session (probe_session" in runtime.RECORD_MODE_PROTOCOL
    assert "never a product FAIL" in runtime.RECORD_MODE_PROTOCOL
