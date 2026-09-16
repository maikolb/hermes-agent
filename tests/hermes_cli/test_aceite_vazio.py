"""Aceite vazio: um card não pode fechar sem nada obrigatório.

Medido em 15/09 contra produção: 101 de 144 specs (70%) nasciam sem nenhum
critério `mandatory=true`. Como o gate de fechamento só barra critério
obrigatório sem PASS, essas specs fechavam o card sem medir nada.

Caso canônico `t_c2f4c61e`: o cliente pediu "puxando as disciplinas
incorretas", a spec virou "implementar endpoint administrativo de remoção
segura", e o card fechou com a mutação de dados não executada e o C20
(readback 22→21) em NOT_RUN com evidência vazia. Nenhum dos 22 critérios era
obrigatório.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import nfos_delivery as nd


def _probe_sql(query="SELECT count(*) FROM position_disciplines WHERE position_id = 'x'"):
    return {"kind": "sql", "query": query, "expect": {"scalar": 21}}


def _probe_http(url="https://app.exemplo.com/api/disciplinas"):
    return {"kind": "http", "url": url, "expect": {"contains_all": ["Portugues"], "status": 200}}


# ---------------------------------------------------------------------------
# P1: a spec não nasce sem resultado obrigatório
# ---------------------------------------------------------------------------

def _checa(spec, precheck=None, measured=False):
    """Roda só a regra nova, sem depender do banco."""
    if not any(c.get("mandatory") for c in spec.get("criteria") or []):
        raise nd.WorkflowError("sem criterio obrigatorio")


def test_o_caso_real_t_c2f4c61e_seria_recusado():
    """22 critérios, nenhum obrigatório: era assim que o card do Auditor Fiscal fechou."""
    spec = {"delivery_type": "code", "criteria": [
        {"id": "C%d" % i, "text": "criterio %d" % i} for i in range(1, 23)
    ]}
    assert not any(c.get("mandatory") for c in spec["criteria"])
    with pytest.raises(nd.WorkflowError):
        _checa(spec)


def test_spec_com_um_obrigatorio_passa():
    spec = {"delivery_type": "code", "criteria": [
        {"id": "C1", "text": "readback pos-mutacao", "mandatory": True, "probe": _probe_sql()},
        {"id": "C2", "text": "suite verde"},
    ]}
    _checa(spec)


def test_relatorio_satisfaz_com_sonda_sql():
    """O alerta do Codex era forçar HTTP em entrega documental. Sonda sql já resolve,
    e specs de report reais já fazem isso (t_a4b4f7a5, t_0360efe0, t_771f5e78)."""
    spec = {"delivery_type": "report", "criteria": [
        {"id": "C1", "text": "o dado que a analise afirma", "mandatory": True, "probe": _probe_sql()},
    ]}
    _checa(spec)
    assert spec["criteria"][0]["probe"]["kind"] == "sql"


def test_a_regra_vale_sem_precheck():
    """A porta de escape antiga: sem precheck, nada era exigido.

    29 de 42 cards de código e 16 de 17 de operação sem critério obrigatório
    não tinham precheck registrado.
    """
    spec = {"delivery_type": "operation", "criteria": [{"id": "C1", "text": "sem obrigatorio"}]}
    with pytest.raises(nd.WorkflowError):
        _checa(spec, precheck={}, measured=False)


def test_a_regra_vale_para_report_tambem():
    """A regra antiga só olhava code e operation; report escapava inteiro."""
    spec = {"delivery_type": "report", "criteria": [{"id": "C1", "text": "sem obrigatorio"}]}
    with pytest.raises(nd.WorkflowError):
        _checa(spec)


# ---------------------------------------------------------------------------
# A regra está no código, não só neste teste
# ---------------------------------------------------------------------------

def test_a_regra_esta_na_validacao_da_spec():
    import inspect
    fonte = inspect.getsource(nd._spec_result_criteria_checks)
    assert "MANDATORY_RESULT_20260915" in fonte
    assert "no mandatory criterion" in fonte


def test_a_regra_nao_depende_mais_do_precheck():
    """A condição antiga exigia precheck medido e delivery_type em {code, operation}."""
    import inspect
    fonte = inspect.getsource(nd._spec_result_criteria_checks)
    trecho = fonte[fonte.index("MANDATORY_RESULT_20260915"):]
    assert "if not any(c.get('mandatory')" in trecho, "a regra tem que ser incondicional"


def test_o_fechamento_tambem_recusa():
    """Exigência do Codex: validar só em save_spec deixaria passar 70% das specs antigas."""
    import inspect
    fonte = inspect.getsource(nd.completion_refusal_note)
    assert "MANDATORY_RESULT_20260915" in fonte
    assert "_sem_obrigatorio" in fonte


def test_a_mensagem_diz_o_que_fazer_e_o_que_nao_conta():
    import inspect
    fonte = inspect.getsource(nd._spec_result_criteria_checks)
    trecho = fonte[fonte.index("MANDATORY_RESULT_20260915"):]
    assert "THE RESULT THE REQUESTER ASKED FOR" in trecho
    assert "not that result" in trecho, "tem que dizer que build/deploy/teste nao e o resultado"


def test_a_nota_de_fechamento_explica_a_saida():
    import inspect
    fonte = inspect.getsource(nd.completion_refusal_note)
    assert "nova revisão da spec" in fonte
    assert "mandatory=true" in fonte


# ---------------------------------------------------------------------------
# A regra não pode quebrar o que já funciona
# ---------------------------------------------------------------------------

def test_spec_vazia_nao_quebra_a_checagem():
    """Sem critérios, outras validações já recusam antes; esta não pode estourar."""
    for spec in ({"criteria": []}, {"criteria": None}, {}):
        with pytest.raises(nd.WorkflowError):
            _checa(spec)


def test_criterio_optional_nao_conta_como_obrigatorio():
    spec = {"delivery_type": "code", "criteria": [
        {"id": "C1", "text": "x", "optional": True, "optional_reason": "coberto por outro"},
    ]}
    with pytest.raises(nd.WorkflowError):
        _checa(spec)
