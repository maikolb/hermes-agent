# CODEX Execution Contract

## Contract Metadata
- Mode: CHANGE_THEN_VERIFY
- Risk Level: high
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Updated At: 2026-08-20T08:54:39-03:00

## Requested Outcome
- Garantir continuidade durável de estado conhecido através de compactação/restart, retomada operacional automática e compactações menos frequentes, sem sacrificar a qualidade do contexto recente nem apagar o transcript original; eliminar também janelas visíveis produzidas por qualquer descendente dos gateways Hermes no Windows.

## In Scope
- `agent/turn_checkpoint.py`: projeção canônica do transcript usada no protocolo write-ahead.
- `hermes_state.py` e `run_agent.py`: persistência/replay do nome de resultados de ferramenta apenas no ponto necessário para estabilidade do round-trip.
- Auditoria e, somente se necessário, hardening mínimo do estado operacional persistido pelo checkpoint: fase, próxima ação, transcript before/after, ferramentas incertas, verificação, entrega, paths, artefatos e bloqueios.
- Política de compactação nos seis perfis locais: `threshold=0.85`, `target_ratio=0.30`, `protect_first_n=5`, `protect_last_n=20`, `max_attempts=3`, mantendo `abort_on_summary_failure=true` e `in_place=true`.
- Testes focados de checkpoint, compactação real com SQLite, persistência de mensagens e parse/effective config dos seis perfis.
- `docs/REVISION_PROTOCOL.md` e este contrato.
- Reload controlado apenas dos gateways Hermes atualmente ativos depois dos gates locais.
- Cadeia canônica de auto-start Windows em `hermes_cli/gateway_windows.py`, artefatos/task actions dos perfis ativos e testes de descendentes reais (`git`, `gh`, `cmd`, `bash`, Node e browser/CUA).
- Retomada de checkpoint/restart: converter `reconcile_required` em reconciliação operacional automática e continuar da `next_action`, pedindo intervenção humana somente quando não houver autoridade consultável ou existir risco irreversível não reconciliável.
- Retomada explícita após falha recuperável: pedidos estritos como "continue de onde parou" devem reutilizar o checkpoint inacabado, preservar o turno original e reativar execução material; é proibido confirmar restauração/progresso com `recovery.restored=false` ou sem avanço verificável do checkpoint.
- Falhas de quota/rate limit: persistir a recuperação e agendar no horário de liberação com tentativas bounded, sem depender de nova mensagem do usuário e sem loop de chamadas ao provedor.
- Lifecycle da compactação: emitir conclusão apenas após commit real; timeout, contenção de lock, cancelamento e falha não podem produzir falso status de sucesso nem deixar novas compactações concorrentes iniciarem.

## Out of Scope
- Roteadores de modelo, credenciais, allowlists, projetos, boards, conteúdo das conversas, bancos fora do round-trip de teste e qualquer projeto de usuário.
- Alterar ou desativar o checkpoint durável, apagar sessões, usar `/reset`, repetir tarefas do usuário ou enviar mensagens de teste a terceiros.
- Reescrever evidências históricas e mudanças preexistentes do worktree.
- Alegar que estado interno invisível do provedor ou efeito externo em voo pode ser serializado: esse estado deve ficar explicitamente `unknown/reconcile_required`, nunca inventado nem repetido automaticamente.

## Failure Signal / Repro
- Em 2026-08-19, Titan/Telegram (17:43), Titan/WhatsApp (17:59 e 18:05) e Project Factory/Telegram (18:18 e 18:21) concluíram a sumarização e falharam imediatamente na retomada.
- Os logs registram em todos os casos `CheckpointConflictError: cannot commit checkpoint: live transcript does not match after transcript`, seguido de falha do rollback pelo mesmo conflito e da resposta genérica do gateway.
- O defeito começou depois do commit `f91f7865b`, que introduziu a validação de hash do checkpoint, e se reproduz quando o transcript contém resultado de ferramenta com `name`.
- Em 19/08, os logs do default e Project Factory registraram 92 gatilhos de compactação; 43 ocorreram a menos de 10 minutos do gatilho anterior. A configuração ativa compactava a 85% para um tail budget de 50% do limiar e admitia dez tentativas por turno.
- Exemplo real: ~242.749 tokens antes da compactação e ~220.943 depois; a redução de ~9% deixou o turno imediatamente próximo do gatilho de 231.200 tokens.
- O Project Factory abriu repetidamente Windows Terminal/`cmd.exe` durante tarefas DOVCRM/RecuperaCli. A atribuição forense mostrou `cmd -> node/bash -> pythonw` sob workers do PF; o gateway e workers foram iniciados por `base uv pythonw.exe`, apesar de a cadeia canônica do upstream usar `wscript.exe -> console python.exe` oculto precisamente para impedir flashes de descendentes.
- Após a primeira correção e 660 s de watchers verdes, o problema voltou. O vídeo de 20:39 mostra uma janela do Windows Terminal permanecendo visível e reaparecendo. O listener atribuiu a ocorrência imediatamente anterior a uma árvore do PF que atravessou o broker interno: `python.exe (gateway) -> pythonw.exe (broker) -> hidden-run.exe -> cmd.exe -> node.exe/powershell.exe`, seguida por novos `WindowsTerminal.exe`/`OpenConsole.exe`.
- Após restart, uma sessão reportou "Sessão restaurada" mas devolveu "O que você quer fazer agora?" em vez de executar a `next_action`/reconciliação persistida.
- Em 20/08 às 08:03, o Project Factory recebeu "Continue de onde parou" após `usage_limit_reached`. O checkpoint novo terminou com `recovery.restored=false`, `resolution=new_turn`, mas o bot afirmou "Retomado do checkpoint" e depois "implementação ... está em execução". Houve um subagente separado que expirou; o turno original não foi restaurado nem continuado.
- Em 20/08, uma compactação de higiene do Titan excedeu 120 s, continuou viva em thread após o timeout e manteve o lock. Três tentativas subsequentes abortaram por `lock_contended`; todas foram mostradas ao usuário como "Context compaction complete", embora nenhuma tivesse concluído. Apenas a tentativa final posterior realizou commit.

## Root-Cause Hypothesis
- Facts: o loop cria resultados de ferramenta com a chave `name`; `transcript_hash()` inclui `name` e `tool_name` como chaves diferentes; a persistência SQLite grava somente `tool_name`, mas os caminhos de insert/flush não promovem `name` para esse campo; o read-back portanto não pode produzir o mesmo hash preparado em memória. Os testes originais usam somente mensagens user/assistant e não cobrem o round-trip com ferramenta.
- Assumptions: outras normalizações de round-trip podem existir; o novo teste deve falhar fechado se aparecerem e a correção deve limitar-se à projeção persistível já suportada.
- Chosen fix point: canonicalizar `name`/`tool_name` como uma única identidade durável, persistir o fallback de `name` em `tool_name` nos dois caminhos de escrita e provar prepare -> archive/read-back -> commit com `SessionDB` real.
- Compaction-thrash cause: `target_ratio=0.50` reserva 115.600 tokens só para a cauda, antes de prompt fixo, ferramentas, cabeçalho e resumo; `protect_first_n=10` fixa ainda mais contexto antigo, e `max_attempts=10` amplifica a repetição no mesmo turno. O `protect_last_n=100` é limitado internamente sob pressão, mas torna a intenção da config enganosa.
- Chosen configuration: manter o gatilho conservador de 85%, mas reduzir o tail budget para 30% do limiar (69.360 tokens), preservar cinco mensagens iniciais e a política oficial de vinte mensagens recentes, limitando a três tentativas. O transcript original continua soft-archived, pesquisável e recuperável sob a mesma sessão.
- Zero-UI root cause, revisão 2: `CREATE_NO_WINDOW`/desktop privado reduziram flashes, mas não isolavam a árvore do desktop enquanto o gateway permanecia na sessão interativa. A cadeia real `python -> pythonw -> hidden-run -> cmd -> node/powershell` podia acionar o terminal delegado do Windows mesmo atravessando o broker.
- Zero-UI root cause estrutural, revisão 3: as tarefas automáticas usavam `InteractiveToken`/`/IT` e executavam um VBS assíncrono. Isso colocava gateway e descendentes na sessão 1 e fazia o Task Scheduler perder ownership do processo real. A correção passa a ser `S4U`/sessão 0 + ação síncrona `base pythonw -> .pyw`, com boot+logon e restart supervisionado; o broker interno mantém a implementação estável de stdio/lifecycle.
- Alternativas rejeitadas: `CREATE_NEW_CONSOLE` + `SW_HIDE` abriu Windows Terminal; o protótipo ConPTY não preservou a interface de stdout/stderr do broker e foi removido antes de qualquer ativação. O isolamento final usa a fronteira de sessão do Windows, que impede descendentes do gateway de materializarem UI no desktop compartilhado.
- Resume root cause: o evento sintético de restart chegava ao agente como um novo turno vazio; `start_turn()` comparava o hash desse vazio com o pedido original e criava um checkpoint novo. Além disso, `build_resume_recovery_note()` mandava transportes interativos relatarem o restore e perguntarem o que fazer. O fix vincula o evento vazio one-shot ao checkpoint inacabado original e exige continuação de `checkpoint.next_action`; efeitos incertos são lidos/reconciliados antes de qualquer retry.
- Explicit-resume root cause: a flag one-shot só era armada para evento sintético vazio. Uma mensagem humana de continuação sempre forçava `_resume_turn_from_checkpoint=false`; `start_turn()` então substituía o checkpoint inacabado por um turno novo. Nenhum gate ligava alegações de "retomado/continuidade ativa" a `recovery.restored=true` ou a avanço material.
- Provider-recovery root cause: `usage_limit_reached` era devolvido como falha comum. O gateway preservava o texto de entrada, mas não persistia `resume_pending` com `not_before`, não agendava wakeup e não retomava quando a quota voltava.
- Compaction lifecycle root cause: `_complete_compaction_lifecycle()` emitia o mesmo status de sucesso em caminhos de exceção, lock contendido e cancelamento. O timeout de higiene cancelava somente a commit fence; a thread continuava executando e segurando o lock enquanto o turno avançava para novas tentativas.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Não desativar o checkpoint nem relaxar o compare-and-swap para aceitar qualquer hash.
- Não apagar, recompactar ou editar sessões reais para fazer o teste passar.
- Não abrir terminal, navegador ou janela visível; reload somente pelos launchers já aprovados e sob monitor zero-UI.
- Não aumentar artificialmente o context window de 272.000 observado no runtime OAuth com base no limite da API direta; não habilitar chave sem consumidor comprovado.

## Validation Plan
- Analyze/lint: `py_compile` nos módulos tocados e `git diff --check`.
- Unit tests: projeção canônica `name`/`tool_name`, escrita atômica/checksum/CAS, recuperação before/after, ferramenta incerta, deliverable e compactação in-place com SQLite real.
- Integration/contract tests: suíte focada de turn checkpoint + compaction + anti-thrash + session sync e parse/effective config dos seis perfis, até 3 ciclos bounded, zero falhas.
- Windows target test: iniciar o gateway pela task canônica e observar descendentes reais por listener/watcher; `VisibleWindows=0`, nenhuma nova janela Windows Terminal atribuível e nenhuma árvore PF órfã após parada planejada.
- Resume target test: interromper um turno com checkpoint em `planning` e outro em ferramenta incerta; o primeiro continua automaticamente e o segundo reconcilia sem perguntar "o que fazer agora" quando existe autoridade consultável.
- Explicit-resume test: checkpoint inacabado + mensagem humana estrita de continuação reutiliza o mesmo `turn_id`, marca `recovery.restored=true` e executa a `next_action`; mensagem comum cria turno novo. Alegação de retomada sem esses fatos deve ser bloqueada.
- Provider recovery test: `usage_limit_reached` persiste `resume_pending/not_before`, agenda exatamente um wakeup bounded e, após sucesso, limpa todo o estado; serialização/restart preservam o contrato.
- Compaction lifecycle test: timeout/lock contention/cancelamento não emitem `COMPACTION_DONE_STATUS`; timeout e lock contendido gravam cooldown durável para impedir novas tentativas enquanto o worker anterior termina; uma compactação com commit emite exatamente um status de conclusão.
- Build/install/deploy checks: nenhuma instalação; source live é o checkout atual. Depois do verde, reload somente das tasks ativas e prova de PID novo/saúde.
- Manual smoke checks: validação passiva dos canais e ausência de novos conflitos de checkpoint; compactação target fica `pending` até ocorrer novamente em turno real, pois não será forçada em sessão do usuário.

## Status
- Contract preflight: validated for zero-UI + automatic-resume scope
- Implementation: concluída no source para explicit-resume, recuperação de quota e lifecycle/concorrência da compactação; correções anteriores de checkpoint hash e launcher Windows permanecem preservadas.
- Validation: `validated-local` nos eixos novos. O classificador de continuação exige checkpoint inacabado, o restore preserva o turno original, confirmações de status são rejeitadas até três vezes e depois viram blocker honesto; quota persiste `not_before` + contador bounded; abortos de compactação não emitem sucesso e gravam cooldown.
- Completion: `validated-local`; `validated-target` aguarda reload drenado do Project Factory. O gateway está executando uma tarefa real no código anterior, portanto não será reiniciado no meio do trabalho do usuário.

## Evidence
- Exact runtime trace: all five reported failures raised `CheckpointConflictError` at the `commit_compaction()` read-back boundary and then `CheckpointWriteError` after the compensating rollback compared the same non-canonical shape.
- Red test: the new three-test repro failed 3/3 before the fix (hash alias, SQLite compaction round-trip, normal flush) and passed 3/3 after it.
- Regression gate: 91 tests passed across turn checkpoint, in-place compaction, persistence, identity flush, legacy compression and gateway failure synchronization.
- Runtime safety state: default e Project Factory foram drenados sem kill e agora são processos `pythonw.exe` diretamente possuídos por tarefas `S4U` em estado `Running`; Exocortex já fornecia a prova local prévia de S4U. Os canais configurados reconectaram. CEOGame não foi iniciado.
- The compare-and-swap remains strict: only `name` and `tool_name` are canonicalized as the same durable tool identity; arbitrary transcript drift is still rejected.
- State-persistence audit: pre-compaction rows are soft-archived (`active=0`) under the same session; checkpoint records before/after hashes before the atomic transcript swap; restart distinguishes swap-not-applied, committed-before-ack and conflict; uncertain tool effects block first exact replay until authoritative reconciliation.
- Resume integration gate: the synthetic empty-event test preserves the original turn id, original user-turn hash and exact `next_action`; the flag is one-shot and cleared before a later genuine user turn.
- Resume target gate: Project Factory scheduled exactly two restart-interrupted sessions; both reused and updated their pre-restart checkpoint files, both executed post-restart tools, and neither emitted the generic restore-and-ask response. At the observation boundary they remained active, so `resume_pending` intentionally remained durable for another crash.
- Windows source/target gate: o instalador e os dois artefatos ativos resolvem `S4U`, `<Hidden>true</Hidden>`, boot+logon e ação direta `base pythonw.exe -> launcher .pyw` com WorkingDirectory. Os XMLs anteriores, o manifesto de migração e a ponte administrativa one-shot ficam preservados sob `C:\Users\maiko\agent-ops\repairs\hermes-compaction-checkpoint-20260819`; o VBS default foi restaurado byte a byte após o registro.
- Zero-UI target evidence final: PF PID 36616/SessionId 0, Telegram connected, com cadeia real `pythonw -> hidden-run -> cmd -> conhost/node` inteira na sessão 0 e watcher 60 s em zero; Titan PID 32688/SessionId 0, WhatsApp+Telegram connected, bridge Node na sessão 0 e watcher 90 s em zero. As tarefas permanecem `Running`, provando ownership e habilitando restart-on-failure.
- Revocation histórica preservada: o vídeo das 20:39 e o WindowOriginListener provaram que os 660 s antigos eram falso-verde para a fronteira interativa. Eles justificam a migração de sessão e não são usados como evidência do estado final.
- Explicit-resume local: suíte integrada revision-bound de checkpoint, restart-resume, hygiene e compaction passou 199/199; o recorte final do cooldown de lock passou 1/1.
- Provider recovery local: `resets_in_seconds` é convertido em epoch, serializado como `resume_not_before`, o wakeup é único por sessão, a tentativa só é consumida com adapter e autorização válidos e o estado é limpo somente após sucesso.
- Estado alvo no fechamento local: Project Factory PID 36616 permanece vivo na sessão 0, Telegram conectado e checkpoints continuam sendo atualizados pelo trabalho que o usuário retomou diretamente. Um monitor read-only de 30 minutos nunca observou `active_agents=0`; o turno Telegram e seu subagente continuaram criando workers e avançando checkpoints. Nenhum reload foi disparado durante esse turno ativo.
