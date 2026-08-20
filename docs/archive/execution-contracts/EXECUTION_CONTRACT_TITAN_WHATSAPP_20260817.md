# CODEX Execution Contract

## Contract Metadata
- Mode: RESTORE_THEN_VERIFY
- Risk Level: high
- Workspace: C:\Users\maiko\AppData\Local\hermes\hermes-agent
- Updated At: 2026-08-17T19:05:00-03:00

## Requested Outcome
- Restaurar o WhatsApp do perfil default (Titan), comprovar recebimento/resposta e garantir que o gateway volte sozinho após logon ou queda de energia.

## In Scope
- Sessão local do WhatsApp em `C:\Users\maiko\AppData\Local\hermes\whatsapp\session`.
- Fluxo oficial `hermes whatsapp` do perfil default.
- Saúde local do bridge do WhatsApp e sua integração com o gateway default.
- Launcher Windows do subprocesso Node do bridge, testes focados e tarefa agendada `Hermes_Gateway`.
- Roteamento inteligente do perfil default no WhatsApp para evitar que mensagens triviais usem Sol/High.
- Registro canônico da regressão recorrente.

## Out of Scope
- Outros perfis Hermes, Telegram, equipes, projetos, boards, repositórios e mensagens.
- Mensagens de teste enviadas pelo agente, outros perfis, Telegram, equipes, projetos, boards e repositórios.
- Refactors amplos do broker ou mudanças de configuração não necessárias ao canal default.

## Failure Signal / Repro
- Após pareamento confirmado, o bridge iniciado pelo gateway encerra com código 1 em toda tentativa. Um Node iniciado diretamente conecta, mas não recebe a configuração do perfil e rejeita as mensagens autorizadas; portanto `connected` isolado não prova o canal funcional.

## Root-Cause Hypothesis
- Facts: o pareamento oficial foi concluído e a sessão é válida; o mesmo `bridge.js` conecta quando iniciado diretamente; pelo gateway, o broker Windows retorna um processo intermediário que morre com código 1 e não preserva a identidade/lifecycle do Node real. O processo direto temporário não recebeu `WHATSAPP_ALLOWED_USERS` e rejeitou o inbound.
- Assumptions: nenhuma; o defeito está reproduzido na fronteira entre o adapter e o broker Windows.
- Chosen fix point: usar a capacidade direta, oculta e supervisionada do broker para o Node long-lived do WhatsApp, preservando environment, stdio, PID real e Job Object.
- Secondary fix point: incluir `whatsapp` na allowlist de plataformas do smart model router do perfil default; o log comprovou `platform_not_enabled` e Sol/High para uma saudação simples.

## Forbidden Actions
- No scope expansion beyond the requested outcome.
- No hidden side effects.
- No behavior changes outside the declared scope.
- No placeholders, fake values, temporary keys, or config overrides unless explicitly requested.
- Não enviar mensagens de teste sem autorização adicional.
- Não apagar ou regenerar a sessão pareada.
- Não tocar nos demais perfis/canais nem substituir o Hermes por fork não autorizado.

## Validation Plan
- Analyze/lint: `py_compile`, `node --check` e `git diff --check` nos arquivos afetados.
- Unit tests: broker Windows, adapter WhatsApp, setup/pareamento e regressão específica do launcher direto.
- Config check: parsear `config.yaml` sem expor segredos e comprovar smart routing ativo para Telegram + WhatsApp.
- Integration/contract tests: parar o bridge temporário, reiniciar apenas `Hermes_Gateway`, observar PID Node gerenciado, listener/health `connected`, inbound e resposta a uma nova mensagem enviada pelo usuário.
- Build/install/deploy checks: validar ação, trigger de logon, restart policy e `StartWhenAvailable` da tarefa agendada.
- Manual smoke checks: usuário envia nova mensagem; o agente observa apenas metadados técnicos, sem ler conteúdo.
- Zero-UI: monitorar o restart real e exigir `VisibleWindows=0`.

## Status
- Contract preflight: validated-local
- Implementation: complete
- Validation: validated-target
- Completion: validated-target; user acceptance pending

## Target Evidence
- Pareamento oficial concluído e sessão preservada.
- `Hermes_Gateway` reiniciado sem bridge auxiliar: tarefa `Running`, Node listener próprio, PID file igual ao PID do listener e `/health=connected`.
- Monitor simultâneo do restart: `VisibleWindows=0` em 20 segundos.
- Inbound novo autorizado; smart route escolheu `gpt-5.6-luna`/`low`; resposta pronta em 11,4 s.
- Ledger de entrega: duas obrigações WhatsApp recentes em estado `delivered`, nenhuma pendente/falha.
- Auditoria de autostart: 3/3 perfis permanentes conformes e `Running`; default inicia por logon com delay de 30 s e reinício automático a cada minuto.
- Testes: 43 testes broker/WhatsApp/setup + 1 contrato zero-UI passaram; Node syntax/native test e `git diff --check` passaram.
