# Continuidade automática do Kanban

O Kanban mantém a tarefa, as tentativas e os eventos. O distribuidor existente reserva
o card para um único worker. Eventos persistentes encaminham bloqueios e conclusão
ao coordenador do projeto. O Vigília lê os mesmos cards e as sessões dos workers.

Criar o card e registrar sua assinatura agora usa uma única transação. Uma interrupção
antes do commit não confirma uma tarefa sem encaminhamento; repetir a criação com a
mesma chave não duplica o card. O cursor de eventos só avança após aceitação durável.

Ao substituir um worker interrompido, o distribuidor reutiliza a sessão persistida da
última tentativa do mesmo card e perfil. O prompt exige ler as instruções atuais e
conferir o alvo antes de repetir uma operação de resultado incerto. Checkpoints de
workers novos identificam card e board e preservam a etapa interrompida na retomada.

O valor zero de gateway_auto_continue_freshness significa retomada sem expiração.
O agendamento e a recuperação de encerramento inesperado agora respeitam esse valor.
No NFOS, os dois gateways recebem essa configuração e agent_wake_on_events=true.
A pausa explícita continua sendo respeitada.

O evento orienta o coordenador a diagnosticar e corrigir a causa dentro do escopo
autorizado, registrar a mudança de abordagem e liberar o mesmo card. Dependências
humanas reais continuam explícitas. Cards históricos exigem reconciliação individual;
resultados prontos, duplicados e trabalho substituído não devem ser reexecutados.

## Evidência e fronteiras

51 testes locais focados passaram. Incluem morte real de processo entre card e
assinatura, repetição idempotente da criação, histórico e checkpoint após morte do
worker, retomada sete dias depois e reserva única da sessão. Ativação e leitura do
alvo são registradas em artefatos separados, identificados pelo SHA real.

Persistência protege o trabalho gravado. Não é garantia contra perda física de disco
nem execução exatamente uma vez de operações externas arbitrárias. Resultados de
operações externas interrompidas precisam de leitura do alvo antes de repetição.
Não foram adicionados watchdog, fila paralela, login ou gate global de publicação.
