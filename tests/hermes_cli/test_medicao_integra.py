"""Integridade da medição: quem executa não afrouxa sozinho a régua do próprio sucesso.

Origem, medida em 15/09 no card `t_f2eb188d`, pedido real do cliente:

    12:57:48Z  nfos_completion_refused: pr22-integrated, pr23-integrated,
               ci-green, test-readback sem medição PASS
    13:01:45Z  spec revisão 3 salva
    13:01:54Z  as quatro sondas PASS
    13:02:59Z  card fechado

A revisão 3 manteve os quatro ids e os quatro `mandatory=true`, e reescreveu as
quatro sondas para a MESMA url, o ref advertisement do git. `ci-green` parou de
medir CI e `test-readback` virou tautologia. O precheck já registrava "TEST and
HML were unreachable".

REVISÃO 2 DESTES TESTES. A revisão 1 da regra foi submetida ao Codex depois de
já estar em produção, e ele reproduziu 16 casos numa cópia isolada: 11 bypasses
aceitos e 4 muros. Cada caso dele virou um teste aqui, com o nome do bypass no
teste, porque regra de integridade sem contraexemplo é só boa intenção.

O que mudou, e por quê:

    COLAPSO REMOVIDO. Recusar quando todos os obrigatórios observam um alvo só
    era insustentável nos dois sentidos: passava com três tautologias mais uma
    sonda real (bastavam dois alvos distintos) e recusava dois requisitos
    legítimos na mesma página HTML. Pior, recusava ANTES de qualquer decisão,
    então nem o Principal conseguia autorizar. Mesma url não prova redundância.

    ALVO COMPLETO. `_probe_target` deixou de fazer `split("?")[0]`, de ignorar o
    `expect`, de baixar a caixa da query e de cortar em 160 caracteres. Cada uma
    dessas reduções era um caminho para mudar a medição sem a regra ver, porque
    a comparação começa em `if antes == depois: continue`.

    RELOCAÇÃO COM ESCOPO. Preservar caminho e `expect` permitia trocar o CI real
    por uma fixture. Agora só vale se o host novo for o destino de entrega.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import nfos_delivery as nd


# ---------------------------------------------------------------------------
# Dublês: a regra é de spec. O banco entra só onde ela realmente consulta.
# ---------------------------------------------------------------------------

class _Resultado:
    def __init__(self, linha):
        self._linha = linha

    def fetchone(self):
        return self._linha


class _Conn:
    """Responde decisões (id, status, spec_revision) e contagem de recusas."""

    def __init__(self, decisoes=None, recusas=0):
        # decisoes: {id: (status, spec_revision)}
        self.decisoes = dict(decisoes or {})
        self.recusas = recusas

    def execute(self, sql, params=()):
        if "nfos_decisions" in sql:
            d = self.decisoes.get(params[0])
            return _Resultado((d[0], d[1]) if d else None)
        if "nfos_completion_refused" in sql:
            return _Resultado((self.recusas,))
        raise AssertionError("consulta inesperada: %s" % sql)


@pytest.fixture(autouse=True)
def _isola(monkeypatch):
    """A revisão da spec e o escopo do host vêm do banco em produção.

    Aqui eles são fixados, senão o teste mediria `get_workflow` em vez da regra.
    `_em_escopo` é sobrescrito por teste quando o caso exige.
    """
    monkeypatch.setattr(nd, "get_workflow", lambda conn, tid: {"spec_revision": 2})
    monkeypatch.setattr(nd, "_probe_in_scope", lambda conn, tid, host, spec=None: host == "destino.exemplo")
    monkeypatch.setattr(nd, "_host_of", lambda url: (str(url).split("//")[-1].split("/")[0] or None) if url else None)


def _anterior(spec):
    return {"content": json.dumps(spec)}


def _http(url, expect, json_path=None):
    p = {"kind": "http", "url": url, "expect": expect}
    if json_path:
        p["json_path"] = json_path
    return p


def _spec(criterios, goal="entregar o pedido", correcoes=None):
    s = {"goal": goal, "criteria": criterios}
    if correcoes is not None:
        s["probe_corrections"] = correcoes
    return s


def _dois_obrigatorios():
    return _spec([
        {"id": "ci", "mandatory": True,
         "probe": _http("https://ci.exemplo/status?sha=solicitado", {"equals": "success"}, "estado")},
        {"id": "test", "mandatory": True,
         "probe": _http("https://destino.exemplo/version", {"equals": "novo"}, "versao")},
    ])


def _corr(cid="ci", decision="dec_ok"):
    return [{"id": cid, "reason": "sonda corrigida", "evidence": ["e.json"], "decision": decision}]


DEC_OK = {"dec_ok": ("resolved", 2)}


# ---------------------------------------------------------------------------
# Os 11 bypasses que o Codex aceitou contra a revisão 1
# ---------------------------------------------------------------------------

def test_bypass01_trocar_a_query_da_url_escolhe_outro_commit():
    """`?sha=solicitado` virando `?sha=antigo-verde` mede outro commit.

    A revisão 1 fazia `split("?")[0]`, então os dois alvos ficavam iguais e a
    verificação nem chegava a rodar.
    """
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["url"] = "https://ci.exemplo/status?sha=antigo-verde"
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), depois)


def test_bypass02_trocar_a_fonte_de_verdade_por_uma_fixture():
    """Mesmo caminho, mesmo expect, outro host: o CI real vira fixture."""
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["url"] = "https://fixture.exemplo/status?sha=solicitado"
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), depois)


def test_bypass03_afrouxar_so_o_expect_no_mesmo_alvo():
    """`equals: novo` virando `contains_all: ["o"]` faz "velho" passar."""
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][1]["probe"]["expect"] = {"contains_all": ["o"]}
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), depois)


def test_bypass04_decisao_antiga_e_sem_relacao_nao_autoriza():
    """Uma decisão de "qual horário?" de uma revisão anterior não vale."""
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["expect"] = {"contains_all": ["s"]}
    depois["probe_corrections"] = _corr("ci", "dec_horario")
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(decisoes={"dec_horario": ("resolved", 1)}),
                                        "t_x", _anterior(antes), depois)


def test_bypass09_literal_sql_sensivel_a_caixa():
    """`.lower()` na revisão 1 apagava a diferença entre dois literais."""
    antes = _spec([{"id": "C1", "mandatory": True,
                    "probe": {"kind": "sql", "query": "select ok from t where nome = 'Alpha'",
                              "expect": {"scalar": 1}}}])
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["query"] = "select ok from t where nome = 'ALPHA'"
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), depois)


def test_bypass10_filtro_sql_depois_do_caractere_160():
    """O corte em `[:160]` apagava um filtro que seleciona outra execução."""
    base = "select resultado from execucoes where " + ("comentario_muito_longo = 'x' and " * 6)
    antes = _spec([{"id": "C1", "mandatory": True,
                    "probe": {"kind": "sql", "query": base + "run_id = 999", "expect": {"scalar": 1}}}])
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["query"] = base + "run_id = 1"
    assert len(base) > 160, "o teste precisa que a diferenca fique depois do corte antigo"
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), depois)


def test_bypass14_decisao_citada_em_criterio_inexistente():
    """A correção apontava para um id que não é de nenhum critério."""
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["expect"] = {"contains_all": ["s"]}
    depois["probe_corrections"] = _corr("criterio-que-nao-existe", "dec_ok")
    with pytest.raises(nd.WorkflowError):
        nd._check_measurement_integrity(_Conn(decisoes=DEC_OK), "t_x", _anterior(antes), depois)


def test_o_caso_real_t_f2eb188d_seria_recusado():
    """As quatro sondas trocando a API do GitHub pelo ref advertisement."""
    refs = "https://github.com/O/R.git/info/refs?service=git-upload-pack"
    antes = _spec([
        {"id": "pr22-integrated", "mandatory": True,
         "probe": _http("https://api.github.com/repos/O/R/pulls/22", {"equals": True}, "merged")},
        {"id": "ci-green", "mandatory": True,
         "probe": _http("https://api.github.com/repos/O/R/commits/main/check-runs",
                        {"equals": "success"}, "check_runs.0.conclusion")},
    ])
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"] = _http(refs, {"contains_all": ["a442f98"]})
    depois["criteria"][1]["probe"] = _http(refs, {"contains_all": ["8f39bd2"]})
    with pytest.raises(nd.WorkflowError) as erro:
        nd._check_measurement_integrity(_Conn(), "t_f2eb188d", _anterior(antes), depois)
    assert "measurement changed" in str(erro.value)


# ---------------------------------------------------------------------------
# Os 4 muros que o Codex reproduziu: a regra não pode barrar spec legítima
# ---------------------------------------------------------------------------

def test_muro06_duas_buscas_independentes_na_mesma_rota():
    """`?q=alpha` e `?q=beta` são observações diferentes, não redundância."""
    spec = _spec([
        {"id": "C1", "mandatory": True, "probe": _http("https://app.exemplo/search?q=alpha",
                                                       {"contains_all": ["alpha"]})},
        {"id": "C2", "mandatory": True, "probe": _http("https://app.exemplo/search?q=beta",
                                                       {"contains_all": ["beta"]})},
    ])
    nd._check_measurement_integrity(_Conn(), "t_x", _anterior(spec), json.loads(json.dumps(spec)))


def test_muro07_dois_requisitos_no_mesmo_html_sem_json_path():
    """Uma página pode comprovar o saldo E o botão de exportação.

    A premissa da revisão 1 era que `json_path` separava os casos, o que vale
    para JSON e não vale para HTML nem texto.
    """
    spec = _spec([
        {"id": "saldo", "mandatory": True, "probe": _http("https://app.exemplo/conta",
                                                          {"contains_all": ["Saldo:"]})},
        {"id": "exportar", "mandatory": True, "probe": _http("https://app.exemplo/conta",
                                                             {"contains_all": ["Exportar"]})},
    ])
    nd._check_measurement_integrity(_Conn(), "t_x", _anterior(spec), json.loads(json.dumps(spec)))


def test_muro12_pontuacao_no_goal_depois_de_recusa():
    """Acrescentar um ponto final não pode exigir decisão do Principal."""
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["goal"] = antes["goal"] + "."
    nd._check_measurement_integrity(_Conn(recusas=3), "t_x", _anterior(antes), depois)


def test_relocacao_para_o_destino_passa_sem_decisao():
    """O caso real de `t_b464e591`: o EC2 de TEST do cliente trocou de IP."""
    antes = _spec([{"id": "AC-01", "mandatory": True,
                    "probe": _http("https://antigo.exemplo/api/auth/profile-summary",
                                   {"equals": "pacote_ativo"}, "plano_atual.tipo")}])
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["url"] = "https://destino.exemplo/api/auth/profile-summary"
    nd._check_measurement_integrity(_Conn(), "t_b464e591", _anterior(antes), depois)


# ---------------------------------------------------------------------------
# Controles
# ---------------------------------------------------------------------------

def test_controle13_goal_alterado_sem_decisao_depois_de_recusa():
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["goal"] = "entregar so o que der"
    with pytest.raises(nd.WorkflowError) as erro:
        nd._check_measurement_integrity(_Conn(recusas=1), "t_x", _anterior(antes), depois)
    assert "goal changed" in str(erro.value)


def test_goal_alterado_sem_recusa_previa_passa():
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["goal"] = "entregar o pedido com readback"
    nd._check_measurement_integrity(_Conn(recusas=0), "t_x", _anterior(antes), depois)


def test_troca_com_decisao_valida_da_revisao_certa_passa():
    antes = _dois_obrigatorios()
    depois = json.loads(json.dumps(antes))
    depois["criteria"][0]["probe"]["expect"] = {"contains_all": ["success"]}
    depois["probe_corrections"] = _corr("ci", "dec_ok")
    nd._check_measurement_integrity(_Conn(decisoes=DEC_OK), "t_x", _anterior(antes), depois)


def test_sonda_identica_nao_exige_nada():
    antes = _dois_obrigatorios()
    nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), json.loads(json.dumps(antes)))


def test_primeira_revisao_nao_tem_com_que_comparar():
    nd._check_measurement_integrity(_Conn(), "t_x", None, _dois_obrigatorios())


def test_anterior_ilegivel_nao_quebra():
    """Artefato corrompido não pode virar recusa: seria bloquear por defeito meu."""
    nd._check_measurement_integrity(_Conn(), "t_x", {"content": "nao e json"}, _dois_obrigatorios())


def test_criterio_optional_e_phase_before_ficam_de_fora():
    antes = _spec([
        {"id": "C1", "mandatory": True, "probe": _http("https://a.exemplo/x", {"equals": 1})},
        {"id": "C2", "optional": True, "optional_reason": "coberto por C1",
         "probe": _http("https://a.exemplo/y", {"equals": 1})},
    ])
    depois = json.loads(json.dumps(antes))
    depois["criteria"][1]["probe"]["url"] = "https://outro.exemplo/z"
    nd._check_measurement_integrity(_Conn(), "t_x", _anterior(antes), depois)
