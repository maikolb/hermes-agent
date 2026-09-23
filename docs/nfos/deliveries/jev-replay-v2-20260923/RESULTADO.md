# Replay v2 do Jev: resultado

Protocolo pré-registrado em [`jev-replay-v2-protocolo.md`](../../jev-replay-v2-protocolo.md): commit `75ca9417` às 03:10 UTC de 23/09/2026 e emenda `2c3d0e84` às 03:12 UTC, as duas antes da primeira chamada paga. Inferência real: OpenRouter `jev-1.13`.

## Veredito

**Nenhuma frente passou. Pela regra pré-registrada, nada é construído nem ativado.**

| Frente | Resultado no teste | Critério que reprovou |
|---|---|---|
| 1. Impedimento | 4 respostas automáticas em 242 (1,7%), 1 errada (25%, Wilson 95%: 5% a 70%) | Erro, Wilson e cobertura |
| 2. Sugestão de skill (como rodou) | 42 sugestões em 76 cards, precisão de 43%; linha de base trivial: 58% | Precisão e margem sobre a linha de base |
| 2. Com o parser corrigido | 53 sugestões, precisão de 42% (Wilson 29% a 55%), cobertura de 70% | Mesmos critérios |
| 3. Repetir ou mudar após falha | Cancelada antes das chamadas | Rótulo inválido; só 19 casos limpos em 15 dias |

## Por que não passou

**Impedimento.** O Principal respondeu `continue` em 77% dos 484 casos. O Jev não separa `continue` de `changes` a partir da pergunta do worker: a AUC de `can_continue_alone` foi 0,518, e o tipo que ele atribui tem a mesma proporção de `continue` nos dois grupos.

Controle cego com a mesma entrada, numa amostra equilibrada de 40 `continue` e 40 `changes`:
- modelo de fronteira: AUC 0,65 (62,5% de acerto no corte de 0,5);
- Jev: AUC 0,49.

Duas conclusões:
- há algum sinal no texto, e o Jev, com essas perguntas, não o captura;
- mesmo o modelo de fronteira fica longe de decidir sozinho com erro de 5%, porque o Principal decide com contexto que não está na pergunta.

**Skill.** A precisão do Jev (42%) ficou abaixo de sugerir sempre `nfos-prime-agent` (58%). O rótulo é a escolha do worker, que carrega em média 3 skills por card, parte delas por hábito do processo. A frente mede se o Jev antecipa essa escolha, não se ela era a melhor.

## Achado corrigido no caminho

O parser da #231 recusava respostas válidas em escolhas com muitas opções. A TypeSafe arredonda cada probabilidade em 2 casas; com 69 opções, a soma fica perto de 0,98 e estourava a tolerância fixa de 0,01. Foram 36 das 152 chamadas de skill. A correção está no commit `d71240a5`, com um teste que falha no parser antigo e passa no novo.

## Números de operação

- Latência só da chamada ao Jev, da VPS ao OpenRouter: p50 de 392 ms e p95 de 544 ms.
- Custo cumulativo de todas as avaliações desde a autorização original: US$ 0,180147, em 1.323 chamadas, sem nenhuma reserva pendente.

## Rastreio

Arquivos em `/root/jev-replay-20260922` na VPS, identificados pelo sha256:

| Arquivo | sha256 (início) |
|---|---|
| `front1.jsonl` | bce4e219 |
| `front2.jsonl` | b7712543 |
| `results_v2.as-run.jsonl` | 4407f194 |
| `results_v2.jsonl` (parser corrigido) | 9b38d048 |
| `control_labels.json` | b7255001 |
| `control_llm_predictions.json` | 5719cb61 |

O conteúdo dos cards não sai da VPS; neste diretório há só contagens e o harness.
