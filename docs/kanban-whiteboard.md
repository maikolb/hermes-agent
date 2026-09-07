# Contrato de apresentação do Kanban

Fonte única: SQLite nativa do Hermes. Sem persistência de estados derivados.
Função canônica: `hermes_cli.kanban_db.task_presentation` no candidato desta execução.

Campos aditivos em `tasks`:
- `task_role`: work (padrão e fallback legado), activity, aggregate.
- `delivery_type`: code, report, operation. NULL legado permanece desconhecido.
- `requires_repo`: 0/1; NULL legado permanece desconhecido.
- `instruction_revision`: inteiro, padrão 0. Incrementa quando título ou body muda.

Não mudar IDs, `tasks.status`, `task_runs.id`, perfil nem vínculo da sessão.
`task_runs.metadata.worker_session_id` aponta para a sessão real do worker no state.db do perfil da run. `tasks.session_id` continua sendo origem/contexto, nunca substitui o vínculo do worker.

Etapas visuais:
- backlog, triage, todo, scheduled, ready: A fazer.
- running: Em andamento, ou Conferindo quando o último evento `claimed` da run corrente traz `payload.source_status=review`.
- review: Conferindo.
- done: Feito.
- blocked: Em andamento se started_at existe, senão A fazer; Conferindo somente se o evento de estado vigente abaixo registra review.
- archived: histórico. Estado desconhecido: seção explícita, preservar nome bruto.

Evento vigente de blocked: maior `task_events.id` para o task_id entre blocked, block_loop_detected, dependency_wait, unblocked, claimed, status, reclaimed, claim_reaped, completed, archived, changes_requested e gave_up. Só interpretar razão/fase se esse evento for blocked, block_loop_detected ou gave_up. Assim um bloqueio antigo não reaparece depois de nova tentativa. Fase review quando `source_status`, `retry_status` ou `resume_status` desse payload é review. Razão: payload.reason, depois payload.error; se ausentes, summary da run identificada por event.run_id e task_id. Sem correspondência: motivo desconhecido. Quando status deixa blocked, razão é sempre nula.

WIP: task_role=work, started_at presente e status diferente de done/archived. Inclui espera; não conta activity ou aggregate novamente.

Sinal recente de execução: tasks.status=running, current_run_id identifica run do MESMO task_id, run.status=running, ended_at NULL, claim_lock não vazio e igual entre task/run, tasks.worker_pid presente; heartbeat da run (fallback task) com idade entre 0 e 90 segundos. Fora disso, não afirmar execução recente. Sem heartbeat: desconhecido. Com heartbeat vencido: sinal vencido.

Processo verificado: só no host do claim e com PID/start identity conferidos. A Lux no contêiner não pode verificar PID do host: apresentar sinal recente, não alegar processo verificado. Activity/aggregate nunca acendem indicador de worker, mesmo com run de registro.

Activity: registro de conversa ou auxiliar; excluído de contadores de trabalho e fila executável. Deve continuar acessível por ID e manter Live Log. Histórico não pode inventar entrega.

Aggregate: card que reúne entregas independentes, sem executor próprio. As tarefas de entrega são `task_links.parent_id` para `child_id=aggregate.id` (a direção é dependência). Progresso deriva desses cards, não de contador próprio. Só o Hermes conclui o agregado com todos os pais done e respectivos resultados persistidos. Arquivado não conta como entregue.

Limite desta coordenação: a Lux publicada ainda não carrega estes campos aditivos. Não reivindicar o novo agrupamento até o delta de frontend ser publicado e lido no alvo. Laboratório excluído.
