# Matriz de enforcement do AOF no Hermes NF

Registro canônico (28/08/2026, ordem do operador: "quero o AOF completo em
tudo") de ONDE cada eixo do AOF é garantido, por quem, e o que ainda é
processo. Regra de arquitetura: **um dono por invariante**. Camadas podem
se sobrepor em série apenas quando aplicam a MESMA policy da mesma fonte
(defesa em profundidade); regra clonada com fonte própria é bug.

Contexto de runtime: o perfil de produção roda `provider: openai-codex`
(o modelo fala via Codex). Os hooks do Codex na VPS são de memória
(ai-memory) e AIRC (inbox) — NÃO fazem route-policy; não há duplicação de
escopo entre camadas hoje (verificado 28/08 em /srv/agents/codex).

| Eixo AOF | Dono único | Mecanismo | Estado |
|---|---|---|---|
| Estado do board: closeout persistido e substantivo | Kernel (tools) | `kanban_complete` persiste summary→result e RECUSA closeout raso do worker do próprio card (após o judge de goal-mode); `HERMES_KANBAN_REQUIRE_CLOSEOUT=off` escapa | **Enforcement real** (release 1745b9366b) |
| Estado do board: claims/ownership/guard de PR | Kernel (tools + dispatcher) | ownership no complete; reaper de claims mortos (worker vivo sempre vence); guard active_pr com rework bypass; automerge NUNCA mergeia head de release-train (staging/develop/main/master/release-*) | **Enforcement real** (1745b9366b + PR #61 pendente de release) |
| Escopo de paths/rotas por sessão | Plugin `aof-route-policy` do gateway | fingerprints re-pinados, self-test no boot | **Enforcement real** (desde a janela do aceite) |
| Transcript durável íntegro (sem replay duplicado) | Kernel (state) | dedupe idempotente no `append_message` (identidade por platform_message_id / conteúdo longo / rajada curta; `HERMES_STATE_DEDUPE=off` escapa) | **Enforcement real** (PR #61, pendente de release) |
| Closeout do principal | Runtime (mirror) | card do turno fecha com a mensagem final do turno (captura fiel; não há "recusar turno", conversa não é recusável) | **Captura garantida** (1745b9366b) |
| Closeout de subagente delegado | Runtime (delegation close) | child summary vira result do mirror; protocolo no goal orienta | Captura ok; enforcement de substância proposto (aplicar o mesmo guard do kernel quando o close usa a tool) |
| Smoke de release de produto (evidência antes de `released`) | Fluxo de release dos produtos | Mecanizado no lado do agente: automerge jamais publica release-train (PR #61). Gate completo PROPOSTO: required check de smoke da rota alterada nos repos de produto + policy no perfil ("staging→main só com evidência de smoke postada") | **Parcial**: trava mecânica do lado agente pronta; gate no repo do produto a decidir com o operador |
| Discovery Promotions preflight | Camada do agente | registry canônico consultável; validador executável no AOF; protocolo do worker orienta preflight | Processo + validador; mecanização adicional proposta (consulta no spawn) |
| Regressão (REGRESSION_LOG) e aprendizado | Repo + revisão | docs/regressions/ + disciplina de PR | Processo; mecanizável via gate de PR futuramente |

## Orientação × Enforcement
O protocolo em texto (goal do delegate, prompt do worker de dispatcher)
NÃO é garantia — é orientação que evita o ciclo cego de recusa-e-retry.
Toda garantia listada como "enforcement real" vive em código que recusa,
com escape de emergência por env e teste cobrindo o caso.

## Pendências decididas pelo operador
- Gate de smoke nos repos de produto (required check): aguarda decisão de
  onde plantar (CI do produto × hook do agente).
- Mecanizar preflight de promotions no spawn do worker.
- Elevar regressão a gate de PR.
