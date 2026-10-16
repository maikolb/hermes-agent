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
