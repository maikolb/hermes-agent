"""compression.model_protect_last_n: protected-tail size per model.

O worker deepseek (contexto de 1M) comprimia e voltava a 240 a 290 mil tokens,
porque o ``protect_last_n`` global (20) protege as ultimas 20 mensagens, que
carregam saidas grandes de ferramenta. O Principal precisa da cauda de 20. A
chave nova e um dicionario casado por substring do nome do modelo (a
correspondencia mais longa vence, como ``model_thresholds``) e o valor
substitui ``protect_last_n`` so para aquele modelo.

Cada teste abaixo e derrubado por uma mutacao concreta (listada no docstring).
"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.context_compressor import (
    ContextCompressor,
    resolve_model_protect_last_n,
)
from agent.context_engine import ContextEngine

LARGE_WINDOW = 1_000_000
OVERRIDES = {"deepseek": 8}


def _compressor(model: str, **kw) -> ContextCompressor:
    defaults = dict(
        model=model,
        quiet_mode=True,
        threshold_percent=0.50,
        protect_first_n=2,
        protect_last_n=20,
    )
    defaults.update(kw)
    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=LARGE_WINDOW,
    ):
        return ContextCompressor(**defaults)


# ---------------------------------------------------------------------------
# (a) deepseek resolve 8, gpt-6-astra continua com 20
# ---------------------------------------------------------------------------

class TestResolver:
    def test_override_applies_only_to_the_matching_model(self):
        """Mutacao: o resolver devolve sempre ``default`` (ignora o mapa), ou
        sempre o valor do mapa (ignora o casamento): um dos dois asserts cai."""
        assert resolve_model_protect_last_n("deepseek-v4-pro", OVERRIDES, 20) == 8
        assert resolve_model_protect_last_n("gpt-6-astra", OVERRIDES, 20) == 20

    def test_compressor_instance_uses_the_resolved_value(self):
        """Mutacao: o __init__ do compressor continua com ``self.protect_last_n
        = protect_last_n`` (sem resolver)."""
        deepseek = _compressor("deepseek-v4-pro", model_protect_last_n=OVERRIDES)
        astra = _compressor("gpt-6-astra", model_protect_last_n=OVERRIDES)
        assert deepseek.protect_last_n == 8
        assert astra.protect_last_n == 20

    # -----------------------------------------------------------------------
    # (b) sem a chave, todos ficam com 20
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("empty", [None, {}])
    def test_absent_or_empty_map_changes_nothing(self, empty):
        """Mutacao: a ausencia da chave vira um valor padrao diferente de 20
        (ex.: mapa vazio resolve para 0 ou 6)."""
        for model in ("deepseek-v4-pro", "gpt-6-astra", ""):
            assert resolve_model_protect_last_n(model, empty, 20) == 20
        for model in ("deepseek-v4-pro", "gpt-6-astra"):
            c = _compressor(model, model_protect_last_n=empty)
            assert c.protect_last_n == 20

    def test_compressor_default_construction_keeps_global(self):
        """Mutacao: o parametro novo passa a ter um default nao vazio."""
        c = _compressor("deepseek-v4-pro")
        assert c.protect_last_n == 20
        assert c.model_protect_last_n == {}

    # -----------------------------------------------------------------------
    # (c) a correspondencia mais longa vence
    # -----------------------------------------------------------------------

    def test_longest_matching_key_wins_regardless_of_dict_order(self):
        """Mutacao: trocar ``len(key) > len(best_key)`` por "primeira que
        casa" ou "ultima que casa" derruba um dos dois sentidos de ordem."""
        short_first = {"deepseek": 8, "deepseek-v4-pro": 5}
        long_first = {"deepseek-v4-pro": 5, "deepseek": 8}
        for mapping in (short_first, long_first):
            assert resolve_model_protect_last_n("deepseek-v4-pro", mapping, 20) == 5
            assert resolve_model_protect_last_n("deepseek-v4-flash", mapping, 20) == 8

    # -----------------------------------------------------------------------
    # (d) valor invalido (negativo ou texto) cai para o global
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize(
        "bad", [-1, -20, "oito", "8", None, 8.5, True, False, [8], {"n": 8}],
    )
    def test_invalid_value_falls_back_to_global(self, bad):
        """Mutacao: tirar a checagem ``value < 0`` (negativo vaza para o
        compressor), a de ``bool`` (True vira 1) ou a de ``int`` (texto/float
        passam), cada uma derruba os parametros correspondentes."""
        assert resolve_model_protect_last_n("deepseek-v4", {"deepseek": bad}, 20) == 20

    def test_invalid_value_on_the_longest_key_uses_global_not_shorter_key(self):
        """O valor invalido da chave mais longa cai para o global (20), nao
        para a chave mais curta valida. Mutacao: o resolver pula a chave
        invalida e usa o 8 da curta."""
        mapping = {"deepseek": 8, "deepseek-v4": -3}
        assert resolve_model_protect_last_n("deepseek-v4", mapping, 20) == 20

    def test_zero_is_a_valid_override(self):
        """Mutacao: ``value <= 0`` no lugar de ``value < 0`` joga o 0 valido
        para o global."""
        assert resolve_model_protect_last_n("deepseek-v4", {"deepseek": 0}, 20) == 0

    def test_invalid_value_never_reaches_the_compressor(self):
        """Mutacao: o compressor atribui o valor cru do mapa."""
        c = _compressor("deepseek-v4-pro", model_protect_last_n={"deepseek": -4})
        assert c.protect_last_n == 20


# ---------------------------------------------------------------------------
# (e) troca de modelo no meio da sessao (fallback / /model) recalcula
# ---------------------------------------------------------------------------

class TestModelSwitch:
    def test_update_model_recomputes_in_both_directions(self):
        """Mutacao: o update_model nao recalcula (fica no valor do modelo
        inicial) ou recalcula a partir do valor ja sobrescrito em vez do
        global cru (o retorno ao gpt-6-astra ficaria em 8)."""
        c = _compressor("gpt-6-astra", model_protect_last_n=OVERRIDES)
        assert c.protect_last_n == 20

        c.update_model(model="deepseek-v4-pro", context_length=LARGE_WINDOW)
        assert c.protect_last_n == 8

        # Fallback de volta: o 8 do deepseek nao pode vazar para o astra.
        c.update_model(model="gpt-6-astra", context_length=LARGE_WINDOW)
        assert c.protect_last_n == 20

    def test_update_model_from_override_model_to_other_override(self):
        """Mutacao: o update_model so resolve quando nao ha valor anterior."""
        c = _compressor(
            "deepseek-v4-pro",
            model_protect_last_n={"deepseek": 8, "kimi": 5},
        )
        assert c.protect_last_n == 8
        c.update_model(model="kimi-k3", context_length=LARGE_WINDOW)
        assert c.protect_last_n == 5

    def test_update_model_without_map_keeps_the_global(self):
        """Mutacao: o update_model zera ou troca o valor quando nao ha mapa."""
        c = _compressor("gpt-6-astra", protect_last_n=13)
        c.update_model(model="deepseek-v4-pro", context_length=LARGE_WINDOW)
        assert c.protect_last_n == 13

    def test_base_engine_update_model_resolves_for_plugin_engines(self):
        """Mutacao: o bloco novo do ``ContextEngine.update_model`` some, ou
        passa a sobrescrever ``protect_last_n`` mesmo sem mapa."""

        class _Engine(ContextEngine):
            @property
            def name(self):
                return "stub"

            def update_from_response(self, usage):
                pass

            def should_compress(self, prompt_tokens=None):
                return False

            def compress(self, messages, current_tokens=None, focus_topic=None):
                return messages

        engine = _Engine()
        engine.protect_last_n = 20
        engine.context_length = 0
        engine.model_protect_last_n = OVERRIDES

        engine.update_model(model="deepseek-v4-pro", context_length=LARGE_WINDOW)
        assert engine.protect_last_n == 8
        engine.update_model(model="gpt-6-astra", context_length=LARGE_WINDOW)
        assert engine.protect_last_n == 20

        # Sem mapa, o valor proprio do engine (6 por padrao) nao e tocado.
        bare = _Engine()
        bare.context_length = 0
        bare.update_model(model="deepseek-v4-pro", context_length=LARGE_WINDOW)
        assert bare.protect_last_n == 6


# ---------------------------------------------------------------------------
# O valor resolvido chega a quem le a cauda: prune proativo e compressao
# ---------------------------------------------------------------------------

def _assistant_call(cid):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "terminal", "arguments": '{"cmd":"ls"}'}}
        ],
    }


def _build_big(n_pairs, big_chars=9000):
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(n_pairs):
        cid = f"call_{i}"
        msgs.append(_assistant_call(cid))
        msgs.append(
            {"role": "tool", "tool_call_id": cid,
             "content": chr(65 + (i % 26)) * big_chars}
        )
    return msgs


class TestConsumers:
    def test_proactive_prune_protects_fewer_messages_for_the_override_model(self):
        """O prune proativo le ``self.protect_last_n``: com a cauda menor ele
        resume mais saidas de ferramenta. Mutacao: o compressor guarda o valor
        resolvido em outro atributo e o prune continua lendo o global (as duas
        contagens ficam iguais)."""
        common = dict(
            protect_last_n=12,
            proactive_prune_tokens=48_000,
            proactive_prune_min_result_chars=8_000,
            model_protect_last_n={"deepseek": 2},
        )
        msgs = _build_big(12)
        astra = _compressor("gpt-6-astra", **common)
        deepseek = _compressor("deepseek-v4-pro", **common)

        _, pruned_astra = astra.prune_tool_results_only(msgs, current_tokens=120_000)
        _, pruned_deepseek = deepseek.prune_tool_results_only(
            msgs, current_tokens=120_000,
        )
        assert pruned_astra > 0
        assert pruned_deepseek > pruned_astra

    def test_prune_follows_the_model_after_a_switch(self):
        """Mutacao: o recalculo do update_model nao chega ao prune."""
        msgs = _build_big(12)
        c = _compressor(
            "gpt-6-astra",
            protect_last_n=12,
            proactive_prune_tokens=48_000,
            proactive_prune_min_result_chars=8_000,
            model_protect_last_n={"deepseek": 2},
        )
        c.update_model(model="deepseek-v4-pro", context_length=LARGE_WINDOW)
        fresh = _compressor(
            "deepseek-v4-pro",
            protect_last_n=12,
            proactive_prune_tokens=48_000,
            proactive_prune_min_result_chars=8_000,
            model_protect_last_n={"deepseek": 2},
        )
        _, switched = c.prune_tool_results_only(msgs, current_tokens=120_000)
        _, direct = fresh.prune_tool_results_only(msgs, current_tokens=120_000)
        assert switched == direct > 0

    def test_preflight_gate_and_switch_guard_read_the_resolved_value(self):
        """``turn_context`` e ``context_switch_guard`` leem o atributo do
        compressor; com o override eles enxergam 8, nao 20."""
        from hermes_cli.context_switch_guard import _estimate_tokens
        from types import SimpleNamespace

        c = _compressor("deepseek-v4-pro", model_protect_last_n=OVERRIDES)
        agent = SimpleNamespace(context_compressor=c)
        # 2 + 8 + 1 = 11 mensagens ainda cabem na protecao; 12 passam dela.
        # Com o global (2 + 20 + 1 = 23) 12 mensagens retornariam None.
        msgs = [{"role": "user", "content": "x" * 400}] * 12
        assert _estimate_tokens(agent, msgs) is not None
        astra = SimpleNamespace(
            context_compressor=_compressor(
                "gpt-6-astra", model_protect_last_n=OVERRIDES,
            )
        )
        assert _estimate_tokens(astra, msgs) is None


# ---------------------------------------------------------------------------
# Piso de causa: o round em andamento nunca e podado (NFOS_ROUND_EM_ANDAMENTO)
#
# A poda proativa roda no meio do turno, depois de anexar os resultados de
# ferramenta e antes da proxima chamada. Com protect_last_n=8 e 10 chamadas
# paralelas, 2 de 10 resultados do round em andamento viravam resumo antes de
# o modelo le-los (com 0, os 10). O piso vale para qualquer protect_last_n.
# ---------------------------------------------------------------------------

def _tc(i, big_args=False):
    args = '{"path": "%s"}' % ("p" * 700 if big_args else f"f{i}")
    return {"id": f"c{i}", "type": "function",
            "function": {"name": "read_file", "arguments": args}}


def _fan_out(n_fan, rounds=3, *, same_content=False, user_after_last=False,
             big_args_last=False):
    """system + user + `rounds` x (assistant com n_fan chamadas + n_fan resultados).

    Cada resultado tem ~3,6 mil caracteres (acima do piso de poda). O ultimo
    round e o round em andamento.
    """
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
    k = 0
    for r in range(rounds):
        last = r == rounds - 1
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [_tc(k + j, big_args=big_args_last and last)
                           for j in range(n_fan)],
        })
        for j in range(n_fan):
            body = "same " * 700 if same_content else ("data%d " % (k + j)) * 600
            msgs.append({"role": "tool", "tool_call_id": f"c{k + j}", "content": body})
        k += n_fan
    if user_after_last:
        msgs.append({"role": "user", "content": "e agora outra coisa"})
    return msgs


def _prune_compressor(protect_last_n, model="deepseek-v4-pro", model_map=None):
    return _compressor(
        model,
        protect_last_n=protect_last_n,
        model_protect_last_n=model_map,
        proactive_prune_tokens=1_000,
        proactive_prune_min_result_chars=500,
        proactive_prune_min_reclaim_tokens=10,
    )


def _round_slice(out):
    last_asst = max(i for i, m in enumerate(out) if m["role"] == "assistant")
    return last_asst, out[last_asst:]


class TestInFlightRoundFloor:
    @pytest.mark.parametrize("model_map", [{"deepseek": 8}, {"deepseek": 0}])
    def test_fan_out_of_10_keeps_every_current_result_with_override_8_or_0(
        self, model_map,
    ):
        """Com 8 e fan-out de 10, nenhum resultado do round atual vira resumo;
        com 0 tambem. Mutacao: tirar o ``min(prune_boundary, round_start)`` (o
        round fica de fora da fronteira e 2 de 10 viram resumo com 8, 10 de 10
        com 0)."""
        c = _prune_compressor(20, model_map=model_map)
        assert c.protect_last_n == model_map["deepseek"]
        msgs = _fan_out(10)
        out, n = c.prune_tool_results_only(msgs, current_tokens=10**6)
        assert n > 0  # os rounds anteriores foram podados
        _, current = _round_slice(out)
        originals = _round_slice(msgs)[1]
        assert current == originals  # assistente + 10 resultados, intactos

    def test_older_rounds_are_still_pruned(self):
        """O piso nao desliga a poda: os rounds anteriores continuam virando
        resumo. Mutacao: o piso devolve o comprimento inteiro (protege tudo)."""
        c = _prune_compressor(20, model_map={"deepseek": 0})
        out, _ = c.prune_tool_results_only(_fan_out(10), current_tokens=10**6)
        first_round = [m for m in out[:12] if m["role"] == "tool"]
        assert first_round and all(len(m["content"]) < 400 for m in first_round)

    def test_global_value_is_floored_too(self):
        """Vale para o global, sem override: fan-out de 25 com protect_last_n=20
        deixaria 5 resultados do round fora da cauda. Mutacao: piso aplicado
        so quando ha override."""
        c = _prune_compressor(20)  # sem mapa
        msgs = _fan_out(25)
        out, _ = c.prune_tool_results_only(msgs, current_tokens=10**6)
        assert _round_slice(out)[1] == _round_slice(msgs)[1]

    def test_assistant_message_of_the_round_is_protected_too(self):
        """A propria mensagem do assistente fica fora da poda de argumentos.
        Mutacao: ``round_start`` apontando para ``idx + 1`` (so os resultados)
        deixa o assistente da rodada fora da fronteira e os argumentos de 700
        caracteres sao truncados."""
        c = _prune_compressor(20, model_map={"deepseek": 0})
        msgs = _fan_out(10, big_args_last=True)
        out, _ = c.prune_tool_results_only(msgs, current_tokens=10**6)
        idx, _ = _round_slice(out)
        args = [t["function"]["arguments"] for t in out[idx]["tool_calls"]]
        assert all(len(a) > 700 for a in args)

    def test_identical_results_inside_the_round_are_not_back_referenced(self):
        """O dedup e cego a cauda por desenho, mas nao pode trocar um resultado
        do round por "Duplicate tool output". Mutacao: tirar o ``continue`` do
        dedup para ``i >= round_start``."""
        c = _prune_compressor(20, model_map={"deepseek": 0})
        msgs = _fan_out(10, same_content=True)
        out, _ = c.prune_tool_results_only(msgs, current_tokens=10**6)
        _, current = _round_slice(out)
        results = [m for m in current if m["role"] == "tool"]
        assert len(results) == 10
        assert all(m["content"] == "same " * 700 for m in results)

    def test_normal_case_with_20_is_unchanged(self):
        """Caso normal, global 20 e rounds de 2: exatamente as mensagens fora
        das ultimas 20 viram resumo e as 20 finais ficam intactas (o piso nao
        muda nada quando o round ja cabe na cauda). Mutacao: o piso passa a
        proteger tambem rounds ja encerrados ou a cauda vira 21."""
        # Varios formatos (chamadas por round x numero de rounds): a fronteira
        # cai em todas as posicoes do ciclo assistente/resultados, entao uma
        # cauda de 19 ou de 21 mensagens aparece em algum deles.
        for fan in (1, 2, 3, 4):
            for rounds in range(8, 8 + fan + 1):
                c = _prune_compressor(20)
                msgs = _fan_out(fan, rounds=rounds)
                out, _ = c.prune_tool_results_only(msgs, current_tokens=10**6)
                boundary = len(msgs) - 20
                for i, (before, after) in enumerate(zip(msgs, out)):
                    if before["role"] != "tool":
                        continue
                    if i < boundary:
                        assert len(after["content"]) < 400, (fan, rounds, i)
                    else:
                        assert after == before, (fan, rounds, i)

    def test_finished_round_is_not_floored(self):
        """Com uma mensagem de usuario depois, o round anterior ja foi lido e
        volta a obedecer o protect_last_n. Mutacao: ``_in_flight_round_start``
        sem a checagem do ultimo usuario (protege sempre o ultimo assistente
        com tool_calls)."""
        c = _prune_compressor(20, model_map={"deepseek": 0})
        out, _ = c.prune_tool_results_only(
            _fan_out(10, user_after_last=True), current_tokens=10**6,
        )
        last_round = [m for m in out[-11:-1] if m["role"] == "tool"]
        assert len(last_round) == 10
        assert all(len(m["content"]) < 400 for m in last_round)

    def test_compression_prune_pass_also_keeps_the_round(self):
        """O prune que a compressao roda antes de resumir (caminho por orcamento
        de tokens) tambem respeita o piso. Mutacao: a mesma do primeiro teste."""
        c = _prune_compressor(2, model_map={"deepseek": 2})
        msgs = _fan_out(10)
        out, _ = c._prune_old_tool_results(
            msgs, protect_tail_count=c.protect_last_n, protect_tail_tokens=8_000,
        )
        assert _round_slice(out)[1] == _round_slice(msgs)[1]


def test_substring_match_is_case_sensitive():
    """Documentado: "deepseek" nao casa "DeepSeek-V4". Mutacao: o resolver
    passa a comparar em minusculas (``key.lower() in model.lower()``)."""
    assert resolve_model_protect_last_n("DeepSeek-V4", {"deepseek": 8}, 20) == 20
    assert resolve_model_protect_last_n("DeepSeek-V4", {"DeepSeek": 8}, 20) == 8


# ---------------------------------------------------------------------------
# Config: chave publica, leitura no agent_init, cache do gateway
# ---------------------------------------------------------------------------

def test_key_is_public_and_empty_by_default():
    """Mutacao: a chave sai do DEFAULT_CONFIG ou o default deixa de ser {}."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["compression"]["model_protect_last_n"] == {}


def test_gateway_agent_cache_is_busted_by_the_key():
    """Mutacao: tirar a chave de ``_CACHE_BUSTING_CONFIG_KEYS``: o gateway
    reaproveitaria um agente cacheado com o valor antigo."""
    text = (Path(__file__).resolve().parents[2] / "gateway" / "run.py").read_text(
        encoding="utf-8"
    )
    assert '("compression", "model_protect_last_n"),' in text


def _config(model_protect):
    compression = {
        "enabled": True,
        "threshold": 0.50,
        "target_ratio": 0.20,
        "protect_first_n": 3,
        "protect_last_n": 20,
    }
    if model_protect is not None:
        compression["model_protect_last_n"] = model_protect
    return {
        "compression": compression,
        "prompt_caching": {"cache_ttl": "5m"},
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path: Path, model: str, model_protect):
    from hermes_cli import config as config_mod
    from hermes_state import SessionDB
    from run_agent import AIAgent

    cfg = _config(model_protect)
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: cfg)
    db = SessionDB(db_path=tmp_path / "state.db")
    with contextlib.redirect_stdout(io.StringIO()):
        return AIAgent(
            base_url="https://chatgpt.com/backend-api/codex",
            api_key="test-key",
            provider="openai-codex",
            model=model,
            enabled_toolsets=[],
            disabled_toolsets=[],
            quiet_mode=True,
            skip_memory=True,
            session_db=db,
            session_id="protect-last-n-test",
        )


class TestAgentInitSeam:
    def test_config_reaches_the_compressor(self, monkeypatch, tmp_path):
        """Mutacao: o agent_init le a chave mas nao a passa ao construtor (ou
        nao a le), e o deepseek fica com 20."""
        deepseek = _make_agent(monkeypatch, tmp_path, "deepseek-v4-pro", OVERRIDES)
        astra = _make_agent(monkeypatch, tmp_path, "gpt-6-astra", OVERRIDES)
        assert deepseek.context_compressor.protect_last_n == 8
        assert astra.context_compressor.protect_last_n == 20

    @pytest.mark.parametrize("absent", [None, {}, "texto", ["deepseek"], 7])
    def test_absent_or_malformed_map_keeps_20_for_everyone(
        self, monkeypatch, tmp_path, absent,
    ):
        """Mutacao: a normalizacao deixa um mapa nao-dict passar (quebra ou
        muda o valor) ou troca o default."""
        for model in ("deepseek-v4-pro", "gpt-6-astra"):
            agent = _make_agent(monkeypatch, tmp_path, model, absent)
            assert agent.context_compressor.protect_last_n == 20

    def test_invalid_value_in_config_falls_back_to_global(
        self, monkeypatch, tmp_path,
    ):
        """Mutacao: o agent_init faz ``int(v)`` (o "oito" derruba o init e o
        -3 vaza) ou deixa de validar."""
        for bad in (-3, "oito"):
            agent = _make_agent(
                monkeypatch, tmp_path, "deepseek-v4-pro", {"deepseek": bad},
            )
            assert agent.context_compressor.protect_last_n == 20
