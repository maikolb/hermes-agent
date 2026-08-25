# Hermes Revision Protocol

Autoridade canônica de regressões operacionais deste checkout.

## REG-2026-08-24-001 — Kanban concludes before Git delivery is proven

- Status: root cause confirmed; correction and target validation pending.
- Cenário: Project OS grava `done` antes de provar commit, push, PR, checks,
  merge e ancestralidade em `main`; a limpeza posterior é best-effort e não
  possui retry owner.
- Causa raiz: Git delivery is not part of the task state machine. Reachability
  from any remote is incorrectly treated as sufficient cleanup evidence, and
  manually named worktrees are outside canonical Kanban ownership.
- Invariante da correção: `delivery_verified` sealed receipt → canonical safe
  prune → `done`; failure remains `completion_blocked_delivery`; no force
  removal and no globally hardcoded remote/project.
- Prevenção/commands: see `docs/regressions/REG-2026-08-24-001.md`.

## REG-2026-08-24-002 — Windows PTY reports false EOF success

- Status: validated-local; candidate merge and target acceptance pending.
- Cenário reproduzível: `close_stdin()` em PTY Windows devolvia sucesso, mas o
  processo bloqueado em `sys.stdin.read()` nunca recebia EOF nem encerrava.
- Causa raiz: `pywinpty.PtyProcess.sendeof()` escreve Ctrl-D; não fecha o pipe
  de entrada do ConPTY. O teste POSIX de half-close não tinha marker de SO.
- Correção: operação Windows agora falha fechado e não chama `sendeof()` nem
  `close()`; o E2E de EOF é POSIX-only e há teste Windows do erro explícito.
- Prevenção vinculada: `docs/regressions/REG-2026-08-24-002.md` e
  `tests/tools/test_process_registry.py::TestStdinHelpers`.
- Comando: `.venv\Scripts\python.exe -m pytest -q tests/tools/test_process_registry.py tests/tools/test_code_execution.py`.
- Evidência: 90 passed, 48 skipped, 5 subtests passed.

## REG-2026-08-24-003 — Archived Kanban subscription is never removed

- Status: validated-local; candidate merge/target pending.
- Causa: `archived` era claimed como focus/control, mas filtrado antes da
  delivery silenciosa que avança cursor e remove a assinatura.
- Correção: archive-only atravessa o pipeline sem mensagem; completed+archive
  entrega a conclusão uma vez e depois remove a assinatura.
- Prevenção: `docs/regressions/REG-2026-08-24-003.md`; Project OS 297/297.

## REG-2026-08-24-004 — Exact-call redirect races with concurrent call

- Status: validated-local; candidate merge pending.
- Causa: threshold de uma assinatura exata era persistido como redirect da
  ferramenta inteira, tornando lote concorrente dependente da ordem de thread.
- Correção: exact/no-progress redirect por canonical signature; escalonamento
  próprio da ferramenta permanece separado.
- Prevenção: `docs/regressions/REG-2026-08-24-004.md`; suite 23/23 e 20 reruns.

## REG-2026-08-24-005 — Production tool effects are not durably fenced against replay

- Status: open; root cause confirmed; correction in progress; hard-process
  crash/restart gate pending.
- Cenário: a produção executa um efeito real e morre com `os._exit(88)` antes
  do retorno. O efeito existe, mas o checkpoint auditado permanece em
  `planning`, sem call pending/unknown; o replay guard isolado bloqueia a mesma
  incerteza apenas uma vez por processo.
- Causa: as APIs de attempt/result não estavam ligadas ao effect boundary de
  `agent/tool_executor.py`, e a autorização de replay dependia de estado
  volátil em vez de reconciliação durável.
- Invariante: exact post-middleware args persistidos antes do efeito; resultado
  após flush canônico; unknown permanece bloqueado até readback autoritativo.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-005.md`.

## REG-2026-08-24-006 — Delivery replay can reset proof or acknowledge the wrong turn

- Status: open; root cause confirmed; correction in progress; exact-once não
  reivindicado.
- Cenário: regravar o mesmo obligation ID entregue com `INSERT OR REPLACE`
  volta a deixá-lo retryable; um ACK antigo pode terminalizar o checkpoint de
  um turno novo; acceptance remota sem ACK local pode ser reenviada.
- Causa: identidade substituível, transições não monotônicas e ausência de
  fence `(turn_id, deliverable_revision, content_sha256)` entre ledger e
  checkpoint; estado ambíguo era tratado como retry normal.
- Invariante: insert-once, transições condicionais, fence exato e
  `delivery_ambiguous` sem resend automático.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-006.md`.

## REG-2026-08-24-007 — Ordinary final response is not an exact replayable checkpoint

- Status: open; root cause confirmed; correction in progress; fresh-process
  final-delivery replay pending.
- Cenário: SessionDB contém user+assistant de um final comum, mas o checkpoint
  segue em `planning` sem deliverable. Após queda, o loop chama o modelo de
  novo em vez de repetir o payload final exato.
- Causa: o final comum não era selado após fill/plugins/footer/sanitização e a
  recuperação não tinha branch `pending exact final -> skip model`.
- Invariante: tail exata flushed + `ordinary_final` revision/hash antes do
  envio; restart reutiliza o payload sem nova inferência.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-007.md`.

## REG-2026-08-24-008 — Windows zero-UI broker depends on import order

- Status: validated-local; candidate merge/target pending.
- Cenário: o teste de recuperação passava na matriz multi-file, mas falhava
  isolado antes de criar o primeiro filho com `KeyError: 'args'`.
- Causa: quando importado depois do guard de testes, o broker capturava
  `Popen(cmd, *args, **kwargs)` e assumia que o bind continha a chave stdlib
  `args`; import anterior na matriz mascarava a premissa.
- Correção: normalização usa a assinatura concreta `Popen(args, ...)` derivada
  pela MRO, mas o wrapper de política capturado continua sendo o executor.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-008.md`; matriz 224/224
  executados, um skip Linux-only declarado, e fronteira 20/20 em processos
  pytest novos.

## REG-2026-08-24-009 — Compaction checkpoint seals the pre-salvage transcript

- Status: validated-local no cenário exato; matriz completa e target pendentes.
- Cenário: a compactação durável produzia um candidato quase sem redução; o
  anti-growth salvava um transcript menor depois de o checkpoint já ter selado
  o hash anterior, causando `CheckpointConflictError` no commit.
- Causa raiz: `prepare_compaction()` era executado antes da decisão final de
  crescimento/salvage, enquanto SQLite persistia o transcript pós-salvage.
- Correção: preparar a fase 2 somente depois da decisão anti-growth e de todo
  salvage, imediatamente antes da mutação durável.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-009.md` e o cenário exato
  `TestInPlaceAntiGrowthGuard::test_in_place_salvages_near_break_even_growth`;
  reprodução adjacente 3/3 verde após a correção.

## REG-2026-08-24-010 — Terminal imports local backend before the Windows zero-UI boundary

- Status: causa raiz confirmada; correção local aplicada; matriz/target pendentes.
- Cenário: os três testes do limite terminal falhavam porque
  `_install_windows_terminal_zero_ui_boundary()` não existia e
  `LocalEnvironment` era importado diretamente.
- Causa raiz: o commit rebased preservado `8b76dee159` carregava os testes, mas
  dependia de um hunk de produção presente apenas na linhagem anterior
  `2173defe1c`, que não foi consolidada.
- Correção: instalar e verificar o broker antes de importar o backend local;
  falhar fechado no Windows e manter no-op fora dele.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-010.md` e
  `tests/tools/test_terminal_zero_ui_boundary.py`.

## REG-2026-08-24-011 — Honcho cache retains the previous configured backend

- Status: validated-local no cenário exato; SDK/serviço real e target pendentes.
- Cenário: um gateway longo alterava `honcho.base_url` no `config.yaml`, mas
  recebia o cliente já conectado ao endpoint anterior.
- Causa raiz: a chave de cache era calculada antes de resolver a URL efetiva
  herdada do arquivo; um cache hit impedia o factory de observar a mudança.
- Correção: resolver URL/timeout antes do lookup, incluí-los na identidade e
  usar exatamente os mesmos valores na construção do SDK.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-011.md` e
  `TestGetHonchoClient::test_config_yaml_base_url_change_rebuilds_long_lived_client`;
  reprodução 1/1 e matriz focada 59 passed, 16 skips condicionais ao SDK.

## REG-2026-08-24-012 — Archiving a running task orphans its worker

- Status: causa raiz confirmada; correção e validação pendentes.
- Cenário: arquivar uma tarefa running apagava PID/claim e encerrava o run, mas
  não terminava o processo; em worktree preservada, ele seguia mutando sem
  owner no ledger.
- Causa raiz: `archive_task()` limpava a identidade antes de capturá-la e nunca
  chamava `_terminate_reclaimed_worker()` após o commit.
- Invariante: snapshot exato de PID/claim/start dentro da transação, audit trail
  durável e término pós-commit; falha de término nunca autoriza apagar checkout.
- Prevenção/commands: `docs/regressions/REG-2026-08-24-012.md`; direct e
  dashboard single/bulk archive com worker real/spies e restart reconciliation.

## REG-2026-08-17-001 — Titan pareado/conectado sem responder após reboot

- Status: closed — validated-target
- Cenário reproduzível: após queda de energia e novo pareamento, a tarefa `Hermes_Gateway` iniciava, mas o bridge Node lançado pelo gateway morria com exit 1. Um bridge Node iniciado diretamente ficava `connected`, porém sem a allowlist do perfil e recusava inbound. Quando o inbound finalmente chegou, uma saudação simples caiu em Sol/High e tentou compactar uma sessão antiga de 392 mensagens (~330 mil tokens).
- Causa raiz: o broker Windows devolvia ao adapter o PID/lifecycle do runner `pythonw`, não do Node long-lived que o adapter precisa monitorar, registrar e encerrar. O processo temporário não carregava o ambiente do perfil. Em paralelo, `smart_model_routing.platforms` continha apenas `telegram`, deixando WhatsApp no modelo baseline.
- Correção: capacidade interna `direct_hidden_child_env()` para subprocessos long-lived manterem PID/stdio/lifecycle reais sob desktop privado + Job Object; adapter WhatsApp opta explicitamente por essa capacidade. Perfil default inclui `whatsapp` no smart model router.
- Prevenção vinculada:
  - `tests/test_windows_process_broker.py::test_direct_hidden_child_marker_is_capability_scoped`
  - `tests/gateway/test_whatsapp_connect.py::TestWindowsBridgeLifecycle`
  - `scripts/whatsapp-bridge/bridge.native.test.mjs`
  - `docs/EXECUTION_CONTRACT.md`
- Comando de validação: Python 3.11 com `venv/Lib/site-packages` em `PYTHONPATH`, executando `pytest tests/test_windows_process_broker.py tests/gateway/test_whatsapp_connect.py -q`; depois restart real de `Hermes_Gateway`, `/health=connected`, PID file igual ao listener Node e monitor com `VisibleWindows=0`.
- Evidência final: 43 testes broker/WhatsApp/setup + 1 contrato zero-UI passaram; restart real passou; tarefa está `Running`; bridge está `connected`; allowlist e modo bot estão configurados; zero janelas novas em 20 s. Inbound novo foi roteado para Luna/Low, resposta ficou pronta em 11,4 s e o ledger marcou a entrega como `delivered`.

## REG-2026-08-18-001 — Roteamento rápido sem quality gate empírico

- Status: open — mitigation validated-local; empirical calibration pending
- Cenário reproduzível: uma saudação curta era automaticamente enviada para Luna/Low ou Haiku/Low, e tarefas comuns para Terra/Sonnet, apenas pelos sinais estruturais/lexicais do pedido. Os testes provavam a matriz e a velocidade do classificador, mas não comparavam a qualidade das respostas com Sol/Fable em tarefas representativas.
- Causa raiz: o primeiro desenho tratou complexidade estimada como autorização de downgrade e usou o microbenchmark local (~tempo de decisão) como evidência de desempenho do roteador. Isso não mede acurácia, cobertura de edge cases, qualidade do resultado nem calibração do reasoning.
- Mitigação imediata: ausência de política de benchmark revision-bound agora falha fechado para GPT-5.6 Sol/XHigh e Claude Fable/XHigh. Escolha explícita do usuário continua soberana. As seis configurações declaram `quality_policy: conservative`.
- Hardening: a palavra `benchmarked` na configuração não é autoridade. O código contém uma allowlist de hashes de benchmark intencionalmente vazia; só uma revisão de fonte que fixe a evidência aprovada pode habilitar rota menor.
- Correção definitiva planejada: benchmark global sanitizado por classe de tarefa, com repetição, gates determinísticos, avaliação cega e comparação de latência somente entre rotas que atingirem o mesmo quality bar. Rotas menores serão allowlisted por classe e vinculadas ao hash do router/harness/evidência; qualquer drift/expiração volta ao fail-safe forte.
- Prevenção vinculada:
  - `tests/gateway/test_smart_model_routing.py::test_missing_benchmark_policy_fails_closed_to_strongest_lane`
  - `tests/gateway/test_smart_model_routing.py::test_benchmarked_label_without_revision_bound_hash_still_fails_closed`
  - `docs/EXECUTION_CONTRACT.md`
  - harness global de qualidade a ser criado em `C:\Users\maiko\agent-ops\benchmarks\hermes-model-router-quality-20260818`
- Comando de validação atual: `venv\Scripts\python.exe -m pytest -q tests\gateway\test_smart_model_routing.py`; verde definitivo exige também o benchmark revision-bound e smoke do runtime consumindo sua allowlist.
- Evidência atual: 30 testes focados verdes; nenhuma rota automática menor é autorizada enquanto o benchmark permanece pendente.

## REG-2026-08-19-001 — Compactação conclui e a retomada do turno cai em erro

- Status: mitigated — implementation and runtime load validated; next natural target compaction pending
- Cenário reproduzível: um transcript durável contendo resultado de ferramenta no formato live (`role=tool`, `name=<tool>`) é compactado in-place. A sumarização termina, mas o read-back do SQLite não produz o hash `after` preparado; o rollback sofre a mesma divergência e o gateway responde `unexpected error`.
- Ocorrências no alvo: Titan/Telegram às 17:43, Titan/WhatsApp às 17:59 e 18:05, e Project Factory/Telegram às 18:18 e 18:21 de 19/08/2026.
- Causa raiz: o checkpoint introduzido em `f91f7865b` hasheia `name` e `tool_name` como campos distintos, enquanto o SQLite só persiste `tool_name`; os caminhos de escrita não promovem o `name` emitido pelo loop. Os testes do checkpoint usavam apenas mensagens user/assistant e não exercitavam esse round-trip real.
- Correção aplicada: a projeção do checkpoint canonicaliza `name`/`tool_name` como a mesma identidade durável; os caminhos de insert/flush promovem o `name` live para a coluna `tool_name`; o compare-and-swap continua estrito para todo o restante do transcript.
- Prevenção vinculada:
  - `tests/agent/test_turn_checkpoint.py` para equivalência canônica.
  - `tests/run_agent/test_turn_checkpoint_compaction.py` para prepare -> SQLite archive/read-back -> commit com resultado de ferramenta real.
  - teste de persistência do `SessionDB`/flush para impedir nova perda do nome.
  - `docs/EXECUTION_CONTRACT.md`.
- Comando de validação: Python do `venv`, com a suíte focada de turn checkpoint, compaction, persistence, identity flush, legacy compression e gateway compression session sync; depois reload controlado dos gateways ativos e inspeção passiva dos logs por novos `CheckpointConflictError`.
- Evidência: repro vermelho 3/3 antes da correção; 3/3 depois; gate ampliado 91/91; `py_compile` e `git diff --check` verdes. Os três gateways-alvo (default, Exocortex e Project Factory) foram recarregados sob monitor (`VisibleWindows=0`), todos os canais configurados reconectaram e não houve novo `CheckpointConflictError`/`CheckpointWriteError` nas janelas pós-start.
- Aceite pendente: a próxima compactação disparada naturalmente em uma conversa real deve emitir o status de conclusão e continuar o turno sem a resposta genérica de erro; nenhuma sessão real foi forçada ou alterada só para fabricar esse aceite.

## REG-2026-08-19-002 — Compactações repetitivas com pouca folga pós-resumo

- Status: mitigated — repair validated-local; natural target compaction pending
- Cenário reproduzível: perfis ativos com `threshold=0.85`, `target_ratio=0.50`, `protect_first_n=10`, `protect_last_n=100` e `max_attempts=10` voltam ao gatilho poucos minutos depois de compactar. Nos logs de 19/08, default e Project Factory somaram 92 gatilhos; 43 intervalos ficaram abaixo de dez minutos.
- Evidência concreta: um turno do default foi de ~242.749 para ~220.943 tokens, redução aproximada de apenas 9%, restando a ~10.257 tokens do limiar real de 231.200.
- Causa raiz: `target_ratio` dimensiona apenas a cauda preservada e reservava 115.600 tokens antes de somar prompt fixo, schemas de ferramentas, cabeçalho, mensagens iniciais e resumo. Dez mensagens iniciais literais aumentavam o piso e dez tentativas permitiam thrash no mesmo turno. `protect_last_n=100` ainda era limitado internamente a oito sob pressão, tornando a configuração enganosa sem oferecer a proteção aparente.
- Correção aplicada: seis perfis com `threshold=0.85`, `target_ratio=0.30`, `protect_first_n=5`, `protect_last_n=20`, `max_attempts=3`, `abort_on_summary_failure=true`, `in_place=true`. Isso mantém 69.360 tokens de cauda recente antes do resumo, acima do default oficial de 20%, mas cria folga material. Os três gateways-alvo foram recarregados e consomem a configuração nova; CEOGame permanece sem start nesta validação.
- Invariante de continuidade: transcript original soft-archived e recuperável; compactado ativo; checkpoint atômico/checksummed com hashes before/after, próxima ação, ferramentas incertas, verificação e entrega. Efeito externo em voo nunca é presumido: fica `reconcile_required` e bloqueia o primeiro replay idêntico.
- Prevenção vinculada: testes de checkpoint/SQLite/anti-thrash, validação efetiva dos seis YAMLs e `docs/EXECUTION_CONTRACT.md`.
- Comando de validação: parser YAML sanitizado para os seis perfis; pytest focado de `test_turn_checkpoint.py`, `test_turn_checkpoint_compaction.py`, `test_compaction_anti_thrash.py`, `test_compression_anti_thrash_persistence.py` e session sync; depois reload controlado dos perfis ativos sob watcher zero-UI.
- Aceite pendente: observar compactação natural concluindo e retomando o turno, com folga pós-compactação material e sem novo gatilho em menos de dez minutos salvo crescimento real do pedido.
- Regressão de 20/08: uma compactação de higiene excedeu o timeout, continuou viva segurando o lock e três tentativas concorrentes abortaram. O lifecycle exibiu "Context compaction complete" também nos abortos. A configuração reduziu pressão, mas não corrigiu a concorrência nem a semântica do status; por isso a entrada foi reaberta.
- Correção de 20/08: somente commit real emite `COMPACTION_DONE_STATUS`; exceção, cancelamento, timeout e lock contendido não podem mais gerar o falso sucesso. Timeout de higiene e lock contendido gravam cooldown durável na sessão; lock contendido usa 300 s, alinhado à janela de higiene do PF, bloqueando o ciclo de tentativas enquanto o worker anterior termina.
- Evidência local nova: suíte focada de hygiene + compaction passou 78/78; gate integrado revision-bound passou 199/199; recorte final do cooldown de lock passou 1/1. Aceite continua dependente da próxima compactação natural após reload.

## REG-2026-08-19-003 — Descendentes do Hermes abrem Windows Terminal/CMD na área de trabalho

- Status: reopened — source mitigation loaded on PF; prolonged real-work target pending
- Cenário reproduzível: tarefas longas do Project Factory (incluindo DOVCRM/RecuperaCli) criam cadeias `pythonw -> bash/node -> cmd/conhost`; o Windows 11 materializa Windows Terminal repetidamente na área de trabalho do usuário. Parar somente a Scheduled Task deixou workers órfãos ativos.
- Causa raiz comprovada, revisão 1: a ativação local substituiu a cadeia canônica do upstream por `base uv pythonw.exe`. Como o gateway não possuía console herdável, netos console-subsystem podiam abrir um console delegado ao Windows Terminal.
- Mitigação anterior: `hermes_cli/gateway_windows.py` voltou à cadeia `wscript.exe //B -> .vbs -> venv console python.exe` em window style 0. Isso protegeu o launcher raiz e passou em 660 s de janelas bounded, mas foi encerrado cedo demais como solução da classe inteira.
- Recorrência comprovada: o vídeo de 20:39 registra Windows Terminal persistente/reaparecendo. O WindowOriginListener atribuiu a ocorrência a uma árvore do Project Factory que atravessou `python.exe (gateway) -> pythonw.exe (broker) -> hidden-run.exe -> cmd.exe -> node.exe/powershell.exe`, seguida por `WindowsTerminal.exe` e `OpenConsole.exe` novos.
- Falso-verde do monitor: o watcher usado nos 660 s anteriores procurava o nome de processo `wt`, mas o host materializa `WindowsTerminal.exe` e `OpenConsole.exe`. Assim, `VisibleWindows=0` não cobria a janela que o usuário via. O watcher foi ampliado para os nomes reais antes de qualquer nova reivindicação target.
- Causa raiz comprovada, revisão 2: `hermes_cli/windows_process_broker.py` entrega subprocessos normais a `base pythonw.exe`; `windows_process_runner.py` aplica `CREATE_NO_WINDOW`; e `hidden-run.exe` aplica `CreateNoWindow=true`. Esses mecanismos reduzem flashes, mas não constituem isolamento do desktop: enquanto o gateway estava na sessão interativa, um descendente que exigisse console ainda podia acionar o terminal delegado do Windows.
- Causa raiz estrutural, revisão 3: o instalador registrava os gateways automáticos com `InteractiveToken`/`schtasks /IT`, e a ação `wscript -> VBS` iniciava o processo real de forma assíncrona. Assim, toda a árvore permanecia na sessão 1 do usuário e a tarefa ficava `Ready` sem possuir o lifetime do gateway; nenhum patch por ferramenta conseguia garantir que um neto futuro não alcançaria o desktop.
- Correção aplicada: `hermes_cli/gateway_windows.py` agora gera tarefas `S4U`, ocultas, com gatilhos de boot e logon, ação síncrona `base pythonw.exe -> launcher .pyw`, `WorkingDirectory` explícito e restart supervisionado. Default e Project Factory foram registrados nesse formato, drenados sem kill e reiniciados; o Task Scheduler agora permanece `Running` e possui o processo real na sessão 0.
- Alternativas rejeitadas: `CREATE_NEW_CONSOLE` + `SW_HIDE` abriu Windows Terminal; o experimento ConPTY perdeu a compatibilidade do stdio existente e foi removido antes da ativação. A fronteira final é isolamento de sessão do sistema operacional, preservando o broker interno estável para stdio/lifecycle.
- Prevenção vinculada: `hermes_cli/gateway_windows.py`, launchers `.pyw`, testes focados de task XML/installer, `hermes_cli/windows_process_broker.py`, `hermes_cli/windows_process_runner.py`, WindowOriginListener e watcher com `WindowsTerminal`/`OpenConsole`.
- Comando de validação: testes focados de Scheduled Task + broker estático; depois reload drenado do PF/default e inspeção de task/PID/SessionId/canais, exigindo `S4U`, tarefa `Running`, toda a árvore na sessão 0 e `VisibleWindows=0`.
- Evidência alvo: PF PID 36616, Telegram connected, e cadeia real `pythonw -> hidden-run -> cmd -> conhost/node` integralmente na sessão 0; Titan PID 32688 com bridge `pythonw -> node` na sessão 0, WhatsApp e Telegram connected. Watchers simultâneos retornaram zero janelas por 60 s (PF) e 90 s (Titan). Nenhum gateway CEOGame foi iniciado.
- Recorrência de 20/08: o usuário voltou a observar flashes curtos de CMD atribuídos ao PF durante trabalho DOVCRM. O listener não reteve uma janela longa o suficiente para atribuição por HWND, mas a árvore PF continuou criando `cmd/conhost/node` sob workers. A entrada permanece reaberta até o PF carregar o broker/guard atual e passar observação prolongada durante trabalho real; não será encerrada por polling curto.

## REG-2026-08-19-004 — Checkpoint é detectado, mas o agente pede ao usuário para recomeçar

- Status: reopened — wiring regression loaded on Titan/PF; new behavioral target evidence pending
- Cenário reproduzível: após restart, o bot informa "Sessão restaurada com sucesso", reconhece que o turno anterior foi interrompido e pergunta "O que você quer fazer agora?" em vez de continuar da ação persistida.
- Causa raiz comprovada: o evento sintético de restart chegava como novo turno vazio e não podia reutilizar o checkpoint original por hash; além disso, a nota de recuperação instruía transportes interativos a narrar o restore e perguntar o próximo passo.
- Correção aplicada: o evento vazio de auto-resume recebe uma flag one-shot, reutiliza o checkpoint inacabado original independentemente do hash vazio e continua da `next_action`; efeitos incertos exigem readback/reconciliação autoritativa antes de retry. A nota proíbe narrar restore e só permite pergunta quando a autoridade inexiste ou o risco irreversível não pode ser reconciliado.
- Contrato correto: estado conhecido continua da `next_action`; resultado concluído é consumido; efeito incerto é reconciliado automaticamente no sistema autoritativo; pergunta humana só é permitida quando não existe autoridade consultável ou há risco irreversível que não pode ser reconciliado.
- Prevenção vinculada: teste de restart em `planning`, teste de ferramenta incerta com reconciliador e asserção negativa contra mensagens genéricas de "o que fazer agora" quando a recuperação é determinística.
- Evidência alvo: no primeiro restart do PF após o patch, duas sessões `resume_pending` foram agendadas. Ambas atualizaram os checkpoints preexistentes e executaram ferramentas pós-restart (pelo menos 3 e 7 concluídas na primeira janela); o scan das respostas não encontrou a mensagem genérica de restore/pergunta e não houve `unexpected error`.
- Recorrência de 20/08: a integração feita pelo Titan no checkout vivo manteve os helpers de retomada, mas perdeu o call site que armava as flags one-shot no `TurnRunner`. Testes isolados continuavam verdes e não protegiam o fluxo produtivo. O call site foi restaurado e um teste de wiring agora falha se helpers/flags voltarem a ficar órfãos.

## REG-2026-08-20-001 — Pedido explícito de continuação produz falsa retomada

- Status: mitigated — repair loaded on Titan/PF; behavioral target pending
- Cenário reproduzível: depois de uma falha `usage_limit_reached`, o usuário envia "Continue de onde parou". O bot responde "Retomado do checkpoint" e depois declara atividades em execução, mas não reativa o turno interrompido.
- Evidência: o checkpoint escrito após a mensagem tem `recovery.restored=false`, `recovery.resolution=new_turn` e novo `turn_id`; a resposta visível afirma o oposto. Um subagente separado foi criado e expirou, sem converter o turno original em continuidade real.
- Causa raiz: o gateway só armava `_resume_turn_from_checkpoint` para eventos sintéticos vazios de restart. Mensagens humanas não vazias, inclusive continuação explícita, forçavam turno novo e substituíam o checkpoint ativo. Na regressão posterior, até o wiring do evento sintético desapareceu: o helper e o guard existiam, mas não eram chamados pelo `TurnRunner`.
- Falha adjacente: `usage_limit_reached` não persiste horário de liberação nem agenda retomada; exige nova mensagem humana e ainda assim abre turno novo.
- Correção aplicada: classificador estrito de intenção de continuação condicionado a checkpoint inacabado; restore one-shot do checkpoint original; retomada bounded no horário de liberação; gate que rejeita respostas de status como "retomado", "continuidade ativa" e "está em execução". Após três recusas, o bot devolve blocker honesto e mantém o checkpoint recuperável em vez de alegar trabalho inexistente.
- Recuperação de cota: `resets_in_seconds` vira `resume_not_before`; o estado persiste no índice de sessões, sobrevive restart, agenda um único wakeup e limita a três tentativas automáticas. Adapter offline ou usuário não autorizado não consomem tentativa; sucesso limpa o estado.
- Prevenção vinculada: testes focados de explicit-resume, provider recovery persistida e falsa confirmação; `docs/EXECUTION_CONTRACT.md`.
- Comando de validação: pytest focado nos módulos de checkpoint/restart-resume/provider failure, seguido de reload controlado do PF e observação passiva do runtime sem mensagem sintética a terceiros.
- Evidência local: 121/121 nos testes históricos de explicit-resume + checkpoint; recorte revision-bound final com checkpoint/restart/notifier/activity em 83/83, incluindo teste que exige o call site produtivo. Titan/default e PF foram recarregados e estão conectados; ainda falta uma retomada real com avanço material pós-reload para `validated-target`.

## REG-2026-08-20-002 — Agente altera o checkout que sustenta o próprio runtime

- Status: mitigated — guards validated-local; loaded on Titan and PF
- Cenário reproduzível: durante um turno longo, o Titan executa merge/cherry-pick e edições no checkout do qual o gateway e workers importam módulos. O processo vivo passa a combinar código carregado antes do merge com arquivos novos no disco; call sites ficam sem definições e mensagens subsequentes caem antes do modelo.
- Evidência: o branch do runtime foi trocado para uma integração local com 22 commits; houve conflito/syntax error transitório. Depois, o fluxo carregado chamou `_resolve_activity_indicator_settings` ausente e o explicit-resume ficou com helper sem call site. Três stashes preservam, separadamente, o dirty state anterior e as automutações tardias; nenhum foi descartado.
- Causa raiz: as proteções Git existentes não reconheciam corretamente caminhos Windows e as ferramentas de arquivo podiam escrever no próprio checkout quando executadas dentro do gateway.
- Correção: normalização Windows no self-repo guard; operações Git destrutivas contra o source vivo são bloqueadas; `write_file`/`patch` falham fechado quando `_HERMES_GATEWAY=1` e o alvo pertence ao checkout Hermes. Manutenção deve ocorrer em worktree/clone e entrar apenas por cutover drenado.
- Prevenção vinculada: `tools/self_repo_guard.py`, `tools/file_tools.py`, `tests/tools/test_self_repo_guard.py`, `tests/tools/test_file_tools.py` e este contrato.
- Evidência local: self-repo guard 118/118; teste estreito de escrita/patch do gateway 2/2; py_compile e diff-check verdes. Titan e PF carregaram o guard após restart drenado.

## REG-2026-08-20-003 — Kanban executa em segundo plano sem anunciar início material

- Status: mitigated — validated-local and loaded on PF; notifier target pending
- Cenário reproduzível: um card é enfileirado atrás de uma dependência e o bot promete continuar automaticamente. Quando o dispatcher realmente assume o card, o tópico não recebe mensagem; o usuário precisa cobrar para descobrir se ainda está na fila ou já roda.
- Evidência DOVCRM: a dependência concluiu às 16:58:59 e o hotfix recebeu `claimed` às 16:59:47, com worker e heartbeat reais; concluiu às 17:30:58. O assinante permaneceu com cursor zero até o evento final porque o watcher consultava somente eventos terminais.
- Causa raiz: `_kanban_notifier_watcher()` excluía `claimed` do conjunto notificável. Heartbeats e comentários eram persistidos, mas o primeiro sinal visível era conclusão/bloqueio.
- Correção: `claimed` passa a emitir exatamente um aviso compacto de início; `heartbeat` continua excluído para não gerar spam por minuto. A mensagem só existe após claim real, nunca por mera fila/promessa.
- Prevenção vinculada: `tests/gateway/test_kanban_notifier.py::test_claimed_task_notifies_only_after_material_start`.
- Comando de validação: pytest focado de notifier + checkpoint/restart/activity; depois reload drenado do PF e observação de um próximo card real.
- Evidência local: 83/83 no gate final. O cutover foi adiado enquanto writers DOVCRM estavam ativos e só ocorreu após duas leituras consecutivas com zero writers/zero agentes; PID novo, Telegram conectado e zero janelas foram confirmados. O próximo evento `claimed` real ainda é o aceite do notifier no alvo.

## REG-2026-08-20-004 — CEOGame responde erro por mistura de módulos do runtime vivo

- Status: mitigated — current source validated-local; gateway reload validated-target; natural message pending
- Cenário reproduzível: uma mensagem nova no tópico CEOGame chega normalmente ao gateway, mas a criação do agente cai antes da primeira chamada ao modelo com `ImportError` para `CHECK_FN_CACHE_BYPASS` e devolve a resposta genérica de erro.
- Evidência: o PID antigo nasceu antes da integração atual; no disco, `tools.registry` já exporta o símbolo e um processo Python limpo importa `model_tools` com sucesso. Isso exclui falha do provedor de imagem como causa do erro mostrado.
- Causa raiz: o checkout que sustenta o runtime foi alterado enquanto o gateway permanecia vivo. O processo reteve módulos antigos em `sys.modules` e passou a importar arquivos novos sob demanda, formando uma combinação incompatível.
- Correção: reload controlado do perfil CEOGame somente depois de `active_agents=0`, pela tarefa S4U/launcher `pythonw` canônico e sob gate zero-UI. O bloqueio de automutação do checkout impede a mesma classe nos runtimes recarregados; manutenção futura deve entrar por cutover drenado.
- Prevenção vinculada: `tools/self_repo_guard.py`, `tools/file_tools.py`, testes desses guards, import smoke de `model_tools` e `docs/EXECUTION_CONTRACT.md`.
- Comando de validação: import limpo de `model_tools`/`CHECK_FN_CACHE_BYPASS`; helper `restart-hermes-profile-gateway-zero-ui.py --profile hermes-ceogame --platform telegram`; inspeção passiva do resultado e do PID novo.
- Evidência alvo: PID novo vivo, task `Running`, `gateway_state=running`, Telegram `connected`, log de subida pronto, `VisibleWindows=0`, resultado `ok=true`. A próxima mensagem natural permanece o aceite comportamental do import lazy no processo alvo; nenhuma mensagem de teste foi enviada pelo agente.

## REG-2026-08-20-005 — Notifier Kanban despeja backlog e retries no tópico

- Status: mitigated — PF recarregado sem replay; próximo evento real pendente
- Cenário reproduzível: reiniciar o Project Factory com assinaturas antigas faz o watcher publicar, no mesmo minuto, uma bolha por transição de cada card (`claimed`, timeout, retry, crash, desistência e conclusão), seguida depois pelo resumo legítimo do saneamento.
- Causa raiz comprovada: o notifier misturava duas autoridades distintas. Depois da notificação passiva por `adapter.send()`, eventos terminais chamavam `deliver_wake()` e entravam no pipeline normal como nova mensagem do tópico. No restart de 19:31 isso converteu 33 eventos históricos em turnos; depois do baseline, outros cinco eventos novos ainda acordaram agentes. Em paralelo, o startup aceitava `resume_pending` sem exigir checkpoint durável e retomou duas sessões interrompidas.
- Invariante: Telegram recebe estado operacional, não event-log nem instrução sintética. Notificação nunca executa agente por padrão; eventos anteriores ao boot são consumidos silenciosamente; restart só executa trabalho respaldado por checkpoint íntegro, inacabado e com `next_action`.
- Correção: `kanban.agent_wake_on_events=false` por padrão e explicitamente no PF; backlog anterior ao boot/assinatura é suprimido com avanço atômico do cursor; auto-resume exige checkpoint durável; `filelock` virou dependência direta do runtime de checkpoints.
- Prevenção vinculada: `gateway/kanban_watchers.py`, `gateway/run.py`, `hermes_cli/config_defaults.py`, `pyproject.toml`, `uv.lock`, testes focados e `docs/EXECUTION_CONTRACT.md`.
- Comando de validação: `pytest` do notifier/restart/reconnect, parse da configuração PF, `py_compile` e `git diff --check`; depois start zero-UI do PF e observação passiva sem mensagem de teste.
- Evidência: 57/57 no gate notifier+restart e 25/25 no gate notify+reconnect; configuração PF confirmou wake passivo e checkpoint obrigatório. No cutover de 21/08, a tarefa S4U subiu com PID novo, Telegram conectado, zero `Scheduled auto-resume`, zero `resume_pending`, zero `kanban notifier: woke agent` e `VisibleWindows=0`.
- Aceite pendente: evento real novo produz notificação passiva e zero novo turno sintético; restart futuro com marcador antigo continua agendando zero retomadas.

## REG-2026-08-21-001 — Ferramentas visuais indisponíveis por erro de sintaxe

- Status: mitigated — runtime recarregado; próxima chamada visual natural no PF pendente
- Cenário reproduzível: uma mensagem com imagens chama o toolset visual e o import de `tools.vision_tools` falha em `line 2276` com `SyntaxError: invalid syntax`.
- Causa raiz: o commit `8b76dee159` adicionou o fallback storyboard de vídeo com um `except` sem o `try` correspondente. A suíte que deveria detectar a falha também não coletava porque `tests/tools/test_video_analyze.py` usava `sys.platform` sem importar `sys`.
- Correção: a chamada nativa de vídeo e sua captura de erro voltaram a formar um bloco `try/except` válido; o retry vazio permanece separado e bounded. O teste voltou a coletar com import explícito de `sys`.
- Prevenção vinculada: `tools/vision_tools.py`, `tests/tools/test_video_analyze.py` e compile gate de todo o diretório `tools`.
- Comando de validação: `python -m compileall -q tools` e `pytest tests/tools/test_vision_tools.py tests/tools/test_vision_native_fast_path.py tests/tools/test_vision_region.py tests/tools/test_video_analyze.py -q`.
- Evidência: import smoke verde; compileall de `agent`, `gateway`, `hermes_cli`, `tools` e `plugins` verde; 72 testes passaram, 1 foi ignorado por condição de plataforma. O PF foi recarregado depois do reparo com Telegram conectado, PID supervisionado e zero janelas; nenhuma mensagem de teste foi enviada ao grupo.

## REG-2026-08-21-002 — Guard de redirecionamento cai ao registrar a primeira falha estrutural

- Status: validated-target — integrated into the current branch and loaded on Titan/default and Project Factory
- Cenário reproduzível: `ToolCallGuardrailController.after_call()` recebe a primeira falha estrutural de `read_file`, `search_files` ou outra ferramenta e tenta registrar a decisão de redirecionamento. A chamada cai com `AttributeError: 'ToolCallGuardrailController' object has no attribute '_redirected_signatures'`; o redirecionamento por ferramenta também cai em `_redirected_tools`.
- Causa raiz: o redirecionamento passou a ler e escrever os dois mapas, mas eles não faziam parte do estado criado por `reset_for_turn()`. O caminho de `before_call()` também não consultava os redirecionamentos registrados, portanto apenas inicializar os campos removeria a exceção sem restaurar o comportamento planejado.
- Correção aplicada: os dois mapas tipados são inicializados e limpos junto do restante do estado por turno; `before_call()` consulta primeiro a assinatura e depois a rota da ferramenta, sem bloquear ferramentas não relacionadas; `reset_for_turn()` volta a permitir a rota no turno seguinte.
- Prevenção vinculada: `tests/agent/test_tool_guardrail_strategy_redirect.py` executa falha estrutural, bloqueio da mesma assinatura/rota, disponibilidade da alternativa, redirecionamento de falha genérica e limpeza por reset.
- Comando de validação: `scripts/run_tests.sh tests/agent/test_tool_guardrail_strategy_redirect.py` pelo wrapper obrigatório do repositório.
- Evidência de prevenção: o wrapper obrigatório passou 6/6 sem retry; o fix isolado `7d245700be` entrou pelo merge `a9c727bfb6`. Após o reinstall do hotfix de normalização do root Windows, Titan/default (PID 27504, Telegram e WhatsApp conectados) e Project Factory (PID 56836, Telegram conectado) recarregaram pela tarefa oculta, registraram o runtime AOF `af15523b1d8da6efb9c9a4349f579a8804cdb37f152e84e5ac3d6b0ed85a25de` e passaram o probe pré-dispatch ligado ao PID atual. Os dois reloads reportaram `visible_windows=[]`; o verificador global posterior reportou `VisibleWindows=0`.

## REG-2026-08-23-001 — Entrega final em streaming deixa checkpoint falsamente retomável

- Status: validated-local; reload e aceite comportamental pendentes.
- Cenário reproduzível: o agente compõe a resposta final, o adapter confirma a entrega por streaming/edição e o usuário recebe a mensagem, mas o checkpoint permanece em `delivery_pending` ou `deliverable_composed`. Em restart posterior, o estado parece trabalho inacabado e pode disputar a autoridade com uma mensagem humana nova.
- Causa raiz: o caminho não-streaming encerrava a entrega, mas o caminho `already_sent` não promovia o checkpoint para `delivered`. Não havia vínculo criptográfico entre o texto realmente confirmado pelo adapter e o `pending_deliverable` persistido.
- Correção: `mark_delivery_if_content_matches` calcula SHA-256 do payload final confirmado, exige igualdade com o `pending_deliverable.sha256` e só então grava entrega/phase `delivered`. Preview, finalize antigo ou conteúdo divergente não conseguem fechar o checkpoint.
- Prevenção vinculada: `tests/agent/test_turn_checkpoint.py::test_stream_delivery_closes_only_the_exact_pending_deliverable` e call site em `gateway/run.py` somente após confirmação de envio.
- Comando de validação: `scripts/run_tests.sh tests/agent/test_turn_checkpoint.py tests/gateway/test_restart_resume_pending.py -q`.
- Estado alvo atual: o dry-run agregado encontrou 246 checkpoints válidos ainda classificados como retomáveis; nenhum foi alterado. O saneamento histórico exige fase operacional separada e confirmação humana.

## REG-2026-08-23-002 — Boards diferentes podem iniciar workers no mesmo checkout

- Status: validated-local; runtime load pendente.
- Cenário reproduzível: cards de boards/perfis distintos apontam para o mesmo `workspace_path`. Como claim/capacidade eram locais ao board, dois dispatchers podiam iniciar writers concorrentes no mesmo checkout e corromper diff, branch ou entrega.
- Causa raiz: a tabela de lease do router não era consultada no boundary real de spawn do Kanban; cada board enxergava apenas seus próprios workers.
- Correção: lease machine-global por caminho canônico em `<kanban_home>/workspace-leases`, adquirido atomicamente antes de claim/spawn, promovido do dispatcher para a identidade PID+create-time do worker e liberado/reclamado apenas com prova de owner morto. Workers vivos anteriores ao upgrade são adotados antes de qualquer novo spawn.
- Prevenção vinculada: `tests/hermes_cli/test_kanban_workspace_lease.py` cobre exclusão no mesmo workspace, paralelismo em workspaces distintos e adoção fail-closed de worker pré-upgrade.
- Comando de validação: `scripts/run_tests.sh tests/hermes_cli/test_kanban_workspace_lease.py tests/hermes_cli/test_kanban_per_profile_cap.py tests/hermes_cli/test_kanban_review_lifecycle.py -q`.
- Invariante: contenção de lease não conta como falha do card, não consome retry e não muda status; apenas adia o spawn.

## REG-2026-08-23-003 — ACL explícita confunde membro da equipe com administrador

- Status: validated-local; reload pendente.
- Cenário reproduzível: um membro explicitamente permitido em um grupo/tópico recebe a mesma decisão `allow` usada para capacidade administrativa, contrariando a premissa “todos podem pedir tarefas, poucos administram runtime/board”.
- Causa raiz: `acl_entries` persistia apenas `allow|deny`; não existia papel separado. O provisionamento histórico de `allowed_users` dependia do significado administrativo de `allow`.
- Correção: a ACL passa a persistir `role=member|admin`; associação implícita e `role=member` retornam capacidade de membro, enquanto entradas antigas e `allowed_users` migram como admin para manter compatibilidade. `deny` continua prevalecendo.
- Prevenção vinculada: `tests/gateway/test_project_router.py::test_explicit_acl_separates_member_from_admin_capability` e suíte integrada de router/slash gating.
- Comando de validação: `scripts/run_tests.sh tests/gateway/test_project_router.py tests/gateway/test_project_router_provisioning.py tests/gateway/test_project_router_gateway.py tests/gateway/test_slash_access_dispatch.py -q`.
- Evidência local: 104/104 verdes.

## REG-2026-08-23-004 — Snapshot vivo não prova qual perfil possui o gateway

- Status: validated-local; runtime load pendente.
- Cenário reproduzível: um `gateway_state.json` criado antes do campo de identidade continua sendo atualizado com PID, start-time e canais, mas a saúde global retorna `recorded_home_unavailable` indefinidamente.
- Causa raiz: `write_runtime_status` copiava do registro atual PID, argv e start-time, mas só gravava `hermes_home` na criação de arquivo novo; snapshots legados nunca eram migrados.
- Correção: todo write de runtime agora atualiza também o `hermes_home` canônico do processo. O reconciliador continua fail-closed para mismatch ou identidade não comprovável.
- Prevenção vinculada: `tests/gateway/test_status.py::TestGatewayRuntimeStatus::test_write_runtime_status_backfills_home_on_legacy_snapshot`.
- Comando de validação: `scripts/run_tests.sh tests/gateway/test_status.py -q -k write_runtime_status`.
- Evidência local: 3/3 testes de write verdes; duas provas POSIX da suíte completa continuam incompatíveis com Windows (`sleep`/fallback `ps`) e foram mantidas fora desta reivindicação.
