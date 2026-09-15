"""Integridade da medição: quem executa não afrouxa sozinho a régua do próprio sucesso.

Medido em 15/09 contra produção, no card `t_f2eb188d`, pedido real do cliente
("mescla os PRs pendentes la no github resolvendo os conflitos").

    12:57:48Z  nfos_completion_refused, pendentes: pr22-integrated,
               pr23-integrated, ci-green, test-readback
    13:01:45Z  spec revisão 3 salva
    13:01:54Z  as quatro sondas PASS
    13:02:59Z  card fechado

A revisão 3 manteve os quatro ids e os quatro `mandatory=true`, e reescreveu as
quatro sondas para a MESMA url, o ref advertisement do git. `ci-green` parou de
medir CI e `test-readback` virou tautologia: qualquer repositório com branch
main satisfaz `contains_all ["refs/heads/main"]`. O precheck já registrava
"TEST and HML were unreachable". O goal também mudou, de "publicar a revisão
integrada no ambiente TEST" para "o TEST é apenas conferência read-only
complementar".

Os guardas que existiam não pegaram: `_probe_signature` inclui o expect, então
quatro sondas na mesma url com expects diferentes contam como distintas, e
`_check_probe_corrections` compara o formato da expectativa por `_expect_rank`
(`contains_all` e `equals` valem 3 os dois) e nunca compara o alvo.

Varredura nos 31 boards: 6 cards fecharam com todos os obrigatórios observando
um alvo só, e 16 critérios em 7 cards mudaram o que medem depois de uma recusa.

O contraponto que os testes têm que proteger vem de `t_b464e591`: ali a sonda
mudou de `15.229.26.202` para `15.228.49.209` porque o EC2 de TEST do cliente
trocou de IP, com mesmo caminho, json_path e expect. Isso é conserto de
instrumentação, e barrar isso seria trocar falso sucesso por muro.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import nfos_delivery as nd


# ---------------------------------------------------------------------------
# Dublês: a regra é de spec, não precisa de banco real.
# ---------------------------------------------------------------------------

class _Resultado:
    def __init__(self, linha):
        self._linha = linha

    def fetchone(self):
        return self._linha


class _Conn:
    """Responde só o que a regra consulta: decisões resolvidas e nº de recusas."""

    def __init__(self, decisoes=(), recusas=0):
        self.decisoes = set(decisoes)
        self.recusas = recusas

    def execute(self, sql, params=()):
        if "nfos_decisions" in sql:
            return _Resultado((1,) if params[0] in self.decisoes else None)
        if "nfos_completion_refused" in sql:
            return _Resultado((self.recusas,))
        raise AssertionError("consulta inesperada: %s" % sql)


def _spec_anterior(spec):
    """`previous` chega como linha de artefato, com o JSON em `content`."""
    return {"content": json.dumps(spec)}


REFS = "https://github.com/Project-Factory-26/infotributos.git/info/refs?service=git-upload-pack"


def _refs(esperado):
    return {"kind": "http", "url": REFS, "expect": {"contains_all": [esperado]}}


# ---------------------------------------------------------------------------
# Colapso: um fato não vira N indicadores verdes
# ---------------------------------------------------------------------------

def test_o_caso_real_t_f2eb188d_seria_recusado():
    """As quatro sondas da revisão 3, exatamente como fecharam o card."""
    spec = {"goal": "integrar as PRs", "criteria": [
        {"id": "pr22-integrated", "mandatory": True, "probe": _refs("a442f98076dd42613c49beb2bc11404ddfa8b34f")},
        {"id": "pr23-integrated", "mandatory": True, "probe": _refs("610ecb32228332535242b71bc00cc3f7ad39f270")},
        {"id": "ci-green", "mandatory": True, "probe": _refs("8f39bd274a5453ca8d349c57fde21c1863ab1627")},
        {"id": "test-readback", "mandatory": True, "probe": _refs("refs/heads/main")},
    ]}
    with pytest.raises(nd.WorkflowError) as erro:
        nd._check_measurement_integrity(_Conn(), "t_f2eb188d", None, spec)
    assert "SAME thing" in str(erro.value)


def test_dois_criterios_na_mesma_resposta_com_json_path_diferente_passam():
    """Uma resposta carrega fatos independentes: isso é medição distinta, não colapso.

    Se esta regra proibisse compartilhar url, viraria muro. O json_path entra no
    alvo justamente para separar os dois casos.
    """
    url = "https://app.exemplo.com/api/pedido/42"
    spec = {"criteria": [
        {"id": "C1", "mandatory": True,
         "probe": {"kind": "http", "url": url, "json_path": "status", "expect": {"equals": "pago"}}},
        {"id": "C2", "mandatory": True,
         "probe": {"kind": "http", "url": url, "json_path": "nota_fiscal.numero", "expect": {"equals": "1234"}}},
    ]}
    nd._check_measurement_integrity(_Conn(), "t_x", None, spec)


def test_um_unico_obrigatorio_nao_dispara_colapso():
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _refs("abc")},
        {"id": "C2", "probe": _refs("abc")},
    ]}
    nd._check_measurement_integrity(_Conn(), "t_x", None, spec)


def test_optional_e_phase_before_ficam_de_fora_do_colapso():
    """Linha de base e critério dispensável não sustentam aceite, então não contam."""
    spec = {"criteria": [
        {"id": "C1", "mandatory": True, "probe": _refs("abc")},
        {"id": "C2", "optional": True, "optional_reason": "coberto por C1", "probe": _refs("abc")},
        {"id": "C3", "mandatory": True, "probe": dict(_refs("abc"), phase="before")},
    ]}
    nd._check_measurement_integrity(_Conn(), "t_x", None, spec)


# ---------------------------------------------------------------------------
# Troca de alvo: mudar o que se prova exige autorização, não justificativa
# ---------------------------------------------------------------------------

def _antes_do_t_f2eb188d():
    return {"goal": "integrar as PRs e publicar no TEST", "criteria": [
        {"id": "pr22-integrated", "mandatory": True,
         "probe": {"kind": "http", "url": "https://api.github.com/repos/O/R/pulls/22",
                   "json_path": "merged", "expect": {"equals": True, "status": 200}}},
        {"id": "outro", "mandatory": True,
         "probe": {"kind": "http", "url": "https://exemplo.com/health",
                   "json_path": "status", "expect": {"equals": "ok"}}},
    ]}


def test_troca_de_alvo_sem_decisao_e_recusada():
    antes = _antes_do_t_f2eb188d()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"] = _refs("a442f98")
    with pytest.raises(nd.WorkflowError) as erro:
        nd._check_measurement_integrity(_Conn(), "t_f2eb188d", _spec_anterior(antes), depois)
    assert "pr22-integrated" in str(erro.value)
    assert "decision" in str(erro.value)


def test_troca_de_alvo_com_decisao_resolvida_passa():
    antes = _antes_do_t_f2eb188d()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"] = _refs("a442f98")
    depois["probe_corrections"] = [
        {"id": "pr22-integrated", "reason": "a API exige token fora do escopo",
         "evidence": ["evidence.json"], "decision": "dec_1bfdb7da066543d89fe0"}]
    nd._check_measurement_integrity(_Conn(decisoes=["dec_1bfdb7da066543d89fe0"]), "t_f2eb188d",
                                    _spec_anterior(antes), depois)


def test_decisao_inexistente_nao_autoriza():
    """Citar um id que não existe é a forma mais barata de fabricar autorização."""
    antes = _antes_do_t_f2eb188d()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"] = _refs("a442f98")
    depois["probe_corrections"] = [
        {"id": "pr22-integrated", "reason": "corrigida", "evidence": ["e.json"], "decision": "dec_inventada"}]
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(decisoes=["dec_outra"]), "t_f2eb188d",
                                        _spec_anterior(antes), depois)


def test_relocacao_de_host_passa_sem_decisao():
    """O caso real de t_b464e591: o EC2 de TEST do cliente trocou de IP."""
    antes = {"criteria": [
        {"id": "AC-01", "mandatory": True,
         "probe": {"kind": "http", "url": "https://infotributos.15.229.26.202.nip.io/api/auth/profile-summary",
                   "json_path": "plano_atual.tipo", "expect": {"equals": "pacote_ativo"}}},
        {"id": "AC-02", "mandatory": True,
         "probe": {"kind": "http", "url": "https://exemplo.com/outro", "json_path": "x", "expect": {"equals": 1}}},
    ]}
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["url"] = \
        "https://infotributos.15.228.49.209.nip.io/api/auth/profile-summary"
    nd._check_measurement_integrity(_Conn(), "t_b464e591", _spec_anterior(antes), depois)


def test_sonda_identica_nao_exige_nada():
    antes = _antes_do_t_f2eb188d()
    nd._check_measurement_integrity(_Conn(), "t_x", _spec_anterior(antes), json.loads(json.dumps(antes)))


# ---------------------------------------------------------------------------
# Meta congelada depois de uma recusa
# ---------------------------------------------------------------------------

def test_goal_reescrito_depois_de_recusa_exige_decisao():
    antes = _antes_do_t_f2eb188d()
    depois = json.loads(json.dumps(antes))
    depois["goal"] = "integrar as PRs; o TEST e apenas conferencia read-only complementar"
    with pytest.raises(nd.WorkflowError) as erro:
        nd._check_measurement_integrity(_Conn(recusas=1), "t_f2eb188d", _spec_anterior(antes), depois)
    assert "goal changed" in str(erro.value)


def test_goal_reescrito_sem_recusa_previa_passa():
    """Refinar a meta antes de qualquer recusa é trabalho normal de spec."""
    antes = _antes_do_t_f2eb188d()
    depois = json.loads(json.dumps(antes))
    depois["goal"] = "integrar as PRs e publicar no TEST, com readback"
    nd._check_measurement_integrity(_Conn(recusas=0), "t_f2eb188d", _spec_anterior(antes), depois)


def test_goal_reescrito_com_decisao_resolvida_passa():
    antes = _antes_do_t_f2eb188d()
    depois = json.loads(json.dumps(antes))
    depois["goal"] = "integrar as PRs; o TEST saiu do escopo autorizado"
    depois["probe_corrections"] = [
        {"id": "pr22-integrated", "reason": "escopo revisto", "evidence": ["e.json"], "decision": "dec_ok"}]
    nd._check_measurement_integrity(_Conn(decisoes=["dec_ok"], recusas=3), "t_f2eb188d",
                                    _spec_anterior(antes), depois)


# ---------------------------------------------------------------------------
# A regra não pode quebrar spec sem sonda nem spec de primeira revisão
# ---------------------------------------------------------------------------

def test_spec_sem_sonda_nao_quebra():
    nd._check_measurement_integrity(_Conn(), "t_x", None, {"criteria": [{"id": "C1", "mandatory": True}]})


def test_spec_vazia_nao_quebra():
    nd._check_measurement_integrity(_Conn(), "t_x", None, {})


def test_anterior_ilegivel_nao_quebra():
    """Artefato corrompido não pode virar recusa: seria bloquear por defeito meu."""
    spec = {"criteria": [{"id": "C1", "mandatory": True, "probe": _refs("abc")}]}
    nd._check_measurement_integrity(_Conn(), "t_x", {"content": "nao e json"}, spec)
