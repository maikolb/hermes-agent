# Jev e Laya: avaliação real, produção em HOLD

Estado: EVALUATED_PREPARED_ONLY. Não houve merge, ativação, restart, treinamento, compra ou alteração de config.yaml/cron. Este pacote complementa o System One Upgrade anterior, que permanece histórico.

## Resultado

Foram congelados 16 casos PT-BR antes da primeira chamada pontuada: oito de desenvolvimento (sete SPECs e o contraexemplo original) e oito reservados. Rótulos foram escritos a partir de requisitos sintéticos explícitos, sem professor LLM, e não entraram nos inputs. O arquivo cases-freeze-hash.json fixa os casos. Primeiro houve desenvolvimento com perguntas anteriores, depois as mesmas informações com perguntas revisadas; o conjunto reservado foi executado uma única vez após a escolha da revisão. Reutilização posterior de h_valid ocorreu somente para prova exploratória da entrada nativa, sem nova alteração de perguntas/limiares.

| Reservado | Jev | Laya |
|---|---:|---:|
| SPECs | 8 | 8 |
| Decisões utilizáveis | 3 | 0 |
| Aceites corretos entre 3 válidas | 2 | 0 |
| Correções corretas entre 4 inválidas | 1 | 0 |
| Falso aceite entre 4 inválidas | 0 | 0 |
| Falsa rejeição entre 3 válidas | 0 | 0 |
| Inconclusivos/fallback | 5 | 8 |

Ausência de falso aceite com fallback não demonstra compreensão. Os JSONs também registram o verdict bruto antes do limiar. A amostra não calibra confiança nem estabelece segurança universal.

O primeiro smoke real foi HTTP200 em https://openrouter.ai/api/v1/systemone, modelo servido typesafe/jev-1.13-20260917. Tanto typesafe/jev-1.13 quanto o alias configurado jev-1.13 funcionaram: não houve motivo para trocar o adapter ou usar chat/completions. O contraexemplo original continuou escolhendo ler no Jev e apagar no Laya. Laya SPEC continua inelegível em active. Não houve troca de checkpoint nem redução do limiar 0,80.

## Ajustes demonstrados

As seis perguntas de SPEC mantêm as mesmas classes, chaves, verificação de consistência e resultado tipado, mas usam formulação PT-BR direta. A pergunta de destino agora explicita que ausência de destino exigido não é defeito; a pergunta global julga a SPEC, não uma entrega ainda não executada. Pedido, instrução atual, SPEC inteira, proibições, tenant e destino permanecem no estado. No desenvolvimento, Jev passou de uma para duas decisões utilizáveis entre sete SPECs; Laya continuou sem decisões utilizáveis. Não se alega ganho geral.

A tela real revelou uma contradição de estado: authenticated=true e inference_tested=true acompanhados de “autenticação não verificada”. O backend agora apresenta inference_verified apenas com recibo vinculado à credencial atual, e a Vigília traduz esse estado. Rotação da credencial invalida essa verificação. Screenshots antes/depois são do backend candidato real em HOME isolado, via ponte Suporte/CLI, sem respostas simuladas.

O helper de orçamento offline reserva custo por pergunta com Decimal, inclui consumo concorrente e billing atrasado, conserva reserva quando billing é desconhecido e bloqueia endpoint diferente do SystemOne OpenRouter antes de acessar credencial. É infraestrutura pequena de avaliação, não política nova de produção.

## Entrada nativa e limites da prova

save_spec foi executado em HOME/SQLite sintéticos pelo intake nativo identificado como Telegram, sem conexão/post Telegram nem cards produtivos. Jev real fez changes e devolveu a SPEC ao mesmo worker; uma chamada ao Principal foi evitada nesse cenário sintético. A alteração posterior da instrução invalidou a decisão antiga. Untagged e Laya SPEC active não fizeram chamadas de SPEC e mantiveram Principal/pins.

Não foi comprovado accept -> worker com inferência real no contexto nativo completo: a SPEC válida ficou em confiança 0,79 na pergunta global e voltou ao Principal, com limiar 0,80 preservado. O caminho accept -> worker e a ausência de revisão redundante passaram nos testes nativos com fixture HTTP explícita; isso não é substituto da prova Jev ao vivo. Economia em produção: zero, pois não houve ativação.

O primeiro harness usou platform fixture, onde hashtags corretamente não são autorizadas; não fez inferência. O segundo também exercitou orçamento no bootstrap, além de SPEC, e confundiu uma tentativa Laya local com chamada paga instrumentada. Essa tentativa atingiu apenas loopback Windows indisponível, sem inferência nem cobrança OpenRouter; a reserva conservadora e o erro foram preservados. A versão final isola SPEC, usa o intake correto e impede qualquer credencial paga em endpoint local.

## Contexto, CPU e custo

Laya permaneceu no serviço persistente nfos-laya.service PID1508465: CPU, duas threads, Transformers5.0.0, checkpoint multilíngue052592a15d198d9ad47da779604259b10b47b7aa, fonte42626c348753fbb17572a813127df2278a1ec527. Os limites verificados são 1024 para estado/pergunta e 256 no head; 8192 do encoder não foi tratado como contexto validado. Casos longos receberam422/context_too_large antes da inferência, sem truncamento. token_lengths mostra cada pergunta, enquanto usage.input_tokens é a soma das perguntas executadas sequencialmente. Uma soma maior que1024 não é, por si, overflow. A carga foi serial, sem serviço temporário, restart ou alteração de limites.

OpenRouter publicou preço de entrada US$0,000000042/token e saída zero na metadata capturada, contexto32000. A resposta pode informar output_tokens, mas a tarifa de saída permaneceu zero. As respostas, uso da chave e débito total da conta reconciliaram exatamente em US$0.001944012 para35 requests OpenRouter, incluindo smokes, desenvolvimento, reservado e entradas nativas. Restam US$2.878055988 da autorização deUS$2,88; saldo total da conta US$2.878489693. Não se consumiu o orçamento por consumir.

Chave presente/exata no arquivo autorizado /srv/hermes/profiles/hermes-project-factory/.env, variável OPENROUTER_API_KEY, modo0600 e proprietário originalUID999 preservados. Escrita pelo mecanismo nativo sob esse proprietário, via stdin SSH e sem arquivo temporário de chave em cache/Git. Outros valores e config.yaml conferidos. O segredo não está no pacote; autenticação e inferência testada são demonstradas pelos recibos, não só pela presença.

## Verificação, publicação e rollback

88 testes focais passaram antes do ajuste final de status; oito testes focais finais (com sobreposição) passaram para status/rotação, orçamento, billing desconhecido e bloqueio de credencial em endpoint indevido. Ruff, sintaxe JS e UI real passaram; VisibleWindows=0. As quatro classes continuam implementadas; apenas a formulação SPEC e o status exibido mudaram.

O manifesto externo fixa os SHAs finais Hermes/companion e imagem preparada. PRs: https://github.com/maikolb/hermes-agent/pull/231 e https://github.com/maikolb/nexa-factory-os/pull/144. Nenhum merge. CI remoto é registrado separadamente; o bloqueio anterior de billing não foi contornado nem configurado.

A mudança não altera schema/SQLite, formato das decisões ou caminhos de ativação. O ensaio final anterior noSHA2f2c0b990e666cd97184dcbe3dd2a3295ca1c652 continua como prova de rollback dos dados aditivos; esta revisão retesta a vinculação/staleness e faz dry-run com o novo manifesto contra o mesmo baseline protegido. Não afirmar ensaio de rollback novo se foi somente dry-run.

Para ativação futura: seguir OPERACAO do pacote preparado, reler produção/main/candidato, reconciliar releases aceitos, coordenar SHA integrado exato/slot com master, fazer backup imediatamente anterior e usar apenas o overlay versionado por hashtag. A chave agora está pronta, mas isso não ativa nada. Rollback preserva SQLite/cards/runs/memória/credenciais novos; restaura apenas pointer/imagem e quatro chaves do upgrade com recusa de drift. Não restaurar .env antigo removendo a nova chave.

Fontes primárias: https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints e https://openrouter.ai/typesafe. A existência de outros endpoints documentados não foi usada para substituir o SystemOne solicitado.
