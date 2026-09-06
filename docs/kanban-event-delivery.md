# Entrega durável de eventos do Kanban

O cursor da assinatura representa entrega confirmada. A reserva de um evento fica em
`kanban_notify_claims`, no mesmo banco do board, com identidade estável e confirmação
independente do aviso e do encaminhamento ao agente.

O consumidor existente recupera reservas de processos mortos ou expiradas. Cada tentativa
tem um token próprio; confirmação e liberação antigas não alteram outra tentativa.
O aviso confirmado não é reenviado quando apenas o encaminhamento precisa ser repetido.
Falhas de transporte não apagam a assinatura depois de doze tentativas.

No gateway com adaptador push, incluindo Telegram, uma sessão ocupada deixa o evento no
banco. Uma sessão livre recebe o evento interno pela entrada habitual. A confirmação do
encaminhamento ocorre depois da escrita atômica do checkpoint, antes do trabalho do agente.
Uma confirmação perdida pode ser recuperada pelo mesmo identificador no checkpoint.
Antes de substituir esse checkpoint, o Hermes confirma a entrada anterior. A recuperação
da execução interrompida continua pertencendo ao mecanismo de checkpoint existente.

Não há um novo watchdog, varredura para adivinhar tarefas esquecidas, alteração dos modos
de assinatura nem reexecução em lote dos cards antigos. Os dezesseis cards de Concursa
continuam exigindo análise individual do bloqueio, já registrada no handoff anterior.

## Fronteiras da garantia

A correção de aceitação durável cobre o gateway push usado no NFOS. O coletor TUI foi
adaptado ao token de reserva para manter seu comportamento existente; a API mantém a
confirmação pela resposta HTTP. Eles não receberam um protocolo novo de entrada durável.
Uma resposta de rede perdida após um envio externo ainda pode gerar repetição do aviso.
Isto não promete execução exatamente uma vez de efeitos externos arbitrários.

## Verificação

Foram aprovados 19 testes focados de banco, entrega e notificações, 38 de checkpoint,
14 de compatibilidade TUI e 3 da entrega por adaptador. Incluem morte real de processo
depois da reserva, token antigo, aviso entregue com encaminhamento falho, confirmação
perdida depois do checkpoint e adaptador real recebendo o mesmo encaminhamento duas vezes.

O arquivo legado `tests/hermes_cli/test_kanban_notify.py` possui 26 falhas de preparação:
executores fictícios inexistentes ou ausentes, rejeitados antes de chegar à entrega.
Comparação da AST confirmou que criação e validação de executor não mudaram em relação
à base 7d5fa69. Esse reparo não amplia escopo para reescrever tais fixtures.

Ativação e leitura do alvo são registradas separadamente, identificadas pelo SHA real.
