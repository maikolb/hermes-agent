# NFOS: operação pelo tópico do projeto

O Principal recebe e coordena. Cada worker executa uma tarefa. O card reúne pedido,
anexos, spec, execuções e entrega. Este documento descreve o fluxo NFOS opt-in;
não reativa contratos, hooks ou aprovações do AOF.

## Uso diário

1. Envie o pedido ou a lista de tarefas no tópico Telegram do projeto, com seus
   anexos. O recibo identifica o pedido persistido; não significa trabalho iniciado.
2. O worker cria o card e analisa o pedido. Havendo tarefas independentes na mesma
   mensagem, propõe a divisão ao Principal. As demais entram no mesmo despacho.
3. Abra o card no Vigília. **Spec** mostra o escopo versionado, **Live Log** mostra
   atividade e resultados disponíveis, **Evidências/Entrega** mostra a comprovação.
4. Se houver uma pergunta para você, responda no tópico fazendo referência ao card
   ou respondendo à mensagem. O Principal restaura o mesmo card e seu histórico.
   Não é necessário editar filas, banco ou estado técnico.

O worker consulta Claude TL para a spec e usa Codex quando Claude está indisponível.
O registro identifica a execução usada e o motivo do fallback. Codex implementa,
testa e corrige em ciclos explícitos; não se declara execução de uma integração
Ralph que não esteja disponível no executor.

Um relatório sem alteração de código pode terminar com revisão e evidências,
sem PR ou deploy. O tipo inicial do projeto é provisório: a primeira spec pode
classificar a tarefa como relatório/operação antes da implementação.

## O que cada estado significa

| Informação | Interpretação |
|---|---|
| Pedido recebido | Original registrado; pode estar aguardando capacidade. |
| Card pronto | Pode executar quando houver vaga e dependências satisfeitas. |
| Em andamento | Uma execução assumiu o card. Veja também sua última atividade. |
| Aguardando Principal | Impedimento/revisão persistido; o worker pode continuar após a resposta. |
| Bloqueado por humano | Pergunta concreta registrada; estado salvo e capacidade liberada após encerramento confirmado. |
| Concluído | Spec, revisão e entrega aplicável comprovadas, com relatório salvo. |

Conexão, heartbeat e progresso são informações diferentes. Um heartbeat recente
prova sinal de vida; uma saída de comando ou etapa concluída demonstra atividade.
Saída parcial e timeout permanecem visíveis, sem converter timeout em sucesso.

## Configuração por projeto

O exemplo abaixo corresponde ao piloto DOV e usa o perfil completo existente:

```yaml
kanban:
  delivery:
    projects:
      dovcrm:
        enabled: true
        profile: hermes-project-factory
        workers: 2
        repo_path: /srv/projects/next-crm
        delivery_type: code
        max_runtime_seconds: 7200
```

O tópico deve resolver para esse board pela configuração existente do gateway.
O limite efetivo considera também a capacidade do host e os demais limites
existentes. O Principal tem seu próprio atendimento e não ocupa uma dessas vagas
de execução. Alterar este exemplo não configura automaticamente outros projetos.

Os projetos opt-in recebem instruções gerenciais no Principal e usam o intake
NFOS antes do caminho de implementação direta. Respostas, perguntas de status e
mensagens de coordenação continuam sendo tratadas pelo Principal.

O reconhecimento automático de pedidos é um caminho rápido. Outras formulações,
inclusive pedidos enviados como resposta, chegam ao Principal com a identidade
real da mensagem, o board e os anexos originais preservados. Se houver trabalho
independente, ele usa o mesmo comando `receive` e deixa o worker criar o card.
O original fica em `nfos_requests` com estado `coordinating`, sem criar card ou
ocupar vaga de worker. O mecanismo de wake existente entrega a mensagem ao
Principal quando sua sessão está disponível. A confirmação exige checkpoint
com texto, origem, anexos e comando de despacho. Antes dessa confirmação, uma
queda deixa a entrega pendente no SQLite; depois dela, a recuperação usa o
checkpoint da sessão. O mesmo pedido passa a `pending` quando o Principal o
identifica como trabalho. Uma pergunta de status ou resposta humana continua
sendo coordenação e não cria tarefa apenas por conter esse contexto.

Se o Principal descobrir uma solução para uma pergunta que ele próprio havia
encaminhado ao humano, pode reconsiderar essa decisão com `reconsider`, razão e
resposta concretas. A decisão anterior permanece no histórico. A nova revisão
usa a spec e o candidato atuais, e a retomada aguarda o encerramento da execução
anterior. Isso não registra uma resposta humana fictícia nem conclui critérios
que continuam sem comprovação.

## Fonte de cada informação

| Informação | Autoridade |
|---|---|
| Pedido original, reserva e recibo | `nfos_requests` no SQLite do board |
| Card, responsável, execução e dependências | `tasks`, `task_runs`, `task_links` |
| Spec e relatório versionados | `nfos_artifacts` |
| Etapa e próxima ação | `nfos_workflows` |
| Fila de impedimentos/revisões | `nfos_decisions` e eventos persistidos |
| Efeito externo solicitado e leitura do destino | `nfos_effects` |
| Uma publicação por projeto | `nfos_project_delivery` |
| Conversa Hermes | SessionDB do perfil, vinculada à execução |
| Comando nativo e saída durante execução | `nfos_tool_calls` e chunks do mesmo board |
| Originais, workspace e evidências grandes | Arquivos referenciados pelos registros acima |

O Vigília lê essas fontes. Não possui uma fila de execução própria. Os arquivos
originais são preservados antes do recibo. O recebimento é idempotente pela
identidade da mensagem e da parte do pedido. Uma resposta Telegram perdida pode
produzir recibo repetido, mas não deve produzir trabalho duplicado.

## Recuperação e publicação

A reconciliação roda no dispatcher existente, incluindo startup. Ela consulta
execuções, chamadas e identidades de processos antes de permitir um substituto.
Processos e filhos conhecidos devem estar encerrados. Uma sessão terminal gera
novo turno executável com histórico preservado, sem reproduzir o encerramento.

PR, merge e deploy registram a intenção antes da chamada externa. Se a resposta
se perder, a próxima tentativa consulta o destino primeiro. A revisão vincula a
spec ao candidato homologado e à sua árvore. O SHA integrado e o artefato de
produção são registrados separadamente; HTTP200 sozinho não comprova a entrega.

Nos cards NFOS, o guard legado de PR não consulta nem integra uma PR citada em
comentário. A publicação pertence ao workflow e ao seu registro de efeitos.

Uma correção na instrução do mesmo card exige atualizar e persistir a spec antes
de continuar a implementação. A revisão anterior não aprova o novo escopo: o
Principal revisa o candidato ou relatório atual antes da publicação ou conclusão.
Spec, revisões anteriores e trabalho salvo permanecem no histórico. A consulta de
um efeito externo já iniciado continua permitida para descobrir seu resultado.

A autorização de negócio dentro da spec permite revisão, merge e deploy sem nova
confirmação humana. Uma dependência real de acesso ou uma escolha de produto ainda
sem resposta gera a pergunta específica, sem criar login ou aprovação genérica.

Ao promover uma correção do runtime, preserve a release existente, prepare a nova
fonte identificada pelo SHA, confira backup/rollback e reinicie apenas os gateways
afetados após conferir suas execuções. Não reinicie o Titan local por dentro dele.

## Limites de comprovação

O comportamento descrito precisa ser conferido no candidato efetivamente ativado.
O relatório `NFOS_VALIDATION_REPORT.md` da execução registra S01–S12 com PASS, FAIL
ou NOT_RUN, fontes, versões e imagens reais. Testes de processo, fixtures de mídia
e uma tarefa publicada não substituem a comprovação de todos os itens, nem provam
por si só resistência a perda física de energia no armazenamento.
