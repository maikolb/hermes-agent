# System One: cobertura completa dos pontos implementados, produção em HOLD

O índice normativo desta edição é coverage-matrix.json. Cada linha separa implementação, fixture, inferência real, execução nativa e limitação. Todos os testes usam HOME/SQLite e recursos descartáveis, sem posts Telegram, cards produtivos, merge, deploy, restart, alteração de cron ou treinamento.

## O que foi provado

| Ponto | Resultado e fronteira |
|---|---|
| Orçamento | Jev e Laya testados em save_spec. Defeito reproduzido: fallback + sizeG elevava1200 para14400s. Corrigido somente no modo ativo de orçamento System One; ambos mantiveram1200 no reteste. Pins e run preservados. |
| Worker: início de etapa | Jev e Laya selecionaram e executaram sonda permitida; o resultado foi anexado ao tool result do worker. Investigação só é coberta como mudança de etapa. |
| Worker: erro transitório | Ambos executaram a medição permitida após primeiro erro reconhecido. Nenhum comando novo foi oferecido. |
| Worker: retry estagnado | Primeiro/segundo erro exercitados; Jev fez uma medição e fallback na outra oportunidade; Laya fez fallback. Terceiro erro não abre oportunidade repetida. |
| Worker: próxima evidência | Ambos executaram uma sonda ao entrar em verify; leitura repetida na mesma etapa não virou nova decisão. |
| Impedimento | block_task real: Jev mediu31/PASS, continuando o mesmo run; mediu29/FAIL, voltando ao fluxo Principal. Laya fez fallback nesses estados completos. Host não autorizado e SQL DELETE foram filtrados antes de qualquer inferência/efeito. |
| Evidência | save_report coletou artefatos reais quando houve escolha executável, sem repetir sondas no retry. SPEC alterada invalidou revisão anterior. Nenhum motor resolveu final_review como continue. |
| SPEC | Fechada a lacuna de aceite real: caso novo de2+3 aceito por Jev seguiu a implement sem segunda revisão Principal. Outro caso novo fez fallback. Changes/fallback anteriores continuam válidos. Laya SPEC permanece excluída. |
| Aprendizado | Collector retrospectivo leu28 trajetórias reais. Export/replay offline:54 candidatos,12 PASS,3 FAIL,39 NOT_PROVEN;13 test e41 quarantine. Sem LLM narrativo pago nem treinamento. |
| Hashtags/resume | Intakes, conflito atômico, #deepseek independente, ausência, cross-card e persistência testados em SQLite/HOME isolado. Não houve chamada DeepSeek nem transporte Telegram. |
| Vigília | Backend real e print atual do aceite Jev; controles e rotação reutilizam testes exatos de código inalterado. Nada foi configurado na UI produtiva. |
| Recursos | Chamadas seriais, limites nativos, SQLite livre durante HTTP, shadow sem efeito. Reutilizada capacidade isolada anterior; não foi feito stress ou teste de10 workers. |

Os28 cenários nativos iniciais foram congelados antes das chamadas. Quatro casos adicionais também foram congelados: teto comsizeG nos dois motores e duas SPECs novas. O reteste do teto repetiu apenas o defeito após o patch. Não se alega heldout novo nem compreensão universal. A seleção de uma sonda entre opções já permitidas não demonstra que o modelo sabe conceder permissões.

O único patch de produto está em nfos_delivery.save_spec: se o orçamento System One ativo não produz estimativa aplicável, o tamanho sugerido pelo worker não pode aumentar o limite existente. Estado desligado/untagged mantém o comportamento nativo anterior. A regressão cobre keep, baixa confiança, HTTP503 e chave ausente. Não houve alteração de limiar, checkpoint, modelo de worker ou aceite final.

## Autoridade e falhas distinguidas

A aprovação inicial das SPECs de medição foi uma fixture explícita de Principal, usada para preparar os testes de sondas. As respostas Jev/Laya e os GETs que contam linhas nos arquivos descartáveis são reais. O recibo de coleta só autoriza inspecionar a medição, não declarar entregue.

Um primeiro assert do harness confundiu final_review antigo encerrado como changes/superseded com aceite. O diagnóstico preservado mostra esse histórico e uma nova revisão pendente. Corrigiu-se o assert, não a autoridade do produto.

O export recusou inicialmente slugs de projeto fornecidos incorretamente pelo harness. A coleta final usa o identificador canônico do diretório do board, igual ao runtime; a validação não foi afrouxada. Os41 registros ainda em quarentena têm contexto/versões incompletos. O Revisor narrativo foi exercitado com fixtures nos testes, não com nova chamada paga. Existe train/test/quarantine, mas nenhuma partição de calibração separada implementada.

## Evidências e custo

60 testes focais passaram para routing/Laya/dataset/Revisor/online;88 passaram após o patch de orçamento, com sobreposição entre as suítes. Ruff e diff-check passaram. Screenshots são do servidor candidato real no HOME de teste; não são telas produtivas. Todos os supervisores reportam VisibleWindows=0.

Custo desta etapa: US$0.000490350. Acumulado desde a autorização original: US$0.002434362 em53 requests OpenRouter; restante autorizado US$2.877565638. Responses, key usage e account usage reconciliados. O limite continua sendo o mesmo US$2,88, sem reset. A chave protegida não foi duplicada, rotacionada ou exportada.

Consulte point-results.json para cada escolha/fallback, efeitos e latência; native-results.json para eventos e artefatos; extras-results.json/extras-fixed-results.json para antes/depois; learning-cli-* para export/replay; cumulative-ledger.json para custos; coverage-matrix.json para o vínculo por critério. Ausência/timeout/429 e corrupção de contrato são testes com fixtures explícitas, não resultados semânticos de modelos reais.

## Ativação e rollback

Produção permanece em HOLD até autorização futura real do proprietário. Seguir o overlay e runbook preparados: reconciliar então-current main/runtime/candidato, preservar fixes aceitos, coordenar SHA integrado/slot com o master e criar backup imediatamente anterior à troca. Nada publica por palavra-chave ou cron.

O rollback comprovado continua sendo o doSHA2f2c0b990e666cd97184dcbe3dd2a3295ca1c652, preservando cards/runs novos. Esta mudança não altera schema; o novo manifesto recebe dry-run, não alegação de novo ensaio completo. Os statuses aninhados de SPEC e rollback foram corrigidos. Não restaurar SQLite antigo nem apagar memória/credenciais/CCM ou trabalho posterior. CI remoto/billing é registrado separado dos testes locais; não foi contornado.
