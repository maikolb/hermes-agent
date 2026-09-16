"""Fase 2: o que separa CARD FECHADO de CORREÇÃO ENTREGUE E ACEITA.

Dois defeitos medidos em 15/09 contra o estado real de produção:

D1. Dez de dezenove cards tinham critérios obrigatórios distintos medidos pela
    MESMA requisição. No `t_ff7b8ccc` os cinco critérios compartilhavam URL e
    expect idênticos, incluindo um que exigia testes unitários verdes e era
    medido por um HTTP procurando o nome de um cargo.

D2. `completed` estava em `_CLIENT_SILENT_KINDS`, então todo card fechado
    emitia `client_publication_suppressed` e o cliente nunca era avisado.
    Nenhuma entrega teve endosso porque ninguém foi informado.
"""

from __future__ import annotations

import pytest

from hermes_cli import nfos_delivery as nd


# ---------------------------------------------------------------------------
# D1: um requisito obrigatório, uma medição própria
# ---------------------------------------------------------------------------

def _probe(url="https://admin.exemplo.com/api/x", expect=None, **extra):
    p = {"kind": "http", "url": url, "expect": expect or {"contains_all": ["Portugues"], "status": 200}}
    p.update(extra)
    return p


def test_dois_criterios_obrigatorios_com_a_mesma_sonda_sao_recusados():
    """O caso t_ff7b8ccc: cinco critérios, uma medição."""
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "text": "o pipeline nao publica snapshot vazio", "probe": _probe()},
        {"id": "C2", "mandatory": True, "text": "a extracao mantem os cargos do PDF", "probe": _probe()},
    ]}
    with pytest.raises(nd.WorkflowError) as erro:
        nd._check_distinct_measurements(spec)
    assert "C1" in str(erro.value) and "C2" in str(erro.value)
    assert "SAME probe" in str(erro.value)


def test_criterio_sobre_testes_nao_passa_por_sonda_http_de_conteudo():
    """O C4 do t_ff7b8ccc exigia suíte verde e era medido por uma string HTTP."""
    spec = {"criteria": [
        {"id": "C3", "mandatory": True, "text": "disciplinas preservam o vinculo", "probe": _probe()},
        {"id": "C4", "mandatory": True, "text": "teste unitario relevante e suite existente verde", "probe": _probe()},
    ]}
    with pytest.raises(nd.WorkflowError):
        nd._check_distinct_measurements(spec)


def test_sondas_distintas_passam():
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _probe(url="https://admin.exemplo.com/api/upload")},
        {"id": "C2", "mandatory": True, "probe": _probe(url="https://app.exemplo.com/api/aluno")},
    ]}
    nd._check_distinct_measurements(spec)


def test_mesma_url_com_expectativas_diferentes_passa():
    """Ler a mesma rota é legítimo; provar coisas diferentes nela também."""
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _probe(expect={"contains_all": ["Portugues"], "status": 200})},
        {"id": "C2", "mandatory": True, "probe": _probe(expect={"not_matches": "Matematica|Historia", "status": 200})},
    ]}
    nd._check_distinct_measurements(spec)


def test_criterio_optional_nao_colide():
    """A saída legítima para um requisito coberto pela mesma leitura."""
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _probe()},
        {"id": "C2", "optional": True, "optional_reason": "coberto pela leitura do C1", "probe": _probe()},
    ]}
    nd._check_distinct_measurements(spec)


def test_sonda_de_linha_de_base_nao_colide():
    """phase=before registra o estado anterior, não sustenta aceite."""
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _probe()},
        {"id": "C0", "mandatory": False, "probe": _probe(phase="before")},
    ]}
    nd._check_distinct_measurements(spec)


def test_criterio_sem_sonda_nao_quebra_a_checagem():
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _probe()},
        {"id": "C2", "mandatory": True},
    ]}
    nd._check_distinct_measurements(spec)


def test_a_checagem_roda_dentro_da_validacao_da_spec():
    """A regra tem que barrar no save_spec, não só quando alguém chama o helper."""
    import inspect
    fonte = inspect.getsource(nd._spec_result_criteria_checks)
    assert "_check_distinct_measurements" in fonte


# ---------------------------------------------------------------------------
# D2: a conclusão chega ao cliente
# ---------------------------------------------------------------------------

def test_conclusao_nao_e_mais_silenciosa_no_chat_do_cliente():
    from gateway import kanban_watchers as kw
    assert "completed" not in kw._CLIENT_SILENT_KINDS, (
        "completed em _CLIENT_SILENT_KINDS faz todo card fechar sem avisar o cliente"
    )
    assert "completed" in kw._CLIENT_DELIVERY_KINDS


def test_o_ruido_continua_silencioso():
    """O contrato de 13/09 continua valendo para tudo que não é o resultado."""
    from gateway import kanban_watchers as kw
    for ruido in ("claimed", "status", "blocked", "crashed", "timed_out",
                  "model_fallback", "block_loop_detected", "review_requested"):
        assert ruido in kw._CLIENT_SILENT_KINDS, ruido


def test_devolutiva_nao_afirma_entrega_que_o_relatorio_nao_sustenta(monkeypatch):
    from gateway import kanban_watchers as kw
    monkeypatch.setattr(kw, "_progress_outcome", lambda board, task_id: "parcial")
    monkeypatch.setattr(kw, "logger", kw.logger)
    msg = kw._client_delivery_message("b", "t_x")
    assert msg.startswith("Resolvido em parte")
    assert "Resolvido:" not in msg


def test_devolutiva_de_entrega_completa(monkeypatch):
    from gateway import kanban_watchers as kw
    monkeypatch.setattr(kw, "_progress_outcome", lambda board, task_id: "entregue")
    msg = kw._client_delivery_message("b", "t_x")
    assert msg.startswith("Resolvido")


def test_devolutiva_de_cancelamento_nao_diz_resolvido(monkeypatch):
    from gateway import kanban_watchers as kw
    monkeypatch.setattr(kw, "_progress_outcome", lambda board, task_id: "encerrado")
    msg = kw._client_delivery_message("b", "t_x")
    assert msg.startswith("Encerrado")
    assert "Resolvido" not in msg


def test_devolutiva_sem_relatorio_legivel_e_neutra(monkeypatch):
    from gateway import kanban_watchers as kw
    monkeypatch.setattr(kw, "_progress_outcome", lambda board, task_id: "concluído")
    msg = kw._client_delivery_message("b", "t_x")
    assert msg.startswith("Concluído")


def test_devolutiva_nunca_quebra_o_notificador(monkeypatch):
    """Board inexistente não pode impedir o aviso: degrada para o veredito seco."""
    from gateway import kanban_watchers as kw

    monkeypatch.setattr(kw, "_progress_outcome", lambda board, task_id: "entregue")
    msg = kw._client_delivery_message("board-que-nao-existe", "t_inexistente")
    assert isinstance(msg, str) and msg.startswith("Resolvido")


def test_devolutiva_nao_vaza_detalhe_interno(monkeypatch):
    """O cliente lê o resultado do pedido dele, não spec, sonda, run nem worker."""
    from gateway import kanban_watchers as kw

    monkeypatch.setattr(kw, "_progress_outcome", lambda board, task_id: "entregue")
    msg = kw._client_delivery_message("board-que-nao-existe", "t_inexistente").lower()
    for interno in ("spec", "probe", "sonda", "run_id", "worker", "kanban_complete", "criterio c1"):
        assert interno not in msg, interno
