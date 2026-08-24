# Execution Contract — Hermes Project OS Telegram + Kanban

## Contract Metadata
- Contract Version: 2
- Mode: IMPLEMENT_THEN_VERIFY
- Risk Level: high
- Workspace: C:/Users/maiko/AppData/Local/hermes/hermes-agent
- Updated At: 2026-08-13
- Machine Runtime Authority: docs/HERMES_PROJECT_OS_AGENT_LOOP_RUN.json

## Requested Outcome
- Implementar a especificação `C:/Users/maiko/Downloads/HERMES_PROJECT_OS_TELEGRAM_KANBAN.md` na instalação oficial atual do Hermes, fornecendo operação natural Telegram → Project Router/ACL → projeto/board/task/sessão/notificação para os perfis reais `default`, `hermes-project-factory` e `hermes-ceogame`, com Honcho degradado, AOF + Codex Spec/Loop/Verde e AIRC apenas como observabilidade.

## Acceptance Criteria
- O runtime resolve deterministicamente `(profile, chat_id, thread_id, sender_user_id)` para team/project/board/workdir/ACL antes de expor ou executar operações Kanban.
- O escopo do board é por request/session/task e não usa `boards switch` nem mutação global de `HERMES_KANBAN_BOARD` no gateway concorrente.
- Reprocessar a mesma mensagem Telegram não duplica projeto, binding, board, task ou subscription.
- Mensagens observadas sem menção entram como contexto atribuído e nunca viram instrução executável isoladamente.
- Project Factory permite Maikol `996979567`, Japa `7550030839` e Pablo `1474565437`; Pessoal permite apenas Maikol; Ceogame nega Pablo e só ganha outros membros por allowlist explícita já confirmada.
- Topics de projeto usam sessão compartilhada; Gestão agrega o time sem ser board de projeto.
- Topic manual pode ser registrado e receber board; criação de projeto pode criar Topic via primitive Telegram nativa quando o bot possui permissão.
- Criação, comentário, pausa/continuação, follow-up, dependência, paralelo e status natural usam APIs/tools internas, não mensagens slash simuladas.
- Notificações de created/blocked/failure/completed retornam exatamente ao chat/thread de origem.
- Persistência transacional sobrevive a reabertura/restart e usa armazenamento profile-scoped.
- Writers no mesmo workspace canônico são serializados por lease; worktrees não são criados nesta entrega.
- Slash commands, DM normal, Topics, sessões, dashboard, dispatcher, workers e CLI permanecem compatíveis.
- Testes unitários, integração, concorrência, ACL, observed chatter, auto-create, restart e notification routing passam.
- Bootstrap dos três perfis é idempotente e backups precedem mutações de config/SOUL/runtime state.
- Smoke real Telegram passa nos alvos tecnicamente disponíveis; limitações do Bot API ou permissões humanas são reportadas como passos mínimos, sem simulação.
- A lane Claude está cancelada por indisponibilidade de quota informada pelo usuário; Codex é o único writer/reviewer/validator desta execução e Claude não deve ser invocado.
- Honcho e AIRC têm saúde/evidência real ou ficam marcados como bloqueados, sem serem promovidos a autoridade.

## In Scope
- Novo componente profile-safe de Project Router/ACL/persistência dentro do checkout oficial, preferencialmente em `gateway/` ou plugin oficial isolado.
- Menor hook reutilizável necessário no gateway para resolver contexto de projeto antes do turno e expô-lo via contexto de sessão concorrente.
- Menor extensão necessária nas tools/APIs Kanban para aceitar board autoritativo por request e rejeitar override divergente.
- Integração com primitives Telegram já existentes de Topics, `forum_topic_created`, shared sessions, observed chatter e delivery por `message_thread_id`.
- Testes novos sob `tests/gateway/`, `tests/tools/` e/ou `tests/hermes_cli/` para os critérios acima.
- `docs/HERMES_PROJECT_OS_EXECUTION_CONTRACT.md`, `docs/HERMES_PROJECT_OS_AGENT_LOOP_RUN.json`, evidência JSONL e documentação operacional mínima do componente.
- Configurações e `SOUL.md` somente dos homes reais: default, `hermes-project-factory`, `hermes-ceogame`, com backups externos ao checkout antes de mutação.
- Registry/boards/bindings profile-scoped para os projetos expressamente previstos na especificação e Topics realmente detectados/criados.
- Contexto de workflow: Honcho apenas como memória auxiliar degradada; AOF como governança; Codex único writer/reviewer/validator; AIRC apenas como observabilidade.

## Out of Scope
- Qualquer código, banco, rota, protocolo, variável, launcher, porta ou dependência das duas implementações Hermes Project Ops rejeitadas, incluindo `C:/Users/maiko/Projetos/Hermes Workspace Portal` e `C:/Users/maiko/Projetos/hermes-workspace-api-worktree`.
- Recriar, renomear, clonar ou excluir os três perfis-alvo existentes.
- Criar bots ou grupos Telegram via userbot/MTProto.
- Autenticação nova, pairing novo, RBAC genérico, multiusuário SaaS, VPS/deploy, exposição pública ou hardening adjacente.
- Criar worktrees, clones, shadow folders ou resetar/stashar/descartar o trabalho local pré-existente.
- Criar um banco paralelo para Kanban, sessões ou projetos quando a autoridade nativa já existir; Project Router pode ter somente o registry transacional indispensável.
- Alterar projetos de produto associados (RecuperaCli, DOVCRM, Mulher +Segura, Sommus, Ceogame).
- Instalar AIRC lookalike; somente a instalação canônica CambrianTech/airc pode ser usada.
- Substituir Claude Fable 5, Codex ou AIRC por subagentes internos Hermes sem autorização explícita.

## Failure Signal / Repro
- Hoje o Project Factory possui um único `kanban.default_board: project-factory`; não existe binding autoritativo por `(profile, chat_id, thread_id)` e o profile não pode selecionar boards concorrentes por Topic sem risco de escopo global.
- `hermes-ceogame` está com `telegram.observe_unmentioned_group_messages: false` e `telegram.group_allowed_chats: []`, divergindo do fluxo desejado.
- Não há Project Router persistente no checkout oficial nem contrato determinístico de ACL por sender+topic antes das tools.
- Honcho está configurado, mas `localhost:8500` recusou conexão no preflight; `honcho_context` não inicializou.
- AIRC canônico existe em `C:/Users/maiko/agent-ops/src/airc`, mas não está no PATH do runtime atual.

## Root-Cause Hypothesis
- Facts: Hermes já fornece Topics, `forum_topic_created`, shared Topic sessions, observed chatter, multi-board Kanban, subscriptions e delivery por thread; falta uma composição autoritativa e concorrente entre essas primitives.
- Assumptions: um hook pequeno no gateway e contexto de sessão/contextvar podem carregar o binding sem mutar ambiente global; o registry indispensável pode ser SQLite profile-scoped.
- Chosen fix point: extensão isolada Project Router + hook de pre-turn/contexto + board enforcement nas tools, reutilizando primitives nativas e sem reimplementar Telegram/Kanban/sessões.

## Claim Discipline
- Fatos validados: núcleo Project Router/ACL/ContextVar/Kanban/Telegram implementado; Gestão permanece control plane sem board físico; bootstrap preparatório convergiu nos três perfis; Project Factory real continua `group`, `is_forum=false`, bot `member`, `can_manage_topics=false`.
- Fatos bloqueados: Ceogame/default tiveram timeout de rede no read-back atual; Topic IDs e bindings reais não existem; smoke Telegram não foi executado; nenhum gateway foi reiniciado com o novo runtime.
- Readiness máxima: `validated-local` para código e bootstrap preparatório; `target-blocked-human` para Topics/permissões; não `released`.
- Target readiness checklist: habilitar Fórum nos grupos, promover bots com `Gerenciar tópicos`, executar bootstrap de Topics/bindings, reiniciar externamente por cadeia zero-UI validada e realizar smoke de retorno ao Topic.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Não ler, importar, executar, copiar, migrar ou referenciar qualquer implementação Project Ops rejeitada.
- Não mutar `os.environ["HERMES_KANBAN_BOARD"]` por mensagem no gateway.
- Não usar `boards switch` para roteamento de Topic.
- Não inferir IDs, membros, paths, tokens ou permissões.
- Não imprimir nem persistir tokens/segredos em contrato, logs AIRC, prompts ou evidências.
- Não executar dois writers no checkout canônico.
- Não editar arquivos já dirty fora do menor hunk indispensável; preservar integralmente mudanças preexistentes.
- Não reiniciar/parar o gateway que está processando este turno; ativações serão externas e zero-UI.
- Stop/cancel de processo rastreado usa exclusivamente Hermes `process(action='kill')`.

## Loop Control
- Qualification: código, ACL, persistência, concorrência e target Telegram; controlled loop obrigatório.
- Maximum build/test/fix iterations: 12 no Codex, conforme a implementação congelada pelo usuário; máximo 3 ciclos materiais de correção/review antes de checkpoint humano.
- Stop condition: todos os green checks aplicáveis passam no snapshot exato; target smoke passa ou existe bloqueio externo específico comprovado.
- Escalation rule: conflito irredutível com dirty work, credencial/permissão Telegram ausente, ou três ciclos materiais sem convergência interrompe nova mutação e reporta o gate exato.
- Runtime authority path: docs/HERMES_PROJECT_OS_AGENT_LOOP_RUN.json
- Append-only evidence path: docs/HERMES_PROJECT_OS_AGENT_LOOP_EVIDENCE.jsonl

## Validation Plan
- Analyze/lint: `git diff --check` e compilação/linters focalizados dos arquivos alterados.
- Unit tests: testes focalizados para slug, binding, ACL, idempotência, auto-create, management, follow-up, dependência e paralelo.
- Integration/contract tests: Telegram adapter/gateway + router + Kanban; concorrência multi-board; persistence reopen; notification routing; existing slash fallback.
- Build/install/deploy checks: import/packaging do checkout exato; bootstrap dry-run e apply idempotente com read-back.
- Target or environment checks: gateway/profile health, Telegram getChat/getChatMember/topic permissions e smoke natural no chat/thread autorizado.
- Delivery pipeline checks: Git exact-tree, commit, push e remote SHA/CI apenas após preservar/classificar todo dirty work e obter snapshot coeso.
- Manual smoke checks: mensagem observada sem resposta; menção natural cria task; conclusão retorna ao mesmo Topic; ACL negativa de Pablo no Ceogame sem executar ação.

## Status
- Contract preflight: pass — contratos AOF/Codex validados pelo checker oficial.
- Implementation: pass — Project Router SQLite, ACL fail-closed, ContextVars concorrentes, board enforcement, idempotência automática, primitive natural de projeto, integração Telegram Topics e control plane Gestão sem board.
- Validation: pass-local — matrizes focais passaram, incluindo 74 testes na bateria final, compileall/AST e `git diff --check`.
- Bootstrap: partial/validated-local — config, ACL, catálogos, boards e SOUL aplicados nos três perfis; segundo apply `change_count=0`, `drift=[]`, sem bindings fabricados.
- Target: blocked — Project Factory não é fórum e o bot não gerencia Topics; Ceogame/default sofreram timeout no read-back atual; launcher Ceogame atual possui `conhost.exe` e a superfície de escrita bloqueou a troca do VBS.
- Completion: blocked-before-target — smoke Telegram, restart externo validado, commit/push/read-back e `released` não concluídos neste contrato.
