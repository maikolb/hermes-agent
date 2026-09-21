# Engines por tarefa no NFOS

O pedido original pode selecionar dois eixos independentes: `#deepseek` escolhe o worker existente; `#jev` ou `#laya` escolhe o engine das decisões limitadas. Sem hashtags, os defaults permanecem. `#jev #laya` é conflito explícito e não cria pedido parcial. Tags desconhecidas não alteram política. Somente o intake nativo autorizado interpreta as tags; anexos, resultados de tools e progresso do worker não selecionam engines.

Exemplos de sintaxe: `Verifique a contagem no projeto #deepseek #laya` e `Verifique a contagem no projeto #deepseek #jev`. Não execute esses exemplos como trabalho real. A seleção guarda configuração e revisão no pedido/workflow e acompanha retry, resume e continuação. Configuração divergente invalida a seleção, em vez de trocar o engine silenciosamente.

## Configuração do perfil

Este é o diff a revisar antes da ativação, não uma afirmação de configuração instalada:

```yaml
kanban:
  delivery:
    decision_engines:
      allowed: [laya, jev]
    laya:
      enabled: false
      endpoint: http://127.0.0.1:18991/systemone
      model: convaiinnovations/laya-multilingual
      model_revision: 052592a15d198d9ad47da779604259b10b47b7aa
      source_revision: 42626c348753fbb17572a813127df2278a1ec527
      timeout_seconds: 90
      min_confidence: 0.8
      uses: [budget, spec, impediment, evidence]
    jev:
      enabled: false
      provider: typesafe
      model: jev-1.13.0
      api_key_env: TYPESAFE_API_KEY
      timeout_seconds: 2
      min_confidence: 0.8
      uses: [budget, spec, impediment, evidence]
```

`allowed` permite opt-in por tarefa, sem ligar engines nas tarefas sem tags. Use somente o provider Jev já contratado/configurado. Chaves não vão no YAML. Ausência de chave/cota preserva o fallback Principal e deve aparecer como indisponibilidade. Não existe fallback pago entre providers.

## Execução e limites

Os quatro usos reutilizam as entradas nativas: orçamento em save_spec, revisão primária da SPEC, escolha de sondagem de impedimento e escolha de medição de evidência. Um aceite primário válido dispensa o Principal para aquela SPEC; changes retorna ao mesmo worker/card. Inconsistência, contexto insuficiente ou falha seguem o fluxo nativo do Principal. Autoridade de aceite final, limites de execução e permissões permanecem nativos. Eventos registram engine selecionado/real, modelo, revisão, latência, fallback, ação executada e chamadas Principal efetivamente evitadas. Escolher orçamento/evidência não prova economia de uma chamada Principal que antes não existia.

O serviço `scripts/nfos_laya_server.py` usa o SDK oficial em revisão fixa e safetensors locais, sem código remoto confiado ou checkpoint inglês. O checkpoint tem max_len=1024 e head_max_len=256; o encoder declara 8192 posições, mas esse limite maior não é promovido a capacidade validada. O serviço verifica tokens reais de estado, instruções e opções antes da chamada. Qualquer corte que o SDK faria retorna HTTP422 context_too_large. Não há truncamento silencioso com aceite.

CPU: duas threads, uma requisição e uma questão por vez, dois núcleos permitidos e máximo 6 GiB via systemd. Endpoint exclusivamente 127.0.0.1:18991. `/health` só fica disponível depois de carga e warm-up. Confiança do modelo não equivale a calibração NFOS; os resultados negativos reais da entrega determinam a prontidão de cada uso.

## Instalação isolada e promoção

Venv, cache/modelo e fonte oficial ficam em `/opt/nfos-laya`, separados das releases Hermes. O lock da entrega fixa dependências resolvidas; PyTorch vem do índice CPU oficial. O adaptador é extraído de `git archive` para `releases/<SHA>`, nunca corrigido dentro de release ativa. `scripts/nfos-laya.service` é o template de unidade persistente, com SHA substituído pelo candidato revisado.

A instalação experimental não ativa hashtags no gateway. A ativação NFOS exige confirmação da thread mestre sobre SHA integrado, evidências, limitações, diff de configuração e sequência. Preserve o entrypoint por perfil, workers, temporários e SQLite; reinicie somente consumidores realmente afetados. Não use scripts antigos de promoção removidos pelo proprietário.

Rollback: remover engines do allowlist desabilita novas decisões por tag; configuração divergente invalida decisões anteriores. Retorne o entrypoint do perfil ao SHA registrado no recibo, apenas se houver promoção. Não restaure banco antigo sobre trabalho novo. O experimento Laya pode ser parado pelo nome exclusivo de sua unidade, sem tocar outros serviços. A instalação isolada não requer rollback de produção.

Consulte `deliveries/laya-tags-20260921/` para evidências reais, limitações, refs e checkpoint de continuação. Testes HTTP locais são identificados separadamente da inferência real na VPS e de entrega Telegram.
