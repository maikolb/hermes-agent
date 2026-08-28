# Arquitetura-alvo do Hermes NF (spec canônica do Maikol, 27/08/2026)

Esta é a spec de comportamento ditada pelo operador. Todo PR de comportamento
do gateway/agente referencia esta spec. Divergência daqui é bug.

## Trabalho

1. O operador manda uma ou várias mensagens e anexos no tópico, em qualquer
   ritmo. Ao marcar o agente, ele analisa TUDO que foi enviado (mensagens e
   anexos acumulados), cria o escopo, checa se já há algo implementado
   (código produtivo, cards, commits), cria o card e trabalha.
2. Se o agente já está trabalhando, delega para workers paralelos que fazem
   EXATAMENTE o mesmo ciclo: analisar, montar spec/escopo, preflight de
   duplicidade, criar card e trabalhar. Tudo seguindo AOF.
3. Queda de sistema, gateway parado, pane, luz, compactação de contexto: ao
   voltar, o checkpoint restaura e os trabalhos voltam. Se o retorno
   automático não der, um humano repuxa e o agente restaura do checkpoint.
   Principal e workers têm CADA UM seu checkpoint e seu retorno; um "volta"
   retoma o principal e os workers onde estiverem.
4. Bloqueio (principal ou worker): move o card, publica closeout AOF com o
   motivo, e a vida dos outros segue tranquilamente.

## Configs

- Limites maiores de escrita e busca; permissão de scratchpad; respeitar
  caminhos já promovidos do AOF. As máquinas são robustas: limite de
  disco/arquivo é generoso; limite de contexto de LLM é o único justificado
  caso a caso.

## AOF

- Enforcement REAL (não textual): exigir escopo, discovery promotions,
  protocolo de regressão, aprendizado (promoção de lições), closeout.

## Visibilidade

- Tag "Trabalhando..." durante execução.
- Fim do trabalho principal: closeout AOF com o trabalho feito; em seguida
  "now watching" no próximo worker ativo.
- Worker terminou: closeout AOF com o resultado DELE; "now watching" no
  próximo; até acabar. Dois workers terminando juntos publicam dois
  closeouts: são independentes, como cópias do principal.
- Tudo sincronizado no Vigília: visibilidade externa equivalente ao
  Telegram.

## Aceite (cenário canônico, ditado 27/08)

Tópico real de teste (ex.: DOVTest). 7 tarefas paralelas: duas terminam
juntas, uma termina depois, duas bloqueiam, as demais entregam em
sequência. Verificar no fluxo: pull das conversas anteriores ao marcar,
tag "Trabalhando...", now watching, closeout AOF por worker e do
principal, blocked movendo card + closeout com motivo, checkpoint
(queda/retomada), Vigília sincronizado com animação. O relatório final
apresenta a spec por categoria com check por item e resumo do
funcionamento.

## Gap map (27/08/2026, ordem de ataque)

1. Compaction não pode arquivar mensagens humanas não-processadas
   (observed/user sem resposta ficam ativas). Causa central da degradação
   de "entender tudo que foi enviado".
2. Steer com mídia: anexos permanecem ligados ao texto da mesma mensagem.
3. Turno retomado recupera project context (fix do gate internal=True no
   resolver do router: resolução read-only para eventos internos).
4. Plugin aof_route_policy degradado (AdapterConfigurationError): consertar
   para o enforcement voltar a ser real.
5. Closeout AOF por worker concluído no display (rastro por worker, não por
   lane) e closeout do principal ao fim do turno.
6. Delegate injeta o protocolo AOF (escopo→preflight→card→trabalho→closeout)
   no goal de cada worker.
7. Retomada re-dispara delegated que morreram com o turno; entrega
   assíncrona (completion wake) confiável — caso 27/08 19:04: workers
   concluíram e o principal só percebeu 1h depois lendo transcripts na
   mão, e a orquestração ficou dependente da inteligência do agente em
   vez do processo.
8. Auditoria de limites de escrita/busca do fork + scratchpad + paths AOF.
   Resultado (27/08): os tetos principais JÁ são config, não código —
   `tool_output.max_bytes/max_lines/max_line_length` (default 50KB/2000/2000),
   `model.max_tokens` (default: provider), busca sem teto rígido
   (`DEFAULT_SEARCH_LIMIT` 50 é só default por chamada), `MAX_FILE_SIZE`
   50KB é warning, não bloqueio. Mudança de código: trace de closeout do
   worker no chat subiu 700→1600 chars (o closeout estruturado do gap 6
   não cabia). Pendências de CONFIG DE PERFIL (janela de deploy, VPS):
   subir tool_output no perfil factory, conferir model.max_tokens do
   perfil, declarar scratchpad permitido no scope validator do AOF e
   ligar kanban.agent_wake_on_events para o principal acordar em
   conclusão de card despachado.
9. route_to_dispatcher default vira opt-in explícito (falso por default).
