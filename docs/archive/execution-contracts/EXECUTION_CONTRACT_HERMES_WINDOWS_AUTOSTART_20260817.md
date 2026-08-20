# CODEX Execution Contract

## Contract Metadata
- Mode: IMPLEMENT_THEN_VERIFY
- Risk Level: high
- Workspace: `C:\Users\maiko\AppData\Local\hermes\hermes-agent`
- Updated At: 2026-08-17T13:09:00-03:00

## Requested Outcome
- Garantir que os três gateways Hermes já marcados operacionalmente como permanentes (`default`, `hermes-ceogame` e `hermes-project-factory`) voltem automaticamente e sem interface visível na primeira sessão do Windows após queda de energia ou reinício, com reinício automático após falha e sem instâncias duplicadas.

## In Scope
- As tarefas agendadas `\Hermes_Gateway`, `\Hermes_Gateway_hermes-ceogame` e `\Hermes_Gateway_hermes-project-factory`.
- Os launchers ocultos já existentes dos três perfis e uma rotina idempotente machine-local em `C:\Users\maiko\AppData\Local\hermes\scripts`.
- Os atalhos legados `Hermes CEOGame.lnk` e `Hermes Project Factory.lnk`, somente para movimentação recuperável após a instalação das tarefas equivalentes.
- Backups, evidência sanitizada e documentação operacional em `C:\Users\maiko\AppData\Local\hermes\backups` e neste contrato.

## Out of Scope
- Perfis `bench-supervisor`, `hermes-darkfactory` e `hermes-exocortex`, que permanecem dormentes.
- Tokens, credenciais, IDs de Telegram, mensagens, envio de mensagens, comandos slash ou smoke Telegram ativo.
- Alterações de modelo, roteamento multi-equipe, Kanban, Honcho, ai-memory, código de negócio ou bancos Hermes.
- Serviço Windows sob `SYSTEM`, execução antes do login, VPS, deploy, Git commit, push ou limpeza ampla do worktree já sujo.

## Failure Signal / Repro
- Após o boot de 2026-08-17, o perfil `default` voltou por Scheduled Task, mas `hermes-ceogame` e `hermes-project-factory` dependeram de atalhos da pasta Inicializar.
- Os dois atalhos iniciaram processos ocultos, porém não fornecem `RestartOnFailure`, `MultipleInstancesPolicy=IgnoreNew` nem uma autoridade única de lifecycle por perfil.
- O resultado é recuperação desigual: um crash posterior ao login pode manter esses perfis offline até nova ação manual ou novo login.

## Root-Cause Hypothesis
- Facts: há uma tarefa agendada somente para `default`; CEOGame e Project Factory têm atalhos de Inicializar; os três processos estão vivos após o login; o Telegram do `default` conectou em polling; CEOGame e Project Factory registraram recuperação/retry de rede.
- Assumptions: os três perfis com mecanismo de startup existente representam exatamente o conjunto pretendido como always-on nesta máquina; os demais perfis devem continuar dormentes.
- Chosen fix point: substituir os dois atalhos por tarefas profile-scoped e reconciliar a tarefa default por uma rotina idempotente que usa launchers `pythonw.exe -> .pyw`, delays escalonados, `IgnoreNew`, `StartWhenAvailable` e restart a cada minuto.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Não executar `sendMessage`, `getUpdates` manual, comando slash, teste de mensagem ou leitura de conteúdo das conversas.
- Não matar gateways já saudáveis para simular falha ou queda de energia; uma troca profile-scoped é permitida somente para transferir a supervisão do atalho legado para a tarefa já validada.
- Não registrar tarefa sob `SYSTEM` nem armazenar senha do usuário.
- Não apagar atalhos ou backups; toda retirada da pasta Inicializar deve ser por movimentação recuperável e somente depois do registro verde das tarefas.

## Validation Plan
- Analyze/lint: parse PowerShell da rotina; validar caminhos absolutos, launchers existentes, executável `pythonw.exe` e subsystem GUI.
- Unit tests: executar modo audit/dry-run da rotina e validar que os três perfis são resolvidos sem tocar os demais.
- Integration/contract tests: registrar/reconciliar as três tarefas; confirmar trigger de logon, delay, `IgnoreNew`, `StartWhenAvailable`, restart policy, ação direta e ausência dos atalhos ativos duplicados.
- Build/install/deploy checks: validar o contrato antes e depois; exportar XML e hashes para backup; conferir task state e PID/profile/birth marker sem expor segredos.
- Manual smoke checks: observar os gateways reais já iniciados, confirmar polling/heartbeat pelos logs sanitizados e executar verificador zero-UI durante uma invocação real e bounded que não envia mensagens.

## Status
- Contract preflight: validated before mutation
- Implementation: complete; three profile-scoped tasks and one idempotent audit/reconcile routine are installed, and CEOGame/Project Factory were transferred to task-owned processes
- Validation: task definitions `3/3`, task-owned runtime `3/3`, PID/birth/home identity `3/3`, health failures=0, Telegram connection/recovery observed passively, zero target-visible windows with listener droppedEvents=0
- Completion: complete at `validated-target` for the scheduled actions and current runtime; the next natural logon trigger remains the only post-install event not yet observed, and no Telegram behavioral smoke was performed
