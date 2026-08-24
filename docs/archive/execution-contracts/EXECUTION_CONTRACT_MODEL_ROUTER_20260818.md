# CODEX Execution Contract

## Contract Metadata
- Mode: CHANGE_THEN_VERIFY
- Risk Level: high
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Updated At: 2026-08-18T12:00:00-03:00

## Requested Outcome
- Remover a política global que invalida todo Claude não-Fable e implantar dois roteadores inteligentes e auditáveis: GPT-5.6 Luna/Terra/Sol e Claude Haiku/Sonnet/Opus/Fable.
- Critério decisório obrigatório: qualidade em primeiro lugar; velocidade apenas como desempate entre rotas que demonstrem qualidade equivalente; custo não participa da decisão.
- Nenhum modelo menor ou effort inferior pode ser liberado apenas por heurística lexical. A elegibilidade depende de benchmark representativo e revision-bound por classe de tarefa; ausência, expiração ou falha da evidência sobe para a rota conservadora.

## In Scope
- `agent/smart_model_routing.py`, integração já existente em `gateway/run.py` apenas se necessária e testes focados.
- `smart_model_routing` dos seis perfis, sem ler ou expor segredos.
- `SOUL.md`, memórias e skills operacionais ativas que hoje impõem Fable-only.
- Validador/promoção global de saúde Hermes, para manter o harness revision-bound após a mudança do roteador.
- `docs/EXECUTION_CONTRACT.md` e `docs/REVISION_PROTOCOL.md`.
- Harness e artefatos globais sanitizados do benchmark, fora de qualquer projeto do usuário.

## Out of Scope
- WhatsApp, Telegram, Kanban, projetos, boards, bancos, sessões, credenciais e modelos de outros provedores.
- Evidência histórica, backups, logs, caches e transcritos antigos.

## Failure Signal / Repro
- O `SOUL` global e cinco perfis declaram Claude válido apenas com Fable 5; memórias e skills repetem a proibição e mandam bloquear Opus/Haiku/Sonnet. O roteador atual aceita somente `gpt-5.6-luna|terra|sol`, portanto uma rota Anthropic preserva sempre o baseline e não escolhe Claude por complexidade.

## Root-Cause Hypothesis
- Facts: a restrição está duplicada em várias autoridades ativas; `agent/smart_model_routing.py` possui regex e providers elegíveis exclusivos da família GPT-5.6; Claude Code 2.1.233 aceita `--model` com aliases Fable/Opus/Sonnet e esforço low/medium/high/xhigh/max; o catálogo Hermes inclui Fable 5, Opus 4.8, Sonnet 5 e Haiku 4.5.
- Assumptions: nenhum perfil será migrado para Anthropic; apenas ganha suporte provider-aware quando Claude for a lane escolhida.
- Chosen fix point: uma matriz única no classificador existente, compatível com o schema GPT atual, mais política operacional apontando para a mesma decisão em delegações Claude Code.
- Regression found during validation: testes unitários e um microbenchmark do classificador provaram somente decisão determinística e overhead local; não provaram equivalência de qualidade dos modelos/efforts. A primeira matriz permitia downgrade baseado em sinais lexicais sem um quality gate empírico.
- Revised fix point: fail-safe conservador imediato e allowlist de rotas menores derivada de benchmark real. O benchmark usa tarefas representativas, repetição, gates determinísticos e avaliação cega; latência só é comparada após o quality gate.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Não alterar provider, credencial ou modelo primário de perfil.
- Chamadas de benchmark são autorizadas apenas no harness sanitizado, bounded e sem conteúdo de projetos reais; registrar modelo, effort, latência e escore, nunca credenciais nem prompts privados.
- Reiniciar somente as três tasks Hermes já `Running`, após verde local, sob monitor zero-UI e com rollback pela própria task agendada.
- Não reescrever evidência histórica nem backups.

## Validation Plan
- Analyze/lint: `py_compile` e `git diff --check` nos arquivos afetados.
- Unit tests: matriz Claude e regressão completa do smart router GPT.
- Integration/contract tests: resolver rotas sintéticas com provider Anthropic e confirmar provider/credencial/cache/override/fail-safe.
- Runtime reload: reiniciar de modo controlado apenas default, CEOGame e Project Factory; validar nova identidade de processo, saúde dos canais e `VisibleWindows=0`.
- Config checks: parse sanitizado dos seis perfis e matriz Claude explícita e idêntica.
- Policy checks: zero regras ativas Fable-only; evidência histórica excluída do gate.
- Quality benchmark: tarefas autocontidas nas classes explicação fiel, alteração de código com testes ocultos, diagnóstico, arquitetura e operação de alto risco; no mínimo três repetições por rota candidata quando houver variância.
- Quality gates: zero falha crítica; todos os must-have determinísticos; sem regressão em tarefas de alto risco; rota menor só elegível quando não inferior ao baseline forte na classe correspondente. Incerteza promove para o modelo/effort superior.
- Blind review: outputs anonimizados e avaliados por rubrica sem revelar o modelo; verificações determinísticas têm precedência.
- Latency gate: comparar velocidade somente entre configurações aprovadas no quality gate. Custo é ignorado.
- Reasoning calibration: comparar esforços adjacentes por classe; selecionar o menor effort somente com equivalência de qualidade comprovada, mantendo um degrau acima quando a amostra for inconclusiva.
- Runtime smoke: após reload, provar rota conservadora; rotas menores ficam bloqueadas até que o artefato de benchmark válido seja consumido pelo resolver.

## Status
- Contract preflight: validated-local; revalidation required after this revision
- Implementation: quality fail-safe implemented and loaded; lower-route allowlist intentionally empty
- Validation: OpenAI empirical benchmark completed and rejected every downgrade at the blind gate; Claude empirical benchmark blocked-external by logged-out subscription session
- Completion: partial — conservative production safety is validated-local; lower-model calibration and target behavioral acceptance remain pending

## Evidence and decision

- 72 OpenAI generation runs and 6 blind Sol/Max judge runs used synthetic data. Terra/High was 44.49% faster than Sol/XHigh on deterministic executive-fidelity checks, but blind review found 4 critical semantic inventions versus 2 for Sol and 5 for Luna. Speed therefore did not override quality.
- The router fails closed to Sol/XHigh or Fable/XHigh and rises to Max for high-risk/long-horizon work. Explicit user overrides remain authoritative.
- A `quality_policy: benchmarked` label is insufficient by design: `_APPROVED_BENCHMARK_POLICY_SHA256` is empty. A lower route can be enabled only by a source revision that pins the exact approved consolidated-evidence hash.
- Claude Code reported `loggedIn=false`; no paid API-key substitute was introduced. Haiku/Sonnet/Opus stay blocked in automatic routing until the subscription-backed benchmark can run.
- Canonical evidence: `C:\Users\maiko\agent-ops\benchmarks\hermes-model-router-quality-20260818\RESULTS.md`, `openai-quality-consolidated.json`, and `openai-summary-blind-judge.json`.
- Runtime evidence: global promotion v1.3.1, report `C:\Users\maiko\.codex\reports\hermes-multiteam-quality-failsafe-v1.3.1.json`, event `14477329-7ff4-4904-9879-1f177b1c74e4.json`; target remains pending only for behavioral validation.
