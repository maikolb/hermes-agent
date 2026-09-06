# Correção dos avisos do Kanban

Correção `6ffe7f4fbf156cb5d20e0d74641f5fa186555326` publicada no fork e ativa no gateway de projetos em srv1918217.
O entrypoint aponta para a nova release. Apenas hermes-gateway@hermes-project-factory
foi reiniciado. O distribuidor default continua no PID 3989907, runtime ab8710cf;
ele não envia notificações dos boards Concursa e DOV, vinculados ao perfil de projetos.
Os workers ficaram fora do serviço reiniciado. Durante a janela, tentativas encerraram
e novas tentativas apareceram no default. Não reiniciar default apenas para igualar SHAs.

## Incidente e correção

O card t_f1ce5125 foi liberado às 04:13:59 UTC e reservado às 04:15:44 UTC de
06/09/2026: 1 minuto e 45 segundos de fila. O primeiro aviso saiu após 19 segundos.
Os 6556 minutos vieram da criação antiga do card. O aviso se repetia porque o código
procurava comentários de um autor diferente daquele usado na gravação.

Agora a idade parte da entrada atual na fila, o evento persistido identifica o alerta
daquela entrada e uma causa não medida não vira acusação de distribuidor parado.
As notas de reavaliação continuam encaminhadas ao coordenador, com aviso curto;
não são saída nem bloqueio de worker. Bloqueios reais conservam o motivo do evento.
As mensagens antigas permanecem como histórico; não houve apagamento nem novo lote de retomada.

## Evidência

26 testes locais focados e 5 testes isolados na VPS passaram. Todos os
10485 arquivos da release foram conferidos por hash.
O novo PID do gateway de projetos está conectado ao Telegram. O default permanece no
mesmo PID. A tentativa 300 tem RTU próprio no Vigília, com reasoning e ferramentas;
o log passou de 742239 para 1078061 bytes.
Estado observado do card: blocked. O novo bloqueio, evento 10002,
é concreto: timeout no setup de Vitest/PGlite; a execução preservou a fixture.
Esse bloqueio é posterior à execução real e distinto do falso alerta das imagens.
O cursor notify+wake avançou para o evento 10002, confirmando o encaminhamento ao
coordenador. O card t_f6e29133 tem worker ativo na tentativa 315. Isso não declara entregas aceitas.

## Operação e rollback

Acompanhar https://vigilia.187.127.60.126.sslip.io/lux/.
Não redisparar o card nem repetir os pedidos owner-continuity-20260906.
Backup: `/var/backups/hermes/alert-integrity-6ffe7f4fbf156cb5d20e0d74641f5fa186555326/activation-20260906T050246Z`. Contém snapshots dos dois boards, configuração e
checkpoints dos coordenadores ativos. Não restaurar bancos antigos sobre trabalho novo.
Rollback, se necessário: repontar /usr/local/bin/hermes para a release ab8710cf e
reiniciar somente hermes-gateway@hermes-project-factory.service. A release anterior
permaneceu intacta; nenhum script de gate global foi criado.

## Closeout proporcional

- Run Metrics: 26 testes locais e 5 no alvo. Tempo de ativação: 424 segundos.
  Cota semanal da conta consultada: 66% usada, 34% restante. Tokens e custo
  específicos desta execução indisponíveis no medidor exposto.
- Policy Compliance: release por SHA, conferência integral de fonte, rollback,
  escopo restrito, VisibleWindows=0 nos transportes concluídos.
- Run Changes: cálculo e dedupe de alerta, motivo real do bloqueio e reavaliação sem saída de worker.
- Discovery Promotions: none. Sem mudança no Vigília, login ou outros projetos.
- Promotion Candidates: none. Correção do notificador ativada; tarefas de negócio continuam.
