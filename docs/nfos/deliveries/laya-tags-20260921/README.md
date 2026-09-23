# Checkpoint Laya e hashtags, 21/09/2026

**Não está pronto para uso por hashtags em produção.** O código está no [PR231](https://github.com/maikolb/hermes-agent/pull/231). Há instalação e inferência real na VPS, mas ativação, qualidade semântica, compatibilidade do serviço e Jev têm pendências distintas. Economia real medida: **zero chamadas Principal**.

## Evidências por critério

| Critério | Resultado e origem verificável | Print real do artefato |
|---|---|---|
| A: inferência VPS | [Inferência compatível5.0](compatible-inference.json), 221ms, picoRSS2,28GiB; pesos conferidos contra LFS oficial em [readback](runtime-readback.json). Warm service ainda4.57; não confundir os processos. | [Compatibilidade](evidence-final.png), [warm inicial](evidence-inference.png) |
| B: intake/eixos/retry | [34 testes nativos](intake-tests.json): sem tag, combinações, conflito atômico, contexto autorizado, persistência, retry e continuação. Nenhuma entrega Telegram real. | [Checks](evidence-checks.png) |
| C: quatro usos | [92 testes integrados](integration-tests.json), depois [13 testes de source binding](source-binding-tests.json). [SPEC real inicial](native-spec-live.json), [ações reais iniciais](native-actions-live.json), [recusa de truncamento](live-guards.json). Esses testes reais nativos usaram4.57; repetição nativa com5.0 pendente. | [Nativo inicial](evidence-native.png), [checks](evidence-checks.png) |
| D: runtime pretendido | [Readback](runtime-readback.json): gatewayPID4191054, main6381c19b79; configuração NFOS não ativada. Serviço experimentalPID1416548 somente127.0.0.1:18991, CPU2, limite6GiB. | [Readiness e bloqueios](evidence-final.png) |
| E: Jev | [Inspeção de disponibilidade](jev-availability.json): nenhum provider/chave suportado configurado nas fontes inspecionadas. Nenhuma inferência Jev inventada. | [Bloqueios](evidence-blockers.png) |
| F: entrega/preservação | [PR231](https://github.com/maikolb/hermes-agent/pull/231), [checks](pr-checks.json), [21 arquivos WIP preservados](wip-preservation.json), [coordenação](coordination-status.json), [reconciliação](reconciliation.json). Merge/release ainda ausentes. | [Final](evidence-final.png), [preservação](evidence-blockers.png) |

Os PNGs são capturas headless reais de páginas que exibem os JSONs de execução e seus hashes. Não são screenshots de Telegram nem da aplicação em produção. Recibos integrais e jobs reproduzíveis ficam em `C:/Users/maiko/AppData/Local/hermes/cache/nfos-jev/implementation/laya-tags`. Os recibos de execução/captura registram `VisibleWindows=0`.

## Resultado técnico e limitações

O checkpoint multilingual tem1024 tokens e head256. O encoder declara8192, mas essa janela não foi promovida a capacidade validada. O SDK corta estado, instruções e opções; o serviço detecta esse corte antes de inferir e retorna422. Revisão real preservou o texto integral; os casos não foram aceitos por truncamento.

Transformers4.57.6 interpretava RoPE local como10000, contra160000 do checkpoint. O alvo isolado `/opt/nfos-laya/compat5` usa5.0.0 e preserva160000. Mesmo assim, diante de “É proibido apagar registros”, o modelo escolheu `apagar` com confiança0,9227. Nenhum comando foi executado. Essa é falha semântica real, não prontidão de substituição. Os quatro casos SPEC e os três usos operacionais iniciais fizeram fallback por baixa confiança.

O script final exige Transformers5.0.0 e o template aponta para o alvo compatível. Essa versão final do script não foi ativada. A revisão automática rejeitou a parada/recriação apenas de `nfos-laya-experiment` com “blocked by policy”, sem motivo específico. [Registro da rejeição](service-switch-rejection.json). Não houve tentativa por outra rota.

O masterPID24248 foi preservado. Seu proxy oficial falhou com socket10050, sem mensagem entregue ou confirmação. Nenhum lock de sessão, app-server ou banco foi alterado. A ativação NFOS continua sujeita à confirmação do candidato integrado pela thread01a0a8ad-dd7b-7000-9528-77e45c50e8c3.

CI do PR ainda não está verde: [teste de áudio macOS falhou](ci-macos-failure.json). O arquivo de áudio não foi alterado nesta tarefa; reprodução da falha na baseline macOS não foi feita. Outros checks estavam pendentes no readback. Não houve merge forçado.

## Arquivos e ativos

Alterados no candidato: `hermes_cli/nfos_delivery.py`, `hermes_cli/nfos_jev.py`, fixture`test_nfos_jev.py`, novos`test_nfos_laya.py` e`test_nfos_decision_engine_tags.py`, serviço`nfos_laya_server.py`, template`nfos-laya.service`, guia`docs/nfos/laya-tags.md` e esta pasta de evidências. Mais de5 arquivos são necessários para os dois módulos nativos, testes dos contratos, serviço separado, guia e evidências expressamente pedidos.

Instalados na VPS: `/opt/nfos-laya/venv`, `source`, `model`, `compat5`, artefato Git46be2c76a, unidade transitória experimental e diretórios sintéticos de teste. Não alterados: entrypoint`/usr/local/bin/hermes`, config do perfil, gateway produtivo, workers, SQLite produtivo, Suporte, Vigilia, containers, pins de worker/Principal, checkouts dirty originais e suas21 amostras. Nenhuma compra, credencial nova ou mensagem a cliente.

## Continuação e retorno

1. Conferir os refs atuais e os checks do PR231. O código funcional antes do guard de versão está emc0033d81ae74ca2be43eef615384234afaa18fb3; a atualização documental/guard posterior é rastreável pelo PR.
2. Resolver o bloqueio concreto da troca experimental pelo fluxo permitido. A unidade atual é transitória, iniciada11:13UTC, com limite2h e código46be2c76a. Não é instalação persistente nem runtime NFOS ativado. Preservar outros processos.
3. Usar o [lock compatível](requirements-compatible-5.0.lock) e repetir os dois scripts nativos em home sintético com o serviço compatível. Não baixar o limiar de confiança para esconder o contraexemplo. Qualidade semântica precisa sustentar os usos que substituiriam Principal.
4. Jev requer provider/chave/cota já autorizados; não escolher conta paga alternativa nem comprar créditos automaticamente.
5. Somente com evidências, checks verdes e confirmação do master do SHA integrado, concluir merge/release imutável/configuração/ativação. [Guia e diff de configuração](../../laya-tags.md).

Rollback produtivo não é necessário, pois não houve ativação. Não restaurar SQLite antigo. A unidade experimental expira sozinha; limpeza posterior deve abranger somente seus diretórios de teste e seu processo. Modelo/venvs podem ser preservados para continuação. As refs anteriores do candidato continuam preservadas, sem reset, stash ou alteração dos checkouts originais.
