# Runbook de aceite da spec (TARGET_ARCHITECTURE)

Roteiro da janela de validação end-to-end ditada pelo operador (27/08):
tópico real, 7 tarefas paralelas, duas acabando juntas, uma depois, duas
bloqueando, entregas em sequência; closeouts, AOF, now watching, tag
Trabalhando, Vigília animado, checkpoint, pull de backlog no mention.

## Fase 0 — pré-condições

- [ ] PR #37 mergeado na main do fork (gaps 2/5/6/7/8/10 + higiene de teste).
- [ ] Suite de gateway verde no host de dev com sandbox armado (sem som,
      sem escrita no home vivo — telemetria da sessão forense).
- [ ] Janela de restart do gateway NF confirmada pelo operador (moratória
      só cai com ordem explícita).

## Fase 1 — configs de perfil (VPS, hermes-project-factory)

Pendências do gap 8, aplicar no config.yaml do perfil (backup antes):

```yaml
tool_output:
  max_bytes: 120000        # 50k -> 120k (~30k tokens por página de terminal)
  max_lines: 4000          # 2000 -> 4000
  max_line_length: 4000    # 2000 -> 4000
kanban:
  agent_wake_on_events: true   # principal acorda em conclusão de card despachado
# conferir: model.max_tokens ausente (= default do provider) ou >= 16384
# delegation.worker_parity: true é o default novo — NÃO precisa declarar
```

- [ ] Scratchpad: garantir diretório de rascunho permitido no scope
      validator do AOF do perfil (ex.: /srv/hermes/scratch) — anexar ao
      escopo declarado, não substituir.

## Fase 2 — release com gates (procedimento canônico)

1. `git fetch` no clone da VPS; SHA alvo = main pós-merge.
2. Dir novo IMUTÁVEL `/usr/local/lib/hermes-agent.release-<data>-<sha>`;
   venv com freeze de paridade do release anterior + extras.
3. Gates NO VENV do release: subsets canônicos (delegation, kanban
   notifier/rotação, steer/busy, retention, internal-context) — pipefail,
   saída gravada em RELEASE_EVIDENCE.
4. Quiesce: `/root/hermes-quiesce-check.sh` até zero cards Principal/worker
   running com heartbeat fresco (aborta a janela se houver trabalho vivo).
5. Repontar symlink `/usr/local/bin/hermes` + restart
   `hermes-gateway@hermes-project-factory` + smoke (adapter conectado,
   bindings, board acessível, aof-route-policy sem degraded).

## Fase 3 — cenário DOVTest (mapa spec → passo)

Setup: tópico novo "DOVTest" no grupo da factory + binding no router
(project dovtest, board dovtest) + ACL do operador. Nada de produção.

| Passo | Ação | Item da spec provado |
|---|---|---|
| 1 | Mandar 3 mensagens + 1 anexo SEM mention; aguardar 2 min; dar mention | Trabalho 1: analisa TUDO acumulado (retention gap 1); escopo+preflight+card antes de trabalhar |
| 2 | Pedir a delegação de 7 tarefas paralelas (goals sintéticos controlados: T1/T2 curtas e simultâneas, T3 longa, T4/T5 com bloqueio induzido — recurso inexistente, T6/T7 médias em sequência) | Trabalho 2: workers com o MESMO ciclo (protocolo gap 6); cards running com heartbeat; worker parity (gap 10): workers citam contexto/decisões herdadas em vez de redescobrir |
| 3 | Observar displays durante execução | Visibilidade: tag "Trabalhando...", now watching 1/7 com rotação, Vigília animando cards + colunas com rolagem |
| 4 | T1+T2 terminam juntas | Dois closeouts independentes no chat (gap 5), cards done |
| 5 | T4+T5 bloqueiam | Card movido pra blocked + closeout com motivo (gap 5/blocked); vida dos outros segue |
| 6 | Durante turno ocupado: mandar voz + imagem steered | Gap 2: anexos chegam JUNTOS no turno (sem fragmentar) |
| 7 | Restart controlado do gateway NO MEIO (com T3/T6/T7 vivas — tópico de teste, janela combinada) | Trabalho 3: checkpoint; crash marker re-entrega goals (gap 7); cards re-claim; principal retoma e re-delega como PROCESSO |
| 8 | T3 termina depois; T6/T7 entregam em sequência | Closeouts na ordem real de término; completion wake confiável |
| 9 | Fim do turno principal | Closeout AOF do principal; board consistente (7 cards terminais) |
| 10 | Conferir Vigília ao final | Sincronizado "igual Telegram": kanban + atividade + logs completos + deep-links |

## Fase 4 — relatório final ao operador

Apresentar a spec POR CATEGORIA (Trabalho / Configs / AOF / Visibilidade)
com check por item + evidência (mensagem, card, print, log) + resumo de
funcionamento. Encerrar com closeout AOF (Run Metrics, Policy Compliance,
Discovery Promotions).
