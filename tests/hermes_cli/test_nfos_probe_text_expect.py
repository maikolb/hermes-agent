"""PROBE_TEXT_EXPECT_20260914: sonda http sem rows (sem json_path, ou json_path que não aponta para lista) mede contains_all e not_matches
no corpo inteiro normalizado, não nos 2000 primeiros caracteres guardados; json_path de lista segue pelas rows; corpo vazio ou maior que
o lido nunca aprova por omissão."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import nfos_delivery as delivery
from hermes_cli import nfos_principal_review as review
from hermes_cli import nfos_runtime as runtime

PAD = "x" * 5000


class _Server:
    def __init__(self, payload):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def serve():
    servers = []

    def start(payload):
        s = _Server(payload)
        servers.append(s)
        return s.url

    yield start
    for s in servers:
        s.close()


@pytest.fixture
def url(serve):
    body = [{"result": {"data": {"json": {"padding": PAD, "positions": ["Professor Classe I [área: Língua Portuguesa]"], "erro": "snapshot vazio"}}}}]
    return serve(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def _measure(probe):
    observed = delivery._run_http_probe(probe, {})
    return delivery._probe_expect_ok(probe["expect"], observed), observed


def test_contains_all_reads_the_whole_body_not_the_stored_prefix(url):
    ok, observed = _measure({"kind": "http", "url": url, "expect": {"status": 200, "contains_all": ["professor classe i [area: lingua portuguesa]"]}})
    assert ok and "Professor Classe I" not in observed["value"]  # fora dos 2000 caracteres guardados
    assert observed["body_checks"]["contains_all"] == [True] and observed["body_checks"]["complete"] is True
    ok, _ = _measure({"kind": "http", "url": url, "expect": {"status": 200, "contains_all": ["Professor Classe I [área: Língua Portuguesa]", "Matemática"]}})
    assert not ok


def test_not_matches_on_text_fails_when_the_forbidden_text_is_there(url):
    ok, observed = _measure({"kind": "http", "url": url, "expect": {"status": 200, "not_matches": "snapshot vazio"}})
    assert not ok and observed["body_checks"]["not_matches"] is True
    ok, _ = _measure({"kind": "http", "url": url, "expect": {"status": 200, "not_matches": "erro fatal"}})
    assert ok


def test_json_path_to_a_dict_checks_that_node_and_a_list_keeps_rows(url):
    ok, observed = _measure({"kind": "http", "url": url, "json_path": "0.result.data.json", "expect": {"contains_all": ["Língua Portuguesa"]}})
    assert ok and "body_checks" in observed
    ok, observed = _measure({"kind": "http", "url": url, "json_path": "0.result.data.json.positions",
                             "expect": {"contains_all": ["Professor Classe I [área: Língua Portuguesa]"]}})
    assert ok and "rows" in observed and "body_checks" not in observed


def test_empty_or_truncated_body_never_proves_absence(serve):
    empty = serve(b"")
    ok, observed = _measure({"kind": "http", "url": empty, "expect": {"status": 200, "not_matches": "erro fatal"}})
    assert not ok and observed["body_checks"]["complete"] is False
    big = serve(b'{"a":"' + b"x" * 200500 + b' erro fatal"}')
    ok, observed = _measure({"kind": "http", "url": big, "expect": {"status": 200, "not_matches": "erro fatal"}})
    assert not ok and observed["body_checks"] == {"contains_all": [], "not_matches": False, "complete": False, "scope_chars": 200000}


def test_status_still_gates_text_checks():
    checks = {"contains_all": [True], "not_matches": False, "complete": True}
    assert not delivery._probe_expect_ok({"status": 200, "contains_all": ["a"]}, {"status": 500, "body_checks": checks})
    assert delivery._probe_expect_ok({"status": 200, "contains_all": ["a"]}, {"status": 200, "body_checks": checks})
    assert not delivery._probe_expect_ok({"contains_all": ["a", "b"]}, {"status": 200, "body_checks": dict(checks, contains_all=[True, False])})


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(review, "settings", lambda: {"principal_validation": False})
    monkeypatch.setattr(delivery, "_resolve_host", lambda host: [])
    monkeypatch.setattr(runtime, "project_config",
                        lambda board, config=None: {"enabled": True, "board": board, "project_id": "pilot", "probe_hosts": ["admin.example.net"]})
    with kb.connect_closing() as conn:
        delivery.init_schema(conn)
    return tmp_path


def test_incomplete_body_without_proof_is_indeterminate(board, monkeypatch):
    incomplete = {"status": 200, "mitigated": None, "value": "", "body_checks": {"contains_all": [False], "not_matches": False, "complete": False, "scope_chars": 0}}
    monkeypatch.setattr(delivery, "_run_http_probe", lambda probe, env, **kw: dict(incomplete))
    with kb.connect_closing() as conn:
        rid = delivery.receive_request(conn, source={"platform": "telegram", "chat_id": "-10001", "thread_id": "41", "message_id": "801"},
                                       text="Conferir a lista.", project={"board": "pilot", "profile": "default", "delivery_type": "report"}, attachments=[])
        request = delivery.reserve_request(conn, capacity=16)
        task = delivery.bootstrap_card(conn, rid, request["claim_token"], pid=os.getpid())
        for expect in ({"status": 200, "not_matches": "erro fatal"}, {"status": 200, "contains_all": ["Língua Portuguesa"]}):
            state, _observed, error = delivery._execute_probe(conn, task.id, {"kind": "http", "url": "https://admin.example.net/api", "expect": expect})
            assert state == "INDETERMINADO" and "cannot conclude" in error
            assert not delivery._NETWORK_ERROR_RX.search(error)
