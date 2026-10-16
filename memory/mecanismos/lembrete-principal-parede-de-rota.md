# Lembrete do Principal com a parede de rota fechada

Marca: `REMINDER_ROUTE_WALL_20261016` em `hermes_cli/nfos_delivery.py`.

**Mecanismo.** A decisão pendente do Principal é uma espera `principal_answer`. `_principal_answer_plan` manda até
`DECISION_MAX_REMINDERS` (3) lembretes e depois retém o card em `awaiting_principal`
("N lembretes sem resposta"). Antes de 16/10 a conta andava com a rota do card fechada.

**Regra.** Se existe parede para a rota do card (`nfos_route_walls.card_route` + `walls.read`, a mesma rota que
`hold` consulta), o plano não lembra nem retém: reagenda em `_reminder_gap` e zera `context.reminders`
(`_forget_principal_reminders`). Quando a parede some, a conta recomeça do zero. Card já retido fica como está.
Usa `read`, não `closed`: entre uma sonda e outra `closed` devolve `None`, mas o worker continua sem resposta.

**Prova.** `tests/hermes_cli/test_nfos_principal_reminder_backoff.py`:
`test_reminders_do_not_count_while_the_route_wall_is_closed` e `test_the_count_restarts_when_the_route_wall_opens`.

**Origem.** CS-0045 e CS-0061 do Concursa, bloqueados às 19:30Z de 16/10 com a rota openrouter sem crédito.
Fora do escopo: o caminho de lembrete da escalada de destino (`_remind_destination_escalation`) não foi alterado.
