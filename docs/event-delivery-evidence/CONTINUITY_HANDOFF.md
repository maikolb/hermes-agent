# Continuidade NFOS: publicação e retomada

Runtime `ab8710cfa5e29c7f09d35f9c84780150492fefc9` publicado no fork e ativo em `srv1918217`.
Os dois gateways estão ativos, conectados ao Telegram e com início automático no boot.
`gateway_auto_continue_freshness=0` e `agent_wake_on_events=true` nos dois perfis.

## Percurso verificado

51 testes locais e 5 testes na VPS passaram. Houve morte real de processos isolados,
recuperação da sessão e do checkpoint, criação atômica de card com assinatura,
repetição idempotente e retomada após sete dias sem duplicação da reserva.

O coordenador recebeu os eventos históricos, diagnosticou bloqueios e liberou tarefas
existentes. Na leitura de 2026-09-06T04:07:02.721132+00:00, havia dois workers reais, distintos:
Concursa `t_74beb26e`, tentativa 294, e DOV `t_427dcfe4`, tentativa 315.
Os dois tinham sessão vinculada à tentativa, reasoning e ferramentas no log completo.
O log de DOV cresceu de 38562 para 217539 bytes.

27 cards receberam pedidos duráveis de reconciliação individual. Dois casos com
informação humana concreta faltando ficaram aguardando. A fila continua sendo tratada;
isso não declara todos os cards antigos resolvidos. Entregas históricas sem evidência
recuperável não são consideradas concluídas apenas pelo relato anterior.

## Handoff operacional

Acompanhar em https://vigilia.187.127.60.126.sslip.io/lux/.
Os coordenadores e o distribuidor já estão trabalhando. Não reenfileirar o lote nem
criar cards, commits ou PRs duplicados. Antes de retomar uma operação externa de
resultado incerto, ler o alvo. Manter requisitos humanos realmente necessários no card.
Publicações dos projetos continuam obedecendo ao escopo autorizado de cada tarefa.

Backup da ativação: `/var/backups/hermes/continuity-ab8710cfa5e29c7f09d35f9c84780150492fefc9/activation-20260906T035221Z`.
Backup dos dois boards antes da reconciliação: `/var/backups/hermes/continuity-ab8710cfa5e29c7f09d35f9c84780150492fefc9/historical-20260906T035531Z`.
A versão anterior permanece intacta: `3c6418ae9edb6130a30634efe722e5e7358c4711`.
Rollback, se necessário, exige parar apenas os dois gateways afetados, repontar o
entrypoint para essa release e religá-los. Não restaurar bancos antigos sobre trabalho novo.
Se rollback de configuração for necessário, os dois YAML anteriores estão no backup.

Os arquivos antigos de publicação global não foram recriados. O Titan local não foi
reiniciado. Nenhuma mudança foi feita em frontend, login, P0-4a, laboratório ou retenção.

## Closeout proporcional

- Run Metrics: 51 testes locais e 5 no alvo; cerca de 50 minutos incluindo preparação e
  aquecimento dos gateways. Cota da conta consultada: 60% usada, 40% restante na janela
  semanal. Tokens e custo desta execução não estão disponíveis no medidor exposto.
- Policy Compliance: fonte real conferida, release nova por SHA, backup, rollback,
  testes isolados, VisibleWindows=0 nos transportes; sem relaxamento de controles.
- Run Changes: sessão/checkpoint do worker, transação card/assinatura, expiração da
  retomada, orientação do evento e configuração de continuidade dos dois perfis.
- Discovery Promotions: cards Principal sem metadata de sessão não possuem o mesmo
  vínculo de RTU dos workers. A thread do Vigília conferiu o comportamento sem editar;
  a UI identifica o fallback como histórico. Isso não integra a garantia sobre workers.
- Promotion Candidates: none. Runtime ativado. Melhorias visuais de Telegram adiadas.

Persistência cobre o que foi gravado; os testes não simularam destruição física de disco
nem prometem execução exatamente uma vez em serviços externos. O reinício real dos
gateways foi verificado, sem desligar fisicamente a VPS de produção.
