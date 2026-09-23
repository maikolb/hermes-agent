# Replay v2 do Jev: impedimento, sugestão de skill e falha de tool (pré-registro)

Data: 23/09/2026. Este arquivo é publicado antes de qualquer chamada paga. Uso previsto do Jev: muitas perguntas pequenas, estado pequeno, decisão em código, LLM e Principal só no que ele sinalizar ([pesquisa](jev-plano-corrigido.md)).

## Regras comuns

- **Fonte:** produção, só leitura: boards NFOS (`sqlite3` em modo leitura) e `state.db` do perfil factory, sessões `source='kanban'`, acesso pelo índice de sessão. Janela: de 08/09/2026 00:00 UTC até a extração. Impedimentos com `judge error: RateLimitError` ficam fora (incidente de 08 a 11/09).
- **Divisão:** por tempo. Os 50% mais antigos de cada frente são calibração; os 50% mais recentes, teste. Limiares são escolhidos só na calibração, por regra fixa abaixo, e congelados para o teste. As perguntas não mudam entre as duas partes.
- **Modelo:** `jev-1.13` pelo endpoint SystemOne do OpenRouter, prazo de 5 s, sem retry, guarda de gasto com teto cumulativo de US$ 2,88. Latência medida só na chamada ao Jev, sem a consulta de saldo do guarda.
- **Entrada:** JSON pequeno com campos nomeados. Perguntas em inglês; conteúdo como veio (português).
- **Privacidade:** conteúdo dos cards e sessões fica na VPS. O repositório recebe só contagens.
- **Decisão:** frente que não passar no teste não é construída nem ativada.

## Frente 1: impedimento

- **Caso:** decisão `impediment` respondida pelo Principal (`continue`, `changes` ou `human`).
- **Estado:** `{"worker_question": pergunta do worker (até 4.000 caracteres), "requested_block_kind": tipo pedido}`.
- **Perguntas:**
  - `request_type` (choice): `decision_or_permission`, `outside_input`, `technical_problem`, `status_or_progress`, `unclear`.
  - `can_continue_alone` (noul): o worker consegue seguir sozinho, no escopo que já tem, sem decisão ou insumo novo?
  - `needs_business_owner` (noul): resolver depende do dono ou do cliente, e não de engenharia?
- **Regra (código):** responder "continue" sem o Principal se `request_type` for `technical_problem` ou `status_or_progress` com confiança ≥ T, `can_continue_alone` ≥ T e `needs_business_owner` ≤ 0,2.
- **Rótulo:** acerto se o Principal respondeu `continue`; erro se respondeu `changes` ou `human`.
- **Calibração:** menor T da grade 0,50 a 0,95 (passo 0,05) com erro ≤ 5% nas respostas automáticas.
- **Critério no teste:** erro ≤ 5% e limite superior de Wilson 95% ≤ 10%; cobertura (respostas automáticas sobre impedimentos do teste) ≥ 20%; p95 da chamada ≤ 1 s.
- **Medida secundária (não decide):** entre os acertos, fração em que a resposta do Principal trazia instrução específica (mais de 200 caracteres). Isso se perderia com um "continue" genérico.

## Frente 2: sugestão de skill (cookbook oficial feito com o Hermes)

- **Caso:** card com sessão de worker que carregou ao menos uma skill.
- **Rótulo:** skills distintas nas 10 primeiras chamadas `skill_view` da primeira sessão do card, normalizadas pelo nome final.
- **Catálogo:** skills do perfil factory (nome e descrição do SKILL.md).
- **Estado:** `{"request": texto do pedido original, "recent_context": ""}`.
- **Chamada 1:** choice sobre o catálogo (nome e descrição curta) e 3 nouls de necessidade (`needs_action`, `needs_procedure`, `needs_domain_knowledge`). Média das nouls < 0,30: não sugere.
- **Chamada 2:** choice sobre as 3 melhores da chamada 1, com a descrição completa, e uma noul `fits` por candidata. Maior `fits` < 0,30: não sugere.
- **Linha de base:** sempre sugerir a skill mais frequente na calibração.
- **Critério no teste:** precisão (sugestão está entre as skills que o worker carregou) ≥ 60% e pelo menos 15 pontos acima da linha de base; cobertura ≥ 30% dos cards.
- **Limite:** a escolha do worker não é verdade absoluta. Esta frente mede se o Jev antecipa a escolha, não se ela era a melhor.

## Frente 3: repetir ou mudar após falha de tool

- **Caso:** chamada de tool que falhou (`error` ou `exit_code` ≠ 0) e foi repetida de forma idêntica (mesma tool, mesmos argumentos) em até 30 chamadas na mesma sessão.
- **Rótulo:** a repetição funcionou ou falhou de novo.
- **Amostra:** 300 casos da calibração e 300 do teste, sorteio com semente 20260923.
- **Estado:** `{"tool": nome, "arguments": até 1.500 caracteres, "error_output": até 2.000 caracteres do resultado}`.
- **Perguntas:**
  - `retry_succeeds` (noul): repetir a mesma chamada, sem mudar nada, deve funcionar?
  - `failure_type` (choice): `transient_network_or_rate_limit`, `wrong_command_or_arguments`, `missing_dependency_or_permission`, `code_or_test_failure`, `environment_or_state`, `unclear`.
- **Regra (código):** "não repetir, mude a abordagem" se `retry_succeeds` ≤ 1 − T.
- **Calibração:** menor T da grade 0,60 a 0,95 com precisão de "não repetir" ≥ 80%.
- **Critério no teste:** precisão de "não repetir" ≥ 80% com limite inferior de Wilson 95% ≥ 65%; recall das repetições inúteis ≥ 25%. A taxa base de repetição inútil é reportada ao lado.
