# A Base: o ciclo padrão-ouro da era pura

> Resposta exata da análise de 29/08/2026 (dissecação telegram+git da era
> 19-24/08, pedida e aceita pelo operador), preservada verbatim como
> companheira da spec (TARGET_ARCHITECTURE.md). As features novas
> (workers, kanban, Vigília, AOF) crescem SOBRE esta base; a régua de
> qualquer feature é: encurta ou alonga a distância entre "arruma isso"
> e "aceito em produção"?

---

Dissecação completa: as duas pontas fecharam o quadro. A triagem do
Concursa (20/08 01:46) mostra o elo que faltava: **"4 ocorrências,
agrupadas em 2 bugs sistêmicos de severidade alta"**, cada ocorrência
com concretude absoluta ("questão sobre enviar a irmã ao Conservatório:
alternativa A aparece como correta mas a explicação diz B"), e ~7h
depois o fix estava em produção. E o DOV mostra a validação em camadas:
"Verificação ad-hoc focada: 20/20" = regressão focal do vídeo 18/18 +
verificador adversarial do manifest 10/10 + 161/161 blobs +
HEAD==origin/main==release + health. Quando cobrado, **revalidava ao
vivo** ("Revalidei agora:").

## A BASE, extraída: o ciclo padrão-ouro em 8 fases

**1. Recepção multimodal total** — vídeo/áudio/print consumidos POR
COMPLETO antes de concluir; card único registrado no recebimento
(registro, não motor); mídia bloqueada = melhorar a plataforma no ato e
seguir.

**2. Triagem → bugs sistêmicos** — ocorrências listadas com a
concretude do conteúdo real (a questão exata, o botão exato), agrupadas
em bugs sistêmicos com severidade; sintoma na linguagem do usuário,
hipótese de causa fora do escopo.

**3. Plano no turno** — todo list sequencial curta
(recover→analyze→fix c/ regressão→verify) que atravessa compaction e
queda.

**4. Fix atômico** — branch `fix/<tema>-<data>`, um bug sistêmico = um
fix.

**5. Prova em camadas antes de subir** — regressão focal (18/18, 11/11)
+ suite completa com link (211/211) + verificação adversarial do
artefato (manifest 10/10, 161/161) + ambiente de prova (staging no
Concursa, restauração isolada de backup no DOV).

**6. Promoção imediata POR FIX** — staging→main pelo próprio agente ou
publicação com hashes exatos + versão anterior parada; funil sempre
vazio (38 min a 7h da triagem à produção; lote nunca existiu).

**7. Validação no alvo + readback** — health, HEAD==release, hashes,
logs limpos, readback dos registros tocados; e re-validação AO VIVO
quando questionado.

**8. Closeout único rico** — PR/SHA/CI-link/produção/dados/card/
readiness/limitação, com as correções descritas na linguagem de quem
pediu.

Transversais: readiness honesto como cultura; o operador cobra no chat
e recebe estado exato imediato; quedas atravessadas pela simplicidade
do plano.

---

## Apêndice: contexto da análise (mesma conversa, minutos antes)

O padrão-ouro da base (era pura, 13-24/08, sem workers/Vigília/
kanban-motor), reconstruído das mensagens reais do topic DOVCRM — ciclo
t_ee72e6ee (18/08 23:27 → 19/08 05:36, do vídeo ao "aceito em
produção") e Wave 3 (21/08):

1. **Um contexto, de ponta a ponta.** Pedido (texto/vídeo/áudio),
   análise, código, teste, deploy e cobrança viviam na MESMA conversa,
   no MESMO agente. Zero handoff interno, zero perda de brief.
2. **Card único como REGISTRO, nunca como motor.**
3. **O plano era a todo list nativa do turno** — sequencial, visível,
   sobrevivia a compaction e shutdown (que JÁ existiam na época).
4. **Evidência antes de subir, sempre com link** — e readiness honesto
   já era cultura ("parcialmente implementada... ainda NÃO publicada").
5. **Entrega = UMA mensagem-closeout rica** — PR, SHA, CI, produção,
   dados, card, readiness, limitação.
6. **Bloqueio virava melhoria de plataforma no ato** — Bot API 20MB →
   server local 2GB, e o fluxo seguiu.
7. **O operador era o watchdog e o dispatcher** — "ajustou?", e o
   agente respondia estado EXATO, na hora.
8. **Trabalho em Waves** — lotes nomeados de correções, entrega única
   evidenciada.

**Princípio 0 (visto no git do Concursa, era-agente desde 19-20/08):
PROMOÇÃO POR FIX, FUNIL SEMPRE VAZIO.** Três ciclos completos só em
20/08; commit 21:14 → produção 21:52 = 38 minutos. Card "[RELEASE]
integrar lote" é antipadrão: lote é inimigo da latência.

**Onde a era das features (25-29/08) desviou:** o motor migrou do TURNO
pro BOARD sem preservar (1) contexto de ponta a ponta (worker sem brief
vivo = G3/T3 da spec) e (2) distância mínima intenção→execução
(card→claim→spawn→judge→gate; cada trava cobra a velocidade que é o
produto). As features em si resolvem limites reais da base
(paralelismo, visibilidade, sobrevivência, enforcement).

**Prova de continuidade:** t_bdb7f409 (Concursa 29/08, 70min
fix→produção com staging + regressão focal + readback) é o MESMO DNA de
19-20/08 executado pela era nova quando nada atrapalha.
