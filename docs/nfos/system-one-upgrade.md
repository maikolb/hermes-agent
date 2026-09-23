# System One Upgrade: pacote preparado, produção em HOLD

Este pacote não autoriza ativação. Esta execução prepara código, testes, artefatos e ensaio isolado. Não executar merge, troca de entrypoint, restart, alteração de configuração produtiva, cron ou treinamento. O manifesto da entrega deve fixar os SHAs finais depois do commit.

## Revisor e aprendizagem

O Revisor continua retrospectivo e usa o cron, memória, watermark e fingerprint existentes. O horário efetivo atual de 00h vem da configuração existente; este upgrade não reprograma o job. Uma trajetória sem mudanças não recebe outra análise apenas para alimentar System One.

O status existente acrescenta `system_one`, com política, elegibilidade, provedores e métricas. Atualizar apenas `body.system_one` passa por `nfos_jev.configure_system_one()` sem criar nem alterar o cron. A configuração da futura ativação deve vir pronta no manifesto, com `active` restrito ao opt-in das hashtags e às três classes abaixo. Ela não foi aplicada neste HOLD:

```json
{"system_one":{"mode":"active","engine":"laya","classes":["budget","impediment","evidence"],"timeout_seconds":90,"max_decisions_per_run":4}}
```

`off`, `shadow` e `active` são modos distintos; política ausente continua `off`. A elegibilidade e as restrições da classe continuam verificadas no runtime. O engine da política é o motor em configuração/inspeção; a hashtag persistida é a autoridade da seleção por card. Selecionar engine não troca o modelo do worker. Laya SPEC primária permanece inelegível com a evidência atual; seus resultados negativos não são ocultados nem corrigidos reduzindo threshold. Jev fica preparado com OpenRouter e depende de credencial/quota válida; sua falta não bloqueia Laya. Preparar o adapter não comprova inferência real.

Os eventos `nfos_system_one_opportunity` registram contexto disponível antes da decisão. `nfos_system_one_outcome` registra inferência e verificação posterior separadamente. O export também aceita `nfos_jev` e `nfos_jev_action` históricos, mas coloca em quarentena candidatos sem contexto/versões completos.

Cada exemplo separa:

- `inputs`: snapshot redigido de `state`, opções e referências; nunca resultado posterior.
- `decision`: resposta registrada, engine selecionado/usado e fallback.
- `reviewer_preference`: preferência retrospectiva citada e efetivamente lida pelo Revisor.
- `policy_validity`: `VALID`, `INVALID` ou `NOT_PROVEN`, com escopo explícito de pertencimento às opções tipadas. Isso não autoriza efeitos nem prova atendimento ao pedido.
- `verified_outcome`: `PASS`, `FAIL` ou `NOT_PROVEN`. Um “aprovado” do Revisor ou `typed_action_executed=true` não estabelece sucesso.
- `provenance`: hashes das fontes, revisões de policy/model/tokenizer/head/schema/catálogo e das evidências usadas.

Para reconhecer o resultado de uma sonda nativa, o export exige artefato `probe:<criterion>` do runtime, revisão referenciada, mesmo run/SPEC, configuração da sonda correspondente à SPEC, mutação e tempo compatíveis. Fonte ausente, destino diferente ou evidência antiga mantém `NOT_PROVEN`.

Export e replay são offline e não chamam LLM, não treinam pesos e não alteram cards:

```bash
python -m hermes_cli.nfos_system_one_dataset export \
  --input /caminho/preparado/nfos-reviewer/runs \
  --output /caminho/preparado/dataset.json \
  --train-before 1789257600 --test-after 1789862400
python -m hermes_cli.nfos_system_one_dataset replay \
  --input /caminho/preparado/dataset.json \
  --output /caminho/preparado/replay.json
```

As datas acima são exemplo de janelas, não uma seleção aprovada do dataset real. Componentes ligados por projeto, tarefa ou linhagem não atravessam treino/teste. Grupos que cruzam as janelas temporais entram em quarentena. Fontes conflitantes, contexto omitido ou contaminado por resultados e revisões ausentes também são excluídos. Replay confere as escolhas registradas; não é benchmark de inferência nem prova de ganho ponta a ponta.

## Baseline de hoje e checagem sem ativação

Baseline protegido capturado em `/srv/hermes/releases/system-one-preparation-baseline-20260921/identity.json`. Ele aponta para backups do entrypoint, configuração do perfil NFOS e unit Laya, além de hashes das units afetadas. Não substituir esse baseline para fazer uma divergência desaparecer.

```bash
python scripts/nfos_system_one_prepare.py check \
  --baseline /srv/hermes/releases/system-one-preparation-baseline-20260921/identity.json \
  --receipt /srv/hermes/releases/system-one-preparation-check.json
python scripts/nfos_system_one_prepare.py dry-run \
  --baseline /srv/hermes/releases/system-one-preparation-baseline-20260921/identity.json \
  --candidate-manifest /caminho/preparado/manifest.json \
  --receipt /srv/hermes/releases/system-one-preparation-dry-run.json
```

O helper não possui comando de ativação. `check`/`dry-run` só leem arquivos e `systemctl cat`; hash divergente produz `REFUSED`, exit 2, sem sobrescrever origem ou backup. Exit 0 significa preparação conferida, não release publicada. O manifesto deve conter `sha` com os 40 caracteres do Git integrado.

## Ensaio isolado de rollback

```bash
python scripts/nfos_system_one_prepare.py rehearsal \
  --baseline /srv/hermes/releases/system-one-preparation-baseline-20260921/identity.json \
  --workspace /srv/hermes/releases/system-one-rehearsal-IDENTIFICADOR-NOVO \
  --old-runtime /usr/local/lib/hermes-agent.git-6381c19b79 \
  --candidate-runtime /opt/nfos-laya/preparation/SHA-EXATO \
  --python /usr/local/lib/hermes-agent.git-6381c19b79/venv/bin/python \
  --receipt /srv/hermes/releases/system-one-preparation-rehearsal.json
```

O diretório de ensaio deve ser novo. O helper recusa reutilizar uma pasta existente ou um destino produtivo. Copia runtime/config para dentro do ensaio, cria cards e runs sintéticos pelas APIs nativas em um HOME mínimo separado, grava os eventos aditivos e marcadores sintéticos de memória/credencial. Depois restaura somente o pointer de runtime e as quatro chaves do upgrade, preservando um projeto sintético adicionado depois do snapshot. Abre o mesmo SQLite com o código antigo em modo somente leitura. O recibo precisa mostrar a origem real `6381c19b79`, os novos cards/runs ainda presentes, eventos lidos pelo Revisor antigo e hashes de DB/memória/credencial preservados. Credenciais reais não são usadas como fixture.

O rollback de configuração limita-se a `kanban.delivery.system_one`, `laya`, `jev` e `decision_engines`. Se uma dessas chaves mudou depois da aplicação, ele recusa sobrescrever. Projetos novos, incluindo o projeto CCM observado durante a preparação, e outras configurações legítimas são preservados. Não restaurar o arquivo antigo inteiro sobre o estado atual. O hash de configuração após o ensaio pode ser diferente do baseline justamente por preservar o projeto novo.

O teste local usando a mesma árvore nos dois lados verifica o mecanismo. Somente o recibo VPS com origem antiga explícita comprova compatibilidade com o runtime anterior. Nenhum ensaio equivale a testar entrega Telegram ou ativação produtiva.

## Runbook futuro, não executar neste HOLD

1. Entregar ao master o SHA integrado exato, checks, política efetiva, diff de configuração, resultados negativos, manifesto e recibos. Receber confirmação específica do pacote/slot. A autorização antiga de Laya não substitui o HOLD atual.
2. Antes de qualquer mutação futura, reler refs main/candidate, entrypoints, configurações, units, workers e publicações concorrentes. Preservar alterações aceitas por outras threads. Se houver drift, reconciliar e reapresentar o diff.
3. Usar `/run/lock/nfos-hermes-deploy.lock`, mutex designado pelo master, com `flock` exclusivo não bloqueante. Manter o mesmo descritor até readback final ou rollback. Nunca apagar o lock nem usar `gateway.lock`/`.dispatcher.lock` como mutex de deploy.
4. Preparar release imutável e backups imediatamente anteriores proporcionais, incluindo SQLite via backup consistente. Preservar os releases antigos, os 21 WIP, workers, diretórios temporários, estado de perfil, memória e credenciais. Não editar um release existente.
5. Validar serviço Laya separado: fonte/model/dependências pinados, loopback, CPU/RAM, warm readiness e timeout total. Depois trocar apenas o ramo NFOS do launcher e sua configuração revisada. Para workers ainda no cgroup, revisar a medida temporária `KillMode=process` exclusivamente na unit NFOS; SIGUSR1 não protege sozinho subprocessos Kanban de `KillMode=control-group`.
6. Executar uma única drenagem nativa do gateway afetado, observar novo PID/SHA/readiness e preservação PID/start-time/claim/run/temp. Não repetir restart nem escalar silenciosamente para kill. Restaurar a configuração temporária de KillMode após a troca comprovada.
7. Alinhar `suporte.service`, que já resolve dinamicamente o runtime NFOS por `/usr/local/bin/nfos-support`, preservando `/opt/suporte.release-7f64d9e24e4f`. Saúde: `http://172.18.0.1:8790/health`. Sineta e closeouts têm pins próprios e não entram automaticamente. Vigília somente com o companion explicitamente incluído e revisado, preservando serviços alheios.
8. Fazer readbacks no runtime instalado, entradas nativas isoladas, status/config, engines/fallback, workers e estado. Informar ao master o resultado final. Se houver rollback, restaurar somente o entrypoint e as quatro chaves de configuração imediatamente anteriores, preservando alterações concorrentes fora do upgrade, e alinhar consumidores afetados. Nunca repor um SQLite antigo sobre cards, runs ou aprendizados que avançaram.

Uma futura instrução do proprietário para ativar o pacote preparado deve aplicar o overlay `active` já versionado no manifesto e tornar `#laya` utilizável nas três classes, sem uma segunda configuração manual nem nova aprovação de negócio. A coordenação técnica da release continua na sessão master existente.

O controller não versionado de ativação preparado no escopo anterior permanece apenas referência histórica. Não executá-lo neste HOLD.

## Evidências desta preparação

- `reviewer-preparation-final-result.json`: 22 testes focais passaram e Ruff passou; exit 0, `VisibleWindows=0`. Inclui cron independente, dedupe, redaction, splits, drift e ensaio nativo isolado.
- `learning-native-entrypoint-result.json`: o batch nativo gerou uma oportunidade real em SQLite e executou uma sonda HTTP de fixture explicitamente sintética. O Revisor coletou essa trajetória e o export ligou `PASS` à SPEC/run/artefato correto. O replay não fez inferência.
- `/srv/hermes/releases/system-one-preparation-check-b087eae99354.json`: baseline produtivo conferido sem mutações, incluindo configuração `25377ffbf979971aac339d1f85c3ac9361cc8e218e09bec05b06aac7d5984cde`, com o projeto novo preservado.
- `/srv/hermes/releases/system-one-preparation-rehearsal-b087eae99354.json`: produtor do snapshot Git `9e2d5b1adaf17668e82a9faf1c46729597101491`, leitor antigo `/usr/local/lib/hermes-agent.git-6381c19b79`, dois cards/runs preservados, eventos aditivos lidos pelo Revisor antigo em SQLite RO e rollback cirúrgico comprovado. Este é o SHA ensaiado para compatibilidade, não uma afirmação sobre o SHA final do pacote.

O manifesto final deve incluir esses recibos e fixar o SHA final preparado. Nenhum desses resultados representa merge ou ativação.

## Configuração pronta e cobertura executável

Aplicar futuramente somente o [overlay versionado](system-one-overlay.yaml), preservando o restante do perfil. `enabled: false` mantém tarefas sem hashtag desligadas; a seleção autorizada `#laya`/`#jev` habilita apenas seu card pela allowlist. `#deepseek` continua independente. Duas hashtags de engine geram conflito explícito. Retry herda identidade/revisão e não recebe política nova silenciosamente.

O arquivo protegido canônico para a futura chave é `/srv/hermes/profiles/hermes-project-factory/.env`, variável `OPENROUTER_API_KEY`, escrita pelo mecanismo nativo `hermes_cli.config.save_env_value_secure` no contexto desse perfil, permissão 0600. Exemplo de formato: `OPENROUTER_API_KEY=<chave fornecida pelo proprietário>`. Nunca incluir a chave no YAML, Git, prints ou corpo de PR. O runtime resolve o arquivo pelo mecanismo nativo `get_env_value`. Configurado, credencial presente, autenticado e inferência testada são estados diferentes. Nenhuma chave nem chamada paga foi usada nesta preparação. OpenRouter é o caminho preparado; Vercel e TypeSafe continuam compatíveis. A ativação Laya não depende dessa chave.

| Classe/gatilho | Execução nativa e limite | Evidência |
|---|---|---|
| Orçamento | Decisão tipada no consumidor existente; limites determinísticos continuam soberanos | Suíte focal das quatro classes e testes online |
| Impedimento recuperável | Seleção entre sondas SQL/HTTP/header já permitidas, filtradas antes do modelo | Batch nativo, fixture HTTP real, artefato ligado à SPEC/run |
| Início de etapa/próxima evidência | Finalização do batch do worker detecta mudança material e mede sondas registradas | Testes de batch segmentado, PID/run/claim e não repetição |
| Retry estagnado | Segundo erro tipado repetido; nenhuma repetição autônoma de comando | Testes de limites, interrupção e oportunidade material |
| SPEC primária | Jev preparado: accept evita revisão redundante, changes retorna ao mesmo card, exceção usa Principal. Laya active excluído; shadow só observa | Testes accept/changes/fallback e exclusão persistente Laya |
| Aprendizagem | Revisor retrospectivo existente, export/replay offline, resultados verificáveis separados de opinião | Dataset/proveniência/redação/dedupe/splits, NOT_PROVEN |
| Vigília | GET/POST reais pela ponte nativa Suporte/CLI em HOME isolado | Quatro POSTs, reload, schedule preservado e screenshots headless |
| Rollback | Pointer e quatro chaves; SQLite novo permanece | Ensaio VPS com leitor antigo 6381 e novos cards/runs |

Essa cobertura não é controle universal de ferramentas: não há chamada antes de cada leitura, comandos inventados, loop paralelo nem autoridade sobre aceite final. Permissões são filtradas antes da escolha; falha, inconsistência, prazo e insuficiência voltam ao fluxo nativo. Shadow não executa nem exige segundo julgamento. O contador de chamadas ao Principal poupadas só aumenta onde existe substituição comprovável; sondas contínuas novas não fabricam economia.

## Capacidade e limitações medidas

O serviço persistente `nfos-laya.service` permaneceu enabled/active, PID 1508465, Transformers 5.0.0, Torch 2.6.0+cpu, fonte `42626c348753fbb17572a813127df2278a1ec527`, checkpoint multilíngue `052592a15d198d9ad47da779604259b10b47b7aa`. O serviço atual não foi substituído. A versão candidata com backpressure foi testada em serviço temporário isolado, encerrado ao final, usando o mesmo ambiente/modelo já instalado.

Na pequena amostra: endpoint persistente 0,874 s; candidato 0,710 s sequencial; par concorrente aceitou uma chamada em 0,564 s e recusou a outra com HTTP 429 em 0,008 s. Pico do cgroup candidato 2,34 GiB, sob limite 6 GiB e duas CPUs. Isso suporta iniciar o experimento com uma inferência por vez; não demonstra dez workers simultâneos nem throughput sustentado. A primeira tentativa no endpoint persistente excedeu 20 s antes de qualquer serviço de teste; diagnóstico posterior mostrou o mesmo PID saudável. O overlay preparado permite até 90 s, ainda limitado pelo orçamento restante do run. Não houve restart para obter o resultado.

A VPS tem capacidade observada para esse experimento limitado, mas CPU local consome recursos compartilhados. Sem custo de API por chamada Laya. O caso semântico negativo anterior permaneceu: opção contraditória escolhida com confiança 0,9227. Não há ganho de qualidade demonstrado nem chamadas reais poupadas ao Principal. Limites reais de estado/head 1024/256; 8192 do encoder não é contexto validado de decisão. Não truncar SPEC para aceitar. Laya SPEC permanece Principal até evidência negativa nativa suficiente.

Testes finais de preparação: 37 passaram, Ruff passou, além dos 109 testes focais anteriores e da ponte UI real (1 passou). Prints em `deliveries/system-one-upgrade-20260921/ui/` são capturas reais do candidato, não do runtime produtivo nem do transporte Telegram. A inferência Jev continua não testada por ausência esperada de chave.
