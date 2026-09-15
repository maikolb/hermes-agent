"""Aceite do cliente: endosso registrado, nunca inferido.

O Codex apontou e a medição confirmou: a Fase 2 provou comunicação, não
aceite. Nos últimos 8 dias, de 18 fechamentos, 12 não tiveram mensagem
nenhuma do cliente nas 24h seguintes, e das 6 que tiveram, nenhuma era
aceite: uma dizia "a solução que foi pra produção não arrumou, parece que
piorou".

Regras que estes testes fixam:
  * só resposta DIRETA à devolutiva conta (reply_to_message_id casando com o
    message_id que publicamos); mesma thread e proximidade de horário não;
  * silêncio nunca vira aceite;
  * "obrigado", "vou testar", "recebido" não são aprovação do resultado;
  * recusa vence aceite quando as duas expressões aparecem;
  * o registro guarda quem confirmou e o texto original.
"""

from __future__ import annotations

import pytest

from hermes_cli import nfos_delivery as nd


# ---------------------------------------------------------------------------
# Classificação conservadora
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("texto", [
    "resolvido, obrigado",
    "Corrigido aqui, valeu",
    "funcionou certinho agora",
    "deu certo",
    "está certo agora",
    "perfeito",
])
def test_confirmacao_clara_e_aceite(texto):
    assert nd._classify_ack(texto) == "aceito"


@pytest.mark.parametrize("texto", [
    "não resolveu",
    "continua igual",
    "ainda aparece o erro",
    "piorou depois do deploy",
    "mesmo problema",
    "voltou a acontecer",
])
def test_reclamacao_e_recusa(texto):
    assert nd._classify_ack(texto) == "recusado"


@pytest.mark.parametrize("texto", [
    "obrigado",
    "vou testar",
    "recebido",
    "ok",
    "👍",
    "",
    "vou pedir pro cliente conferir",
])
def test_acusar_recebimento_nao_e_aprovacao(texto):
    """O Codex foi explícito: não aceitar recebido/obrigado/vou testar como aceite."""
    assert nd._classify_ack(texto) == "ambiguo"


def test_recusa_vence_aceite_na_mesma_frase():
    """'resolvido? não, continua igual' não pode virar aceite."""
    assert nd._classify_ack("resolvido? não, continua igual") == "recusado"
    assert nd._classify_ack("corrigido mas ainda aparece o erro") == "recusado"


# ---------------------------------------------------------------------------
# Vínculo: só resposta direta à devolutiva
# ---------------------------------------------------------------------------

class _Conn:
    """Conexão mínima que devolve o evento de devolutiva publicada."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=()):
        class _Cur:
            def __init__(self, row): self._row = row
            def fetchone(self): return self._row
        alvo = str(params[0]) if params else None
        for r in self._rows:
            if str(r["json"]) == alvo:
                return _Cur(r)
        return _Cur(None)


def _linha(message_id, chat="-100123", thread="41", task="t_abc"):
    import json as _j
    return {"json": message_id, "task_id": task,
            "payload": _j.dumps({"message_id": message_id, "chat_id": chat,
                                 "thread_id": thread, "outcome": "entregue", "ack": "awaiting"})}


def test_resposta_direta_a_devolutiva_resolve_o_card():
    conn = _Conn([_linha("18083")])
    source = {"chat_id": "-100123", "thread_id": "41"}
    task_id, pub = nd._delivery_awaiting_ack(conn, source, "18083")
    assert task_id == "t_abc"
    assert pub["outcome"] == "entregue"


def test_sem_reply_nao_resolve_nada():
    """Silêncio e mensagem solta na thread não viram aceite."""
    conn = _Conn([_linha("18083")])
    source = {"chat_id": "-100123", "thread_id": "41"}
    assert nd._delivery_awaiting_ack(conn, source, None) == (None, None)
    assert nd._delivery_awaiting_ack(conn, source, "") == (None, None)


def test_reply_a_outra_mensagem_nao_resolve():
    conn = _Conn([_linha("18083")])
    source = {"chat_id": "-100123", "thread_id": "41"}
    assert nd._delivery_awaiting_ack(conn, source, "99999") == (None, None)


def test_mesma_id_em_outra_conversa_nao_conta():
    """message_id se repete entre chats; a conversa tem que bater."""
    conn = _Conn([_linha("18083", chat="-100123", thread="41")])
    outra = {"chat_id": "-100999", "thread_id": "41"}
    assert nd._delivery_awaiting_ack(conn, outra, "18083") == (None, None)
    outra_thread = {"chat_id": "-100123", "thread_id": "4"}
    assert nd._delivery_awaiting_ack(conn, outra_thread, "18083") == (None, None)


# ---------------------------------------------------------------------------
# O conserto está no código, e no lugar certo
# ---------------------------------------------------------------------------

def test_o_intake_reconhece_o_aceite_antes_de_virar_pedido():
    import inspect
    fonte = inspect.getsource(nd.receive_request)
    assert "_client_ack_intake" in fonte
    assert fonte.index("_client_ack_intake") < fonte.index("_urgent_intake(conn, request_id"), (
        "o aceite tem que ser reconhecido antes do intake tratar a mensagem como pedido"
    )


def test_o_ack_nunca_derruba_o_intake():
    """Uma falha ao registrar aceite não pode perder o pedido do cliente."""
    import inspect
    fonte = inspect.getsource(nd._client_ack_intake)
    assert "except Exception" in fonte and "return None" in fonte


def test_o_registro_guarda_quem_confirmou_e_o_texto():
    import inspect
    fonte = inspect.getsource(nd._client_ack_intake)
    for campo in ("'ack'", "'author'", "'text'", "'reply_to'"):
        assert campo in fonte, campo


def test_a_devolutiva_pede_confirmacao_uma_vez_e_so_quando_ha_o_que_conferir(monkeypatch):
    from gateway import kanban_watchers as kw
    monkeypatch.setattr(kw, "_progress_outcome", lambda b, t: "entregue")
    msg = kw._client_delivery_message("b", "t_x")
    assert "Responda a esta mensagem" in msg
    assert msg.count("Responda a esta mensagem") == 1

    monkeypatch.setattr(kw, "_progress_outcome", lambda b, t: "encerrado")
    assert "Responda a esta mensagem" not in kw._client_delivery_message("b", "t_x"), (
        "card cancelado pelo dono não tem o que o cliente confirmar"
    )


def test_a_devolutiva_registra_o_message_id_para_permitir_vinculo():
    import inspect
    from gateway import kanban_watchers as kw
    assert hasattr(kw, "_record_client_delivery")
    fonte = inspect.getsource(kw._record_client_delivery)
    assert "message_id" in fonte
    assert "nfos_client_delivery_published" in fonte
    assert "awaiting" in fonte


def test_devolutiva_sem_message_id_avisa_em_vez_de_fingir(monkeypatch):
    """Se o adapter não devolveu id, o aceite não pode ser vinculado, e isso tem que aparecer."""
    import inspect
    from gateway import kanban_watchers as kw
    fonte = inspect.getsource(kw._record_client_delivery)
    assert "logger.warning" in fonte
    assert "não poderá ser vinculado" in fonte
