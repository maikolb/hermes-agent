# Hermes Revision Protocol

Autoridade canônica de regressões operacionais deste checkout.

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

- Status: mitigated — validated-target; acceptance pending durante uso prolongado real
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

## REG-2026-08-19-004 — Checkpoint é detectado, mas o agente pede ao usuário para recomeçar

- Status: closed — validated-target
- Cenário reproduzível: após restart, o bot informa "Sessão restaurada com sucesso", reconhece que o turno anterior foi interrompido e pergunta "O que você quer fazer agora?" em vez de continuar da ação persistida.
- Causa raiz comprovada: o evento sintético de restart chegava como novo turno vazio e não podia reutilizar o checkpoint original por hash; além disso, a nota de recuperação instruía transportes interativos a narrar o restore e perguntar o próximo passo.
- Correção aplicada: o evento vazio de auto-resume recebe uma flag one-shot, reutiliza o checkpoint inacabado original independentemente do hash vazio e continua da `next_action`; efeitos incertos exigem readback/reconciliação autoritativa antes de retry. A nota proíbe narrar restore e só permite pergunta quando a autoridade inexiste ou o risco irreversível não pode ser reconciliado.
- Contrato correto: estado conhecido continua da `next_action`; resultado concluído é consumido; efeito incerto é reconciliado automaticamente no sistema autoritativo; pergunta humana só é permitida quando não existe autoridade consultável ou há risco irreversível que não pode ser reconciliado.
- Prevenção vinculada: teste de restart em `planning`, teste de ferramenta incerta com reconciliador e asserção negativa contra mensagens genéricas de "o que fazer agora" quando a recuperação é determinística.
- Evidência alvo: no primeiro restart do PF após o patch, duas sessões `resume_pending` foram agendadas. Ambas atualizaram os checkpoints preexistentes e executaram ferramentas pós-restart (pelo menos 3 e 7 concluídas na primeira janela); o scan das respostas não encontrou a mensagem genérica de restore/pergunta e não houve `unexpected error`.

## REG-2026-08-20-001 — Pedido explícito de continuação produz falsa retomada

- Status: mitigated — repair validated-local; PF reload/target pending
- Cenário reproduzível: depois de uma falha `usage_limit_reached`, o usuário envia "Continue de onde parou". O bot responde "Retomado do checkpoint" e depois declara atividades em execução, mas não reativa o turno interrompido.
- Evidência: o checkpoint escrito após a mensagem tem `recovery.restored=false`, `recovery.resolution=new_turn` e novo `turn_id`; a resposta visível afirma o oposto. Um subagente separado foi criado e expirou, sem converter o turno original em continuidade real.
- Causa raiz: o gateway só arma `_resume_turn_from_checkpoint` para eventos sintéticos vazios de restart. Mensagens humanas não vazias, inclusive continuação explícita, forçam turno novo e substituem o checkpoint ativo. O sistema também não vinculava alegações de restauração/progresso a estado material verificável.
- Falha adjacente: `usage_limit_reached` não persiste horário de liberação nem agenda retomada; exige nova mensagem humana e ainda assim abre turno novo.
- Correção aplicada: classificador estrito de intenção de continuação condicionado a checkpoint inacabado; restore one-shot do checkpoint original; retomada bounded no horário de liberação; gate que rejeita respostas de status como "retomado", "continuidade ativa" e "está em execução". Após três recusas, o bot devolve blocker honesto e mantém o checkpoint recuperável em vez de alegar trabalho inexistente.
- Recuperação de cota: `resets_in_seconds` vira `resume_not_before`; o estado persiste no índice de sessões, sobrevive restart, agenda um único wakeup e limita a três tentativas automáticas. Adapter offline ou usuário não autorizado não consomem tentativa; sucesso limpa o estado.
- Prevenção vinculada: testes focados de explicit-resume, provider recovery persistida e falsa confirmação; `docs/EXECUTION_CONTRACT.md`.
- Comando de validação: pytest focado nos módulos de checkpoint/restart-resume/provider failure, seguido de reload controlado do PF e observação passiva do runtime sem mensagem sintética a terceiros.
- Evidência local: 121/121 nos testes de explicit-resume + checkpoint; 103/103 no recorte de resume/provider error; gate integrado 199/199; py_compile e diff-check verdes. O gateway PF ainda executa código anterior porque um monitor read-only de 30 minutos confirmou `active_agents=1`, workers em atividade e checkpoints Telegram/subagente avançando; target permanece corretamente pendente.

## REG-2026-08-21-001 — Guard de redirecionamento cai ao registrar a primeira falha estrutural

- Status: mitigated — isolated source candidate validated-local; live cutover pending
- Cenário reproduzível: `ToolCallGuardrailController.after_call()` recebe a primeira falha estrutural de `read_file`, `search_files` ou outra ferramenta e tenta registrar a decisão de redirecionamento. A chamada cai com `AttributeError: 'ToolCallGuardrailController' object has no attribute '_redirected_signatures'`; o redirecionamento por ferramenta também cai em `_redirected_tools`.
- Causa raiz: o commit que introduziu o redirecionamento passou a ler e escrever os dois mapas, mas não os adicionou ao estado criado por `reset_for_turn()`. O caminho de `before_call()` também não consultava os redirecionamentos registrados, portanto apenas inicializar os campos eliminaria a exceção sem restaurar o comportamento planejado.
- Correção aplicada: os dois mapas tipados são inicializados e limpos junto do restante do estado por turno; `before_call()` consulta primeiro a assinatura e depois a rota da ferramenta, sem bloquear ferramentas não relacionadas; `reset_for_turn()` volta a permitir a rota no turno seguinte.
- Prevenção vinculada: `tests/agent/test_tool_guardrail_strategy_redirect.py` executa falha estrutural, bloqueio da mesma assinatura/rota, disponibilidade da alternativa, redirecionamento de falha genérica e limpeza por reset.
- Comando de validação: `scripts/run_tests.sh tests/agent/test_tool_guardrail_strategy_redirect.py` pelo wrapper obrigatório do repositório, com `HERMES_PYTHON` apontando para o venv de desenvolvimento existente.
- Evidência: baseline reproduziu 5/5 falhas com os dois `AttributeError`; após a correção, 6/6 testes passaram sem retry. Nenhum gateway/processo foi recarregado e o checkout Hermes vivo não foi editado nem recebeu operação Git.
- Aceite pendente: cutover drenado e smoke no runtime real em janela controlada; o change-set permanece isolado até essa etapa.
