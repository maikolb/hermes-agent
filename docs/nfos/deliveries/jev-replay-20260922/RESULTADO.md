# Replay pré-registrado do Jev contra vereditos reais do Principal

Protocolo e critérios fixados em [`jev-plano-corrigido.md`](../../jev-plano-corrigido.md), commit `b4a913bc` publicado às 23:18 UTC de 22/09/2026, antes da primeira chamada paga. Inferência real: OpenRouter `jev-1.13`, endpoint SystemOne, prazo de 5 s, sem retry.

## Veredito

**Nenhuma camada passou. Pela regra pré-registrada, nem a camada 1 nem a 5 serão construídas ou ativadas.**

| Camada | Resultado decisivo | Critério que reprovou |
|---|---|---|
| 1, variante A (perguntas da #231) | 9 de 261 revisões (3,4%). Todas `accept`: 4 certas, 5 onde o Principal pediu `changes` | Falso aceite 5/5 = 100% (Wilson 95%: 57% a 100%); benefício 1,5% (mínimo 20%) |
| 1, variante B (perguntas decompostas, ponto 1 do Codex) | 26 de 261 (10%). Todas `accept`: 16 certas, 10 onde o Principal pediu `changes` | Falso aceite 10/10 = 100% (Wilson 95%: 72% a 100%); benefício 6,1% |
| 5 (pré-checagem da entrega) | 1 de 245 (0,4%): um `changes` correto | Benefício 0,4% (mínimo 20%); cobertura quase nula |

Manipulação (ponto 2 do Codex): não mensurável na camada 1, porque o Jev nunca deu `changes` confiante (0 casos elegíveis). Na camada 5, 1 caso: não virou.

## Por que tão pouco decisivo

- 118 das 261 revisões de spec têm anexo que o Jev não lê (texto apenas): fallback obrigatório.
- A maior parte do restante ficou com alguma resposta abaixo de 0,8 de confiança, o mesmo limiar do runtime. Confiança mínima por chamada, mediana: 0,35 (1A), 0,45 (1B), 0,37 (5).

## Análise exploratória, fora do pré-registro (não serve para promover)

Recontando as respostas guardadas sem limiar de confiança, sem nova chamada:

| Variante | Principal pediu `changes` | Principal aceitou |
|---|---|---|
| 1A | Jev aceitou 46, pediu `changes` 8 | Jev aceitou 56, pediu `changes` 9 |
| 1B | Jev aceitou 43, pediu `changes` 7 | Jev aceitou 48, pediu `changes` 11 |
| 5 | Jev `changes` 132, `ready` 15 | Jev `changes` 76, `ready` 19 |

Mesmo sem limiar, o Jev praticamente não separa os casos: na spec ele aceita cerca de 85% com ou sem defeito; na entrega ele devolve cerca de 85% com ou sem defeito. O problema não é o limiar; é o sinal.

## Efetividade contra o runtime de hoje (6381c19b79)

Contrafactual com os tempos medidos em produção na mesma janela ([`effectiveness-vs-runtime.json`](effectiveness-vs-runtime.json)). Regra do runtime: confiança mínima de 0,8.

| Medida no runtime atual | Valor |
|---|---|
| Espera pela revisão de spec do Principal | mediana 1,2 min, p90 2,1 min, 7,7 h somadas em 261 revisões |
| Ciclo de implementação (spec aceita até 1º relatório) | mediana 16,4 min (130 ciclos) |
| Espera pela revisão final | mediana 2,0 min |

| Camada 1 ligada | Variante A | Variante B |
|---|---|---|
| Revisões de spec substituídas | 9 (4 certas, 5 erradas) | 26 (16 certas, 10 erradas) |
| Chamadas do Principal a menos | 4 (1,5%) | 16 (6,1%) |
| Espera economizada | 0,19 h | 0,83 h |
| Retrabalho adicionado (limite inferior) | 1,53 h | 3,06 h |
| **Saldo de tempo** | **-1,34 h** | **-2,23 h** |
| Specs com defeito enviadas à implementação | 5 | 10 |

A revisão do Principal já é rápida (1,2 min). O gargalo é retrabalho: o Principal rejeita 49% das specs e 61% das entregas. Como o Jev não separa spec boa de ruim, ele só troca uma espera curta por um ciclo de implementação perdido. A camada 5 decide em menos de 1% dos casos: efeito nulo.

Modelo do contrafactual: aceite errado custa ao menos um ciclo de implementação desperdiçado e uma revisão final a mais (medianas medidas). Defeito que a revisão final deixasse passar não entra na conta, então o custo real é maior ou igual.

## Ressalvas

- A referência é o veredito do Principal, não uma verdade absoluta. Parte das rejeições depende de prints, logs e contexto que o Jev não recebe.
- 450 de 506 vereditos têm a instrução vigente exata; o recorte exato está em `metrics-final.json` e não muda a conclusão.
- Conteúdo dos cards não sai da VPS. Aqui só há contagens.

## Custo e rastreio

491 chamadas cobradas pelo provedor (`RESPONSE_BILLED` em todas, nenhuma reserva pendente): US$ 0,127056. Uso da chave dedicada desde a autorização original: de US$ 0,0066 para US$ 0,1316, dentro do teto cumulativo de US$ 2,88.

Arquivos na VPS (`/root/jev-replay-20260922`, sha256): `cases.jsonl` f2c400f6…, `results.jsonl` 4a8eff2d…, `ledger.json` aff6292c…, `metrics-final.json` a590f09b…, `exploratory-no-threshold.json` 42caed1d…, `effectiveness-vs-runtime.json` 11c6208e…. Harness em `harness/`.
