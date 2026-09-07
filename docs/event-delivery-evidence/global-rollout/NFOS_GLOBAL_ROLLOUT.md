# Ampliação do NFOS para os projetos

Pedido vigente de Maikol em 07/09/2026: habilitar o fluxo novo em todos os projetos. Corrigir arquitetura, sem instruções pontuais aos workers, desbloqueios manuais ou execução das tarefas pela manutenção.

## Escopo

- R01: entrada Telegram usa configuração do projeto, identidade persistente e um único despacho.
- R02: cards abertos sem executor vivo são adotados no mesmo ID. Preservar pedido, comentários, dependências, workspace, arquivos e execuções. Não fabricar spec/relatório/resultado.
- R03: impedimentos anteriores chegam à fila persistente do Principal. Somente a decisão real do Principal ou resposta humana apropriada permite retomada. Migração e decisão/retomada são atômicas e idempotentes.
- R04: backlog e concluídos permanecem em seus estados; executor vivo termina no protocolo que carregou. A próxima execução inacabada usa NFOS.
- R05: configuração ativa de todos os projetos reais da VPS, com origem e repositórios próprios. Concursa usa `/srv/projects/Concursa_ai/source`. Limites existentes de capacidade preservados.
- R06: verificar versão implantada, encaminhamento real e projeção no Vigília. Autonomia completa não é inferida de testes unitários ou de um único card entregue.

TDD inicial: `outputs/nfos-12h/global-rollout-red-02.log`, cinco falhas comportamentais e um controle aprovado. O primeiro RED continha também um erro de fixture, corrigido antes da implementação; não é contado como prova comportamental.

**Ativado em produção em 07/09/2026, 19:05 BRT.** Gateways default e hermes-project-factory confirmados em `running` às 19:07, com código `53c5f68b24c4d8a40766af1d34990e92e664e20e`. PR [#90](https://github.com/maikolb/hermes-agent/pull/90) integrado em `67cfd47d1139b763c93489ac02d92f98942a3e59`; árvore integrada igual à testada, `882eac4272a9a2d6898c808dfdac87c0b1f89689`.

## Resultado por requisito

| Item | Resultado comprovado | Evidência e print |
| --- | --- | --- |
| R01 | PASS nos contratos de entrada e recibos de vários projetos. Nova configuração carregada em produção. NOT_RUN: pedido novo real em cada tópico após esta publicação. | `global-rollout-linux-artifacts.json`, testes intake 8/8 e receipts 11/11; `global-rollout-evidence/contracts-linux.png`. |
| R02 | PASS. 108 cards abertos adotados nos mesmos IDs. Comparação com backup consistente: nenhum card, vínculo, comentário, anexo, evento ou ID de execução anterior desapareceu; título, descrição, criação e caminhos de workspace preservados. | `global-rollout-live-03.json`, `global-rollout-preservation.json`; `contracts-linux.png` e capturas dos eventos reais do Vigília. |
| R03 | PASS no contrato e início real de retomada. 35 impedimentos foram registrados na fila do Principal. Às 19:12 ele havia resolvido dois por `continue`; o distribuidor criou o run430 no mesmo card DOV t_427dcfe4, PID3481337 com identidade confirmada. | `global-rollout-live-05.json`; decisão DOV `dec_ad1fc9e78a06e7368ca4197e`, decisão Concursa `dec_abad00d5b4b433c507576147`; `dovcrm-events-desktop.png` e `concursa-ai-events-desktop.png`. |
| R04 | PASS nos contratos de exclusividade, PID vivo/reutilizado, backlog, dependências, conclusão e revisão. Na publicação não havia processo de worker ativo; nenhuma execução foi encerrada pela manutenção para fabricar uma janela. | `global-rollout-activation-check.json`, `global-rollout-activation-read-03.json`, `global-rollout-preservation.json`; `contracts-linux.png`. |
| R05 | PASS para ativação dos dez quadros cadastrados. Somente `kanban.delivery.projects` foi alterado semanticamente nos dois arquivos de configuração; demais definições preservadas. Sem requisito de login novo, reativação do AOF ou troca de modelos/memória. | `global-rollout-live-06.json`, campos `other_settings_unchanged=true`, `project_count=10`, `drain_present=false`; `global-rollout-evidence/deployment.png`, captura do relatório de consultas reais. |
| R06 | PASS para fonte carregada, adoção sem duplicação, primeiras decisões/retomada e projeção em desktop/celular. NOT_RUN: conclusão autônoma de uma jornada de cada projeto após a migração; o acompanhamento permanece ativo. | `global-rollout-live-05.json`, `global-rollout-evidence/concursa-ai-projection.json`, `dovcrm-projection.json`, quatro PNGs reais. APIs do board e live log retornaram 200, sem erro JavaScript nas duas capturas. |

Os caminhos acima são relativos a `outputs/nfos-12h/` desta tarefa. São verificações distintas: presença de arquivo não comprova adequação funcional, estado running não comprova progresso e adoção não comprova conclusão.

## Cobertura dos projetos

| Quadro | Cards adotados | Impedimentos iniciais | Caminho do projeto |
| --- | ---: | ---: | --- |
| Concursa AI | 45 | 16 | `/srv/projects/Concursa_ai/source` |
| DOVCRM | 32 | 10 | `/srv/projects/next-crm` |
| RecuperaCLI | 4 | 4 | `/srv/projects/recuperacli` |
| InfoTributos | 7 | 1 | `/srv/projects/hermes-project-facto-786f51f34a055368/infotributos` |
| Dovtest | 2 | 2 | `/srv/projects/hermes-project-facto-786f51f34a055368/dovtest` |
| Central DEC | 0 | 0 | `/srv/projects/hermes-project-facto-786f51f34a055368/central-dec` |
| Sommus | 0 | 0 | Operação, sem repositório Git cadastrado |
| Mulher Mais Segura | 0 | 0 | Operação, sem tópico Telegram vinculado no cadastro atual |
| Project Factory | 5 | 2 | Quadro compartilhado, preserva workspace de cada card |
| Trabalho | 13 | 0 | Quadro compartilhado, preserva workspace de cada card |

Os três workflows DOV que já existiam continuam preservados, além dos 32 adotados. Não foram ativados quadros de teste, diretórios históricos de chats, perfis inativos ou NFO-Homolog-Lab. O cadastro Dovtest tem Git local sem `origin` disponível; isso não foi silenciosamente resolvido nem apresentado como deploy comprovado. Projetos futuros precisam ser cadastrados; esta publicação cobre os projetos existentes.

## Publicação e recuperação

- Backup consistente dos bancos e cópia dos arquivos alterados em `/var/backups/hermes/nfos-global-53c5f68b24c4d8a40766af1d34990e92e664e20e`.
- Release imutável preparada em `/usr/local/lib/hermes-agent.release-20260907-53c5f68b24c4d8a40766af1d34990e92e664e20e`; hash dos arquivos e importação real conferidos antes da troca.
- Durante a publicação, o procedimento deteve temporariamente os locks de despacho já usados pelo runtime. Nenhuma decisão/card foi editada. Os workers já haviam terminado naturalmente. O drain existente do gateway foi usado para a curta troca dos processos do Principal.
- O marcador de manutenção inicialmente herdou dono root do config e ficou ilegível ao serviço. O dono do marcador criado por esta publicação foi corrigido para o usuário do runtime antes de prosseguir. Isso é uma correção do procedimento de publicação, não uma intervenção nas tarefas.
- Entry point e configurações substituídos atomicamente. Apenas os dois gateways afetados foram reiniciados. Locks liberados e marcadores de drain removidos. Não houve restauração de bancos sobre dados novos.
- O rollback preparado repõe entrypoint/config anteriores; não apaga trabalho produzido após o backup. Nenhum rollback foi necessário.

## Testes e limites

Linux isolado: 39 testes PASS em cinco arquivos pelo `scripts/run_tests.sh`, 713,5s. Mesmos hashes dos arquivos do candidato. RED inicial: cinco falhas comportamentais, um controle PASS. Regressões Windows focadas PASS. Fonte, comando, logs completos, hash do log e captura real do relatório estão no [pacote de evidências](https://github.com/maikolb/hermes-agent/tree/faf98eb54162539c6888b6ddece4f8855f6dca3d/docs/event-delivery-evidence/global-rollout).

CI: Ruff bloqueante, Windows footguns, lint diferencial, Windows-only e E2E PASS. No momento do merge, a suíte Python completa ainda rodava. Leitura posterior às 19:19: terminou com 35.652 testes aprovados, 288 falhas e 380 ignorados; o PR89 anterior tinha 35.635 aprovados e as mesmas contagens de falhas/ignorados. A comparação dos logs registra 286 IDs de falha comuns, incluindo tentativas anteriores aos retries; contagens iguais não comprovam causas iguais. O teste de exclusividade NFOS falhou na espera da fixture de processo na primeira tentativa e passou 4/4 no retry automático, uma instabilidade de teste a acompanhar. Não houve falha nos cinco arquivos focados deste rollout. Nix ainda aguardava executor. O teste macOS de áudio falhou em `test_voice_mode.py:634`, com a mesma falha comprovada no PR89 anterior e arquivos de áudio sem alterações contra a base. A CI ampla não está verde. Nenhum check foi removido ou desabilitado. Fontes: `global-rollout-git-read.json`, `global-rollout-ci-comparison.json` e jobs 101870277181/101856108970.

CPU steal de 75–85% foi medida durante esta janela. O worker Concursa registrou build/typecheck incompletos com essa limitação e não publicou o candidato `6780f7ca`. O novo NFOS preservou essa entrega inacabada e a encaminhou ao Principal. Não houve correção de infraestrutura global nem repetição de build por esta manutenção.

## Desfecho da dupla DOV

Em `global-rollout-pair-closure.json`, ambos constam done, com relatório/spec, revisão aprovada, evidências verificadas e worker/filhos encerrados. Áudio t_c208ca00/run417 preservou o ANALISE.md original, hash `b36cae8ed1f59b0d1e7d7434835a44e1ea16a9b1be2fe3a7aeda8221171e3211`.

Lead Scoring t_3bd4a964/run426 entregou a demonstração HML e foi confirmado por Maikol como correta nesta conversa. Spec2, report4, critérios C1–C8, scores 0/30/50, imagens atuais e revisão `dec_cbbee1cdedc24aa5b386`. A revisão distingue entrega demonstrativa de correção de produto: filtro Quente FAIL, ordenação causal não comprovada e Recalcular apenas visível. Não houve código/deploy nesse card. Há assistência anterior registrada; os aproximadamente 1h40 observados por Maikol não são uma média nem prova de autonomia integral. Evidência detalhada: `global-rollout-pair-report.json`.

## Continuidade

A automação existente `nfos-acompanhar-dupla-dov-e-analisar-concursa` foi atualizada para observar o fluxo global e os casos do Concursa, silenciosa quando nada muda. Não despachar lotes manualmente nem orientar workers. Somente defeitos comprovados de arquitetura justificam correção com testes. A cobertura funcional de cada projeto permanece sendo conferida pelo fluxo real.

## Coordenação do Vigília

Vigília é componente interno de `maikolb/nexa-factory-os`, em `modules/vigilia`. A `main` remota foi conferida em `77109f69be4a3c428ca4d6d2fad4b139d359c13e`, PR #34. Contém a integração Codex `f085efd` e os commits do frontend do Claude até `481f92f`. A única diferença do módulo entre a branch final do Claude e a main é ajuste de CRLF no harness de teste.

A limpeza relatada pelo Claude não removeu o checkout ativo do runtime Hermes: `C:/Users/maiko/Projetos/default-64c4270e467b6d39/new-chat-f520c76c60/assignee-publish-worktree`, branch `fix/nfos-12h-dov-20260907`. Não houve restauração de worktrees ou troca do checkout do frontend por esta tarefa. Próximas publicações do Vigília devem derivar da main integrada do NFOS, não do antigo repositório independente ou das antigas branches.

## Não confundir entrega e migração

O par DOV foi concluído; a observação agora cobre as novas retomadas. Às 19:19 BRT, DOV t_427dcfe4/run430 e Concursa t_81d88134/run390 tinham processos vivos com identidade confirmada, após decisões reais do Principal. RecuperaCLI também tinha uma decisão resolvida. A autorização posterior para migrar todos os projetos permite alterar configuração/runtime; não autoriza criar mensagens como se viessem do usuário nem tomar decisões pelos gestores dos cards. O fluxo normal passa a ser responsável pelos cards retidos.
