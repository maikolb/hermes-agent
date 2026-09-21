# Jev opcional no NFOS

Implementação isolada sobre Hermes 6381c19b79d69bcee07d41442c2466d70a1a2471.
Nenhum deploy faz parte desta entrega. Jev fica desligado sem configuração explícita.

## Configuração

No config.yaml do perfil que executará o NFOS, use um bloco kanban.delivery.jev.
Não altere o modelo do worker para Jev. As chaves ficam somente no ambiente desse processo.

~~~yaml
kanban:
  delivery:
    jev:
      enabled: false
      provider: vercel
      model: typesafe-ai/jev
      api_key_env: AI_GATEWAY_API_KEY
      uses: [budget, spec, impediment, evidence]
      timeout_seconds: 2
      min_confidence: 0.8
      spec_review_mode: auxiliary  # primary habilita revisão primária somente da SPEC
~~~

Para escolher outro provider, substitua os três campos correspondentes:

| provider | model | api_key_env | Endpoint fixo |
|---|---|---|---|
| vercel | typesafe-ai/jev | AI_GATEWAY_API_KEY | https://ai-gateway.vercel.sh/typesafe/v1/systemone |
| openrouter | jev-1.13 | OPENROUTER_API_KEY | https://openrouter.ai/api/v1/systemone |
| typesafe | jev-1.13.0 | TYPESAFE_API_KEY | https://api.typesafe.ai/v1/systemone |

Sem model/api_key_env, o provider usa os defaults da tabela. Não há fallback entre providers ou modelos.
Pode habilitar apenas os usos desejados. Timeout permitido: 0.1 a 5 segundos, sem retries.
O limite de confiança é uma política configurável, não calibração comprovada em português/NFOS.
Erros, ausência de chave e baixa confiança preservam o caminho anterior.

## Entradas efetivas

- save_spec seleciona P/M/G, grava a estimativa no card e aplica o limite sem ultrapassar o teto corrente. Preserva modelo/provider/effort fixados e consumo acumulado. Se reduzir o prazo expiraria o run atual, conserva o limite corrente. Não renova tentativas.
- save_spec em auxiliary envia checagem por critério ao spec_review existente. Em primary, Jev pode aceitar a SPEC ou devolver changes diretamente ao worker, com autoria Jev e recibo canônico. Não adiciona revisão no modo que não a exige.
- block_task tenta uma coleta útil antes de abrir impedimento ao Principal, depois das verificações de dependência/decisão pendente. Somente sondas sql/http/header já presentes na spec atual e permitidas pelo executor são candidatas. Executa run_probes, registra recibo e responde ao worker com blocked=false, status=running e resultado da coleta. Isso não afirma resolução do impedimento. Sem ação aplicável, segue a rota anterior.
- save_report pode coletar uma medição faltante antes da conferência de evidências existente. Origem, destino, prova e aceitação continuam sob as regras nativas. PASS/FAIL de uma sonda vem de seu executor, nunca de Jev.

A recuperação inicial é deliberadamente limitada a sondas existentes sem medição atual. Não há geração de comandos, criação de credencial, reparo arbitrário, deploy ou mudança de permissões pelo classificador. Problemas que exigem outros passos seguem o executor/Principal atual. Uma sonda já medida, inclusive FAIL/indeterminada, não é repetida automaticamente sem alteração de spec/mutação.

As chamadas não ocorrem dentro de transação SQLite nem sob o lock do dispatcher. Respostas que chegam depois de mudança da identidade do trabalho são descartadas. Uma reserva em task_events limita a uma chamada por uso/contexto/configuração material. Spec/orçamento podem reutilizar resposta válida; ações não são reexecutadas pelo cache. Nenhuma tabela ou serviço novo.

Eventos nfos_jev registram provider, modelo pedido/real, uso, latência, usage e custo quando fornecido, ou motivo do fallback. nfos_jev_budget_applied separa estimativa de limite. nfos_jev_action aponta para as medições nativas. Nenhum evento contém chave, resposta HTTP integral ou raciocínio privado.

## Status e smoke explícito

Use o Python da instalação candidata, com HERMES_HOME apontando ao perfil desejado:

~~~text
python -m hermes_cli.nfos_jev status
python -m hermes_cli.nfos_jev smoke --live
~~~

status é read-only e não faz rede, inicializa banco ou dispara jobs. Distingue disabled, unavailable (chave/config ausente/inválida) e configured_not_validated_live. Não considera existência da chave prova de disponibilidade.

smoke --live envia UMA pergunta sintética ao provider configurado, se enabled=true. Pode gerar custo conforme a conta; não troca para oferta paga automaticamente. Sua saída distingue unavailable de validated_live e inclui usage/latência. O smoke grava um recibo sanitizado em HERMES_HOME/cache/nfos-jev-smoke.json. O status lê esse recibo sem rede e informa validated_live ou unavailable, com data e modelo, somente enquanto configuração e credencial correspondem. É prova datada daquela execução, não disponibilidade atual nem SLA. Configurar provider/model/chave e enabled=true é suficiente para alcançar os quatro usos; nenhum patch posterior é necessário.

Esta entrega usa fixtures HTTP locais, NÃO inferência live. Não há alegação de ganho de latência, calibração, redução de custo ou bloqueios. Avalie isso com tarefas representativas antes de uma futura ativação produtiva. A promoção gratuita de um provider não é garantia de cota ou preço futuro.

## Validação e retorno

Testes: tests/hermes_cli/test_nfos_jev.py, executados pelo scripts/run_tests.sh, com HOME/HERMES_HOME/banco isolados. O HTTP fixture é explicitamente local; os testes dos probes coletam uma resposta HTTP real desse alvo de teste, não de produção. Regressões nativas cobrem aceitação, delta de relatório, autonomia, escalação, posse do worker e HML.

Desativar: enabled=false (ou remover o bloco). Caminhos anteriores não fazem chamadas nem gravam decisões Jev. Isso não apaga medições/specs que tenham sido legitimamente produzidas durante uma futura ativação.

Branch: codex/nfos-jev-integration. Ref de retorno: baseline/nfos-before-jev-20260920. O bundle de entrega inclui essas refs; git bundle verify confere integridade/pré-requisitos. O manifesto externo registra SHAs e teste de restauração. Branch/bundle preservam código, não banco/configuração/arquivos dirty dos checkouts originais. Esses originais não são editados. Não restaurar banco antigo sobre dados novos. Como não houve ativação, abandonar a branch não demanda rollback de produção.

## Contratos usados

- https://docs.typesafe.ai/api
- https://openrouter.ai/docs/guides/community/typesafe-sdk
- https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe

Corpo model/state/questions e resposta answers/model/usage. Não usa chat/completions ou um SDK adicional.

## Revisão primária da SPEC (opt-in)

Configure enabled: true, uses contendo spec e spec_review_mode: primary no mesmo bloco kanban.delivery.jev. O default permanece auxiliary e Jev globalmente OFF. Provider, chave, endpoint, timeout e comandos status/smoke continuam os mesmos. status expõe spec_review_mode. smoke --live valida conectividade por pergunta sintética; não certifica qualidade da revisão primária.

O runtime envia o texto integral do pedido original, a instrução atual, a spec integral, destino, restrições e limites. Há uma verificação explícita de cobertura do pedido independente dos critérios já listados, além de escopo, destino, restrições e cada critério. Anexo sem conteúdo revisável, informação removida por sanitização, contexto acima do limite, resposta parcial/inconclusiva ou contraditória não permitem aceite primário: segue o Principal atual. URLs sem credenciais são preservadas para comparar o destino; segredos não são enviados.

- Accept: grava decisão spec_review resolvida por Jev, com provider/modelo real, identidade e recibo. require_spec libera a implementação dentro da autorização existente; ask reutiliza esse aceite e não convoca Principal para a mesma revisão.
- Changes: grava resposta nativa com códigos de lacuna/critério convertidos em instruções fixas de correção. save-spec retorna spec_review e o show/wait expõe a mesma decisão; tentar avançar devolve a correção. Worker corrige e salva nova spec no mesmo card/run, sem Principal repassando texto. Informação investigável fica com o executor existente.
- Fallback: indisponibilidade, baixa confiança ou avaliação insuficiente geram a revisão pendente normal do Principal. Não criam pergunta humana nem bloqueio Jev.

O aceite só vale para a spec/instrução/pedido/run/claim/configuração atuais, com evento de origem correspondente. Alterar payload, recibo, modelo/configuração, credencial, modo ou desabilitar Jev invalida o aceite Jev. Cache auxiliar não vira aceite primário; chamadas repetidas sobre a revisão já decidida reutilizam a decisão legítima. Não há novo parâmetro genérico para um worker passar author=Jev ou forjar assessment.

final_review, completion_ready, complete_accepted e as autorizações de produção, destruição, gasto e pins mantêm suas regras. Aceitar a SPEC não certifica resultado ou concede permissão nova. Inferência live e ganho operacional continuam não comprovados.

Retorno desta evolução: baseline/nfos-before-jev-primary-spec aponta para a55fcc01438e146f352d753e7d12c079f2731077; a baseline anterior 6381c19 permanece preservada. Evidências e bundle novos são versionados, sem substituir os artefatos da primeira entrega.
