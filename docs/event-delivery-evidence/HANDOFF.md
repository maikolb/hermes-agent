# Entrega durável do Kanban: handoff

Release ativa e verificada em srv1918217: `3c6418ae9edb6130a30634efe722e5e7358c4711`.
Git: https://github.com/maikolb/hermes-agent/commit/3c6418ae9edb6130a30634efe722e5e7358c4711
Branch: `fix/nfos-whiteboard-20260905`. O commit posterior de fechamento altera somente documentação e a fixture TUI; o código em produção permanece neste SHA.

O Kanban conserva a entrega pendente até a confirmação. Aviso e encaminhamento têm progresso
separado. No gateway push, incluindo Telegram, a confirmação exige checkpoint persistido.
A repetição do mesmo encaminhamento aceito não cria outro turno. A recuperação do trabalho
interrompido continua no checkpoint existente. Nenhum watchdog novo foi criado.

Os dois gateways estão ativos, executando a release e conectados ao Telegram, com PID conferido.
Foram verificados os hashes dos seis arquivos de runtime alterados e a nova tabela nos boards
Concursa e DOV. A validação do fluxo foi feita com dados isolados na VPS, sem enviar tarefas
fictícias aos grupos. A aceitação final permanece com Maikol.

Backup válido: `/var/backups/hermes/event-delivery-3c6418ae9edb6130a30634efe722e5e7358c4711/activation-20260906T013819Z` (30 bancos do Kanban, cópia SQLite e quick_check).
Rollback: repontar atomicamente `/usr/local/bin/hermes` para
`/usr/local/lib/hermes-agent.release-20260905-7d5fa695c0a75cafd8c8c8ef4a03e328a57c1c03/venv/bin/hermes` e reiniciar somente
`hermes-gateway@default.service` e `hermes-gateway@hermes-project-factory.service`
após conferir atividade. Não restaurar dados sobre atividade nova.

O código de entrega passou nos testes do alvo. A execução inicial teve 73 aprovações e uma
falha TUI: o prazo de cinco segundos era consumido na inicialização do banco de `/loop`,
antes do Kanban. A captura de pilha confirmou esse caminho. Com a sessão de teste isolada
de `/loop`, os 14 testes TUI passaram no mesmo runtime imutável. A fixture foi corrigida
no Git; nenhum recurso foi desativado em produção. São 74 casos distintos aprovados,
considerando a fixture corrigida. As 26 falhas legadas de executor fictício estão registradas
separadamente e não foram transformadas em trabalho de produto nesta mudança.

Erro operacional desta execução: o primeiro roteiro incluiu 10,6 GiB de histórico de sessões
no backup durante a parada, embora o esquema alterado seja do Kanban. A cópia excedente foi
interrompida, identificando exclusivamente o processo desta execução pelo arquivo aberto.
A cópia parcial foi marcada inválida para restore. A ativação prosseguiu com snapshots
somente dos bancos afetados. O episódio alongou a indisponibilidade e está registrado.

Pendências anteriores preservadas: os dezesseis cards bloqueados de Concursa não foram
reexecutados; modos de assinatura não foram alterados. A correção do checkout canônico do
DOV continua em escopo separado. Vigília, seu login removido, NFO-Homolog-Lab, retenção,
VACUUM, permissões, hooks e políticas globais não foram alterados.

Run Metrics: cota semanal da conta em 57% consumida (43% restante) na leitura registrada;
tokens e custo específicos desta execução não estão disponíveis. Houve uma implementação,
testes focados e a correção do backup excedente, sem agentes adicionais.
Policy Compliance: publicação autorizada, release imutável, backup dos bancos afetados,
zero janelas visíveis no transporte monitorado, nenhuma nova exigência ou controle global.
Run Changes: entrega, reserva, confirmação por checkpoint, compatibilidade da API TUI e testes.
Discovery Promotions: none. Promotion Candidates: problemas anteriores de cards/checkout,
já separados no handoff; nenhuma ampliação automática nesta execução.
