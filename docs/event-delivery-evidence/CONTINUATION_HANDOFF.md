# Continuidade dos workers: runtime e Vigília publicados

Conferência: 2026-09-06T07:38:41.152858+00:00. Alvo confirmado: srv1918217. A retomada de execução foi comprovada; a operação inteira ainda tem a pendência AOF descrita abaixo. Não declarar todos os cards resolvidos.

## Estado publicado

- Hermes: `b011553a62865aefa8226b0c12b553b343457c30`, branch `fix/nfos-whiteboard-20260905`, pushed. Entrypoint: `/usr/local/lib/hermes-agent.release-20260906-b011553a62865aefa8226b0c12b553b343457c30/venv/bin/hermes`. Dispatcher default PID73364, Telegram conectado. Gateway hermes-project-factory PID4097947 preservado.
- Vigília: `0d67d6f6dc28136bb4648c945ad1d33555bf44be`, branch `codex/vigilia-lux-vps`, pushed. Imagem `ghcr.io/maikolb/vigilia-live-view@sha256:d50ab48296e03b68bb154137636dafa5812ccba7f305f14dbb0bb078392c6e01`, container `82bed2781657`, healthy. Somente dockerImage mudou no Dokploy.
- URL: https://vigilia.187.127.60.126.sslip.io/lux/

## Antes e depois medidos

- Concursa `t_d61b19ea`: quatro respostas antigas idênticas, nenhuma ferramenta. Após a correção, run354 executou ferramentas novas na mesma sessão `20260906_043517_a6380b`, preservando o histórico. As verificações pendentes ensurePopulation 4/4 e reconcileInheritedPositions 5/5 passaram. O setup PGlite seguinte excedeu 10s; o Hermes registrou diagnóstico no comentário643, desbloqueou pelo mecanismo existente e o distribuidor iniciou run356. A UI pública mostrou sinal recente e mensagens da execução356.
- DOV `t_9ce8e0e3`: run399 executou ferramentas novas na mesma sessão `20260906_042325_19b4a8`. O probe AWS terminou com exit0. A recusa atual é AOF, registrada nos comentários767/768. A UI pública mostrou bloqueado, sem anunciar trabalho ativo.
- O checkpoint de uma resposta já composta inicia um turno executável na mesma sessão; uma operação interrompida e uma verificação pendente mantêm a retomada durável. O evento gave_up permanece bloqueado até uma continuação registrada, inclusive quando o contador genérico ainda está abaixo do limite.
- O live log perdia task.worker ao atualizar. Agora usa as mesmas funções de projeção do quadro, na conexão somente leitura. Nenhuma nova fonte de estado.

## Verificação e limites

10 testes focados passaram na release Linux exata, incluindo morte abrupta de processo, preservação de histórico/checkpoint, criação idempotente com assinatura e próximo dispatch após esgotamento. 14 testes focados do Vigília passaram; o novo teste falhou antes com KeyError: worker. O endereço público foi conferido visualmente em 390px, com IDs das novas mensagens, estado do worker e nenhum overflow horizontal. Capturas e conteúdo de sessões ficaram em outputs/rtu-ui, fora do Git.

Isso não equivale a desligar fisicamente a VPS: a falha de processo foi exercida em fixture isolada. Não havia worker de negócio ativo no instante de ativação do gateway. O teste de restauração não foi apresentado como prova de todos os modos de falha ou aceite dos cards.

## Pendência operacional concreta do DOV

O hook instalado `/opt/agent-operating-framework/adapters/hermes/plugin/aof-route-policy/__init__.py` escolhe o primeiro `docs/EXECUTION_CONTRACT.md` na ancestralidade do workspace com .git. Para este worker, seleciona `/srv/projects/next-crm/docs/EXECUTION_CONTRACT.md`: contrato de 17/08 para nomes de contatos do WhatsApp, diferente da auditoria de responsividade/HML atual. O gate verifica todo o git status, incluindo arquivos preexistentes `.hermes/notion-*`, relatórios e gravações. A fonte canônica local de validate_scope_alignment.ps1 inspecionada não oferece declaração de baseline preservado por hash; a identidade exata do validador instalado ainda deve ser conferida antes de qualquer alteração AOF.

Próxima correção necessária, antes de novo unblock: vincular o contexto do card ao contrato vigente e reconhecer arquivos preexistentes como preservados sem permitir sua alteração. Validar pelo mesmo hook e perfil do worker: leitura/teste permitido, escrita fora do escopo negada, preexistentes intactos. Não basta inserir todos os arquivos no In Scope; isso ampliaria permissões. Não apagar arquivos, trocar cwd/perfil para escapar do hook nem reenviar o mesmo pedido. Nenhum hook ou política AOF foi alterado nesta entrega. Evidência privada detalhada: continuity-aof-read.json e continuity-aof-origin.json.

Não reconstruir contexto: ler kanban_show e comentários767/768; preservar PR51, ambiente HML, histórico e testes; não fazer merge/produção do DOV. O Concursa já continua pelo próprio Hermes e não precisa de outro worker duplicado.

## Backup, diff e rollback

- Hermes: `/var/backups/hermes/worker-continuation-b011553a62865aefa8226b0c12b553b343457c30/activation-20260906T070905Z`. Releases antigas intactas. Diff: continuity-runtime.diff. Rollback de código: conferir workers ativos, repor entrypoint antigo `/usr/local/lib/hermes-agent.release-20260906-6ffe7f4fbf156cb5d20e0d74641f5fa186555326/venv/bin/hermes` e reiniciar graciosamente apenas o default pelo procedimento existente. Não restaurar bancos sobre trabalho novo.
- Vigília: backup local `C:\Users\maiko\Documents\Codex\2026-09-05\quero-corrigir-a-configura-o-operacional\outputs\rtu-ui\continuity-projection-before`. Diff: continuity-projection-runtime.diff. Rollback: repor somente dockerImage `ghcr.io/maikolb/vigilia-live-view@sha256:9915cb18da83933251b9bb0fbbaa1eb1a9e1aa1d69b8e66ac6f81e094dc97090` na aplicação `_KYsPrD8OAUzqCS0e6XZH` e redeploy.
- Evidências de ativação, testes e interface acompanham este handoff. As cópias anteriores dos contratos permanecem no backup local e no histórico Git.

## Run Metrics

Fechamento desta correção: 2026-09-06T07:38:41.152858+00:00. Tokens/custo monetário por tarefa indisponíveis. Cota compartilhada consultada às07:34 UTC:72% semanal usado,28% restante; nenhum reset consumido.

## Policy Compliance

VisibleWindows=0 nos processos headless e transporte. Nenhum novo login, gate, framework, worktree, PR, permissão global, hook desativado, Titan local reiniciado ou mudança nos projetos excluídos. AOF foi somente inspecionado. Aceite do usuário permanece pendente.

## Run Changes

Hermes: seleção de turno executável e persistência do bloqueio após esgotamento; dois arquivos de teste focados. Vigília: projeção atual da tarefa no live log e teste de refresh. Ativação por SHA/imagem existentes, preservando dados e escopo dos cards.

## Discovery Promotions

Pendência AOF acima impede a execução integral do card DOV e precisa de correção na origem. Algumas mensagens persistidas foram observadas com conteúdo/timestamp iguais e IDs diferentes; não há evidência de execução externa duplicada. Não acrescentar uma correção especulativa ao runtime por isso.

## Promotion Candidates

none para os dois patches publicados. A correção AOF continua pendente e não tem candidato nem aprovação de gates fabricada.
