# Execution Contract

## Contract Metadata
- Contract Version: 2
- Mode: RELEASE
- Risk Level: HIGH
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Updated At: 2026-08-24T00:00:00-03:00
- Machine Runtime Authority: none: this release consolidation uses explicit reviewed Git operations; no autonomous code-edit loop is authorized

## Requested Outcome
- Preservar, separar em commits coerentes e publicar todo o trabalho local válido do Hermes Agent; reconciliar os branches/worktrees concluídos em uma `main` limpa no fork `maikolb`, sem reintroduzir implementações superadas do intake nem misturar o projeto externo no core.

## Acceptance Criteria
- Todo branch local, worktree, stash e arquivo dirty é inventariado; nenhuma alteração válida é perdida.
- O working tree misto é dividido em commits rastreáveis por responsabilidade: transporte do intake, OAuth genérico, checkpoint/resume, workspace lease, ACL, runtime identity, wake e worktree routing.
- Branches antigos já superados não são mesclados cegamente; equivalência e ancestry são registradas e apenas commits realmente ausentes entram na `main`.
- A `main` do fork `maikolb/hermes-agent` contém todo o trabalho concluído, está reconciliada com o upstream atual sem force-push e é confirmada por hash remoto.
- Stashes de quarentena/incompletos permanecem preservados, identificados e fora da linha de release até prova de conclusão.
- Todos os worktrees terminam limpos; branches removidos localmente, se houver, só depois de alcançáveis por refs remotos confirmados.
- Um único cron do perfil Hermes PF lê o lote `raw`, separa conversa normal de candidatos, infere uma referência aberta de projeto e grava candidatos canônicos com selo antes de qualquer mutação externa.
- O mesmo Hermes PF decide somente a relação `new`, `complement`, `duplicate` ou `regression`, chama o writer Jira sem alterar a semântica selada e conclui o checkpoint.
- O cron antigo do perfil padrão é removido; o perfil padrão participa apenas pelo bridge que já persiste o WhatsApp em `raw`.
- A credencial Jira não é copiada para memória/configuração do PF: a chamada pontual ao writer usa o armazenamento OAuth já autorizado, por caminho explícito no subprocesso.
- Falha de formato, cobertura, selo ou registro preserva o lote `raw` e o checkpoint `curated` para recuperação.
- O snapshot imediatamente anterior à exclusão define a lista exata de issues `HIS`; somente essa lista é apagada e um snapshot posterior comprova o sandbox vazio.
- Testes focais, suites afetadas, hashes source/runtime, contratos e `VisibleWindows=0` ficam verdes antes da entrega.

## In Scope
- `C:\Users\maiko\AppData\Local\hermes\hermes-agent\**`
- Worktrees registrados `C:\Users\maiko\Projetos\hermes-bug-intake-20260821\**`, `C:\Users\maiko\Projetos\hermes-tool-guardrail-fix-20260821\**` e `C:\Users\maiko\Projetos\hermes-workspace-api-worktree\**`.
- Worktree temporário de integração `C:\Users\maiko\Projetos\hermes-main-consolidation-20260824\**`, criado de `origin/main` e removido somente depois do push verificado.
- Branches locais `codex/hermes-workspace-api`, `codex/tool-guardrail-redirect-state`, `feat/passive-intake-20260821`, `fix/context-compaction-active-turn-20260819`, `fix/telegram-topic-status-isolation-20260818`, `integrate/local-runtime-20260820`, `integrate/local-runtime-v2-20260820`, `main` e `preserve/wip-local-main-20260818`.
- Remoto pessoal `maikolb`; remoto `origin` somente como upstream de leitura.
- Remoção explícita do resíduo inválido não rastreado `tests/gateway/test_whatsapp_bridge_spawn_config.py`, após confirmação de que importa helper promovido/removido.
- `scripts/bug_intake_files.py`, somente para remover o helper de intake específico deste repositório após sua promoção ao pacote externo.
- `tests/scripts/test_bug_intake_files.py`, somente para remover os testes do helper promovido.
- `docs/bug-intake-sandbox.md`, somente para mover o runbook específico para o pacote externo e remover a cópia deste repositório.
- `docs/regressions/REG-2026-08-23-003.md`
- `docs/EXECUTION_CONTRACT.md`
- `C:\Users\maiko\AppData\Local\hermes\scripts\bug-intake-sandbox*.py`, somente para remover o gate antigo do cron padrão.
- `C:\Users\maiko\AppData\Local\hermes\profiles\hermes-project-factory\scripts\bug-intake-sandbox*.py`, somente launchers/gates finos de perfil, sem regra de intake.
- Os jobs cron do sandbox nos perfis Hermes padrão e Hermes PF, alterados somente pela interface pública de cron: remover o job antigo padrão e criar um único job PF.
- Jira `HIS`, exclusivamente para snapshot e exclusão pontual dos issues confirmados imediatamente antes da exclusão.

## Out of Scope
- Mergir branches remotos de contribuidores do upstream ou publicar qualquer ref em `NousResearch/hermes-agent`.
- Incorporar os fontes do repositório `C:\Users\maiko\Projetos\hermes-intake-hub` ao core.
- Aplicar ou apagar stashes rotulados `quarantine`, `incomplete` ou autostash sem validação independente.
- Alterar Jira, cron, WhatsApp, Telegram, Kanban ou serviços de runtime como efeito da consolidação Git.
- Force-push, reset destrutivo, squash de branch stale ou refatoração funcional nova.
- Alterar bridge/captura WhatsApp, JID, DMs, Telegram, grupos reais, Jira fora de `HIS`, Kanban, código de produto ou produção.
- Autorizar OAuth novo ou copiar arquivos de credencial entre perfis.
- Exigir que o usuário declare o projeto no grupo teste.
- Adicionar banco, serviço, watcher, analisador multimodal, fila paralela ou regra de batching nova.

## Failure Signal / Repro
- O checkout ativo está em `integrate/local-runtime-v2-20260820` com 24 arquivos rastreados alterados e 10 não rastreados de múltiplas responsabilidades; há quatro worktrees, nove branches locais e quatro stashes.
- A `main` local rastreia o upstream `origin/main` e está milhares de commits atrás do ref upstream em cache, enquanto o trabalho próprio rastreia o fork `maikolb`; um merge indiscriminado poderia duplicar, regredir ou publicar no remoto errado.
- Evidência forense e cenário reproduzível: `C:\Users\maiko\AppData\Local\hermes\hermes-agent\docs\regressions\REG-2026-08-23-003.md` documenta que a partição de fontes estava correta, mas o agente da fase B releu histórico misto e contaminou o título do candidato de webhook com `LECLER SAÚDE`, entidade presente somente no candidato irmão.
- No mesmo lote, o agente primeiro gravou arquivos planos com schema inventado fora do layout canônico; o scanner os ignorou até autocorreção posterior.

## Root-Cause Hypothesis
- Facts: trabalhos concluídos foram acumulados em uma branch de integração e três worktrees; passive intake e guardrail antigos já possuem implementações posteriores na integração; o workspace API possui commits próprios ainda não integrados.
- Assumptions: `maikolb/main` é a autoridade publicável do usuário e `origin/main` é apenas upstream; branches/stashes rotulados WIP/quarantine não são release-ready sem prova.
- Chosen fix point: preservar primeiro o working tree em commits separados, atualizar refs, construir uma integração limpa sobre a autoridade remota correta, reaplicar apenas commits únicos e validados e publicar sem reescrever histórico.
- Facts: a partição `source_ids` ficou correta; um segundo agente releu histórico e reescreveu a identidade semântica já correta do candidato com uma entidade não sustentada por suas fontes; o token OAuth é armazenado por perfil, mas o storage suporta caminho explícito.
- Assumptions: o cron PF consegue invocar o writer em subprocesso apontando para o storage autorizado sem copiar o token e sem mudar a identidade/conversa do PF.
- Chosen fix point: um único agente PF é autor semântico e decisor Jira; o pacote externo sela e valida o checkpoint antes do writer, e o perfil padrão fica somente no transporte WhatsApp.

## Claim Discipline
- Facts already established: fork, upstream, worktrees, branches, stashes e diff misto foram enumerados; os três worktrees auxiliares estão limpos; o branch ativo está sincronizado com seu branch remoto.
- Inferences that still require validation: estado remoto atual após fetch, equivalência completa dos branches antigos e compatibilidade dos commits próprios com o upstream atual.
- Highest readiness state allowed by current evidence: diagnosed.
- Target readiness checklist or equivalent: contratos verdes; dirty work dividido e testado; refs atualizados; integração sem perdas; suite focada e integrada verde; push no fork confirmado; status de todos worktrees limpo.
- Facts already established: o lote real foi capturado; os dois assuntos foram separados; o vazamento de `LECLER SAÚDE` ocorreu depois da partição; o PF não possui OAuth Atlassian e o perfil padrão possui a fronteira autenticada atual.
- Inferences that still require validation: o PF inferirá corretamente o projeto em lote novo sem dica; o split de cron eliminará releitura semântica; o próximo lote do usuário chegará ao Jira sem intervenção desta sessão.
- Highest readiness state allowed by current evidence: diagnosed.
- Target readiness checklist or equivalent: implementação revisada; testes locais verdes; runtime promovido por hash; crons observados autonomamente; sandbox vazio; teste do usuário pendente para `validated-target` e `accepted`.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Não consumir nem apagar `raw` diante de qualquer erro de contrato.
- Não permitir chamada OAuth/Jira quando relatório, request ou selo não forem canônicos e coerentes.
- Não usar intervenção manual no pipeline como evidência de funcionamento autônomo.
- Não permitir que histórico, Jira ou candidato irmão alterem semântica já selada.
- Não apagar issue fora da lista exata confirmada no snapshot imediatamente anterior à exclusão.
- Não disparar pipeline, Jira, cron ou gateway como parte da consolidação.
- Não usar `git reset --hard`, `git checkout --`, force-push, squash destrutivo, `git clean` ou remoção recursiva.
- Não mesclar um branch antigo quando o trabalho já estiver superseded no branch de integração.
- Não remover branch, worktree ou stash antes de provar recuperação por commit/ref remoto.

## Loop Control
- The controlled micro-loop is not required because no autonomous code-edit runner is authorized; the root agent will execute at most three explicit patch-test-review iterations and stop closed on any unresolved mismatch.
- Qualification: bounded manual spec-build-review-green release consolidation.
- Maximum build/test/fix iterations: 3.
- Stop condition: focused and integrated tests pass, all intended commits are reachable from the fork `main`, remote/local hashes match and every worktree is clean.
- Escalation rule: after three failed iterations or any unresolved conflict, authorship, remote or test ambiguity, stop before push and report blocked with evidence.
- Runtime authority path: none; every mutation is issued and reviewed explicitly by the root agent.
- Append-only evidence path: `docs/regressions/REG-2026-08-23-003.md`.

## Validation Plan
- Analyze/lint: `git diff --check`, ancestry/range-diff, secret/path review and restricted-diff review for every commit.
- Unit tests: seal idempotence, semantic mutation rejection, allowed Jira-only fields, sibling-entity isolation and commit rejection on divergent seal.
- Integration/contract tests: executar via `scripts/run_tests.sh` os testes de bridge/passive intake, checkpoint/resume, Kanban lease/routing, ACL/status/wake e OAuth; nenhum teste pode tocar o `HERMES_HOME` real.
- Build/install/deploy checks: validação de imports/build proporcional ao conjunto integrado; nenhum deploy ou restart de serviço é autorizado.
- Target or environment checks: fetch dos dois remotos, `git ls-remote` do fork e comparação de hashes de `main`.
- Delivery pipeline checks: push sem force e consulta dos checks GitHub quando disponíveis.
- Manual smoke checks: não aplicável; provar `VisibleWindows=0` durante execução real.

## Status
- Contract preflight: pending release validation.
- Implementation: pending Git consolidation.
- Validation: pending integrated-main tests and remote verification.
- Completion: pending.
