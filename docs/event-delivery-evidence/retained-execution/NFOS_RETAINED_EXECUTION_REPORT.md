# NFOS: suspensão do Principal e execução dos cards retidos

Atualização em 07/09/2026. Candidato `f1286be030c8ee73023c1930da9d4798fd573ac1`, [PR #91](https://github.com/maikolb/hermes-agent/pull/91). Publicação em produção ainda pendente.

## Evidência do incidente

No DOVTest, as mensagens originais `req_532fd84409b659ee03f885f8` e `req_729b0a2d80e1bd74a18925da` persistiram na fila. A primeira foi aceita no checkpoint do Principal. As decisões `dec_37c6ae38013b4a0bb003` e `dec_7b4530820d41454fb400` registraram `human` com a ordem explícita de parar. Mesmo assim, `t_6420099b`, execução 34, continuou e pediu revisão posterior. O Principal registrou que não conseguira confirmar a parada total após perder a renovação da sessão. Fonte: `outputs/nfos-12h/dovtest-stop-read-01.json` e `dovtest-queue-02.json`.

O runtime esperava o processo morrer antes de aplicar o bloqueio, enquanto a limpeza de processos exigia que o run já estivesse encerrado. A espera em uma pergunta posterior também não mostrava a suspensão anterior. O problema foi reproduzido com processos e filhos reais em ambiente isolado.

## Escopo e aceite

| Requisito | Resultado atual | Evidência |
|---|---|---|
| Aplicar a suspensão registrada pelo Principal, mesmo se o worker não chamar block | PASS local e Linux | `test_nfos_principal_stop.py`; `principal-stop-green-02.log`; `suspension-tests.log` |
| Impedir avanço, chamadas novas e nova atribuição enquanto a suspensão estiver vigente | PASS local e Linux | Mesmo teste; `test_nfos_tool.py`; 48 PASS e 1 SKIP local, 49 PASS Linux após repetição do arquivo interrompido |
| Encerrar processos e filhos identificados, preservar arquivos, permitir retomada válida no mesmo card | PASS local e Linux | `test_nfos_worker_shutdown.py`, `test_nfos_principal_reconsideration.py`; 18 PASS na repetição, exit 0 |
| Separar checkouts dos cards retidos e preservar mudanças tracked, index, arquivos binários, exclusões e checkout original | PASS local e 6 testes específicos PASS Linux; regressão Linux em execução | `test_nfos_retained_workspaces.py`; `retained-workspace-regression.log`; `retained-validation-progress-02.json` |
| Recuperar interrupção de preparação sem dois executores nem sobrescrever worktree alheia | PASS local e Linux | `test_nfos_worktree_recovery.py`, 17 PASS na repetição; `test_nfos_recovery_ownership.py`, 5 PASS na rodada anterior |
| Falha de saída para Telegram não impedir a entrega interna da decisão ao Principal | PASS local e Linux | `test_nfos_principal_queue.py`; ACKs independentes, 3 PASS Linux |
| Preservar os demais contratos de entrega, particionamento, recuperação e notificação | PASS local | `principal-stop-contracts.log`, 63 PASS |
| Conferir Git, HML e produção antes de refazer e manter o Principal como gerente do board | Instrução explícita no runtime; comportamento real observado em cards anteriores | `hermes_cli/nfos_runtime.py`; não é prova de aderência universal |
| Aplicar a correção em produção | NOT_RUN | Depende da publicação; não marcar como concluído por teste local |
| Confirmar a parada solicitada do DOVTest | PASS no runtime anterior, antes da nova publicação | `dovtest-shutdown-01.json`: zero executores identificados vivos, zero cards running/ready; execução 34 encerrada e filhos terminados |

Os logs de RED foram preservados. A primeira rodada da suspensão contém duas falhas comportamentais relevantes e uma falha de preparação de teste, corrigida antes da rodada GREEN. Os testes antigos de reconsideração agora montam explicitamente o histórico inconsistente que a nova proteção impede criar pela API de claim. Nenhum teste apresenta esse histórico como uma atribuição atualmente permitida.

Print real dos logs locais: `outputs/nfos-12h/retained-execution-evidence/contracts.png`. A captura identifica que não é comprovação de produção. Prova de execução headless: `retained-execution-evidence/zero-ui.json`, `VisibleWindows=0`.

O Principal efetivou a parada do DOVTest pelo fluxo vigente. Às 23:34:59 UTC, a execução 34 estava encerrada, com limpeza dos processos confirmada às 23:14:55 UTC. As duas mensagens originais tinham aceite persistido. A spec e os relatórios permaneceram no card. Prints reais: `retained-execution-evidence/dovtest-desktop.png` e `dovtest-mobile.png`. Esta parada não foi causada pelo candidato ainda não publicado. O projeto e seu histórico não foram fisicamente excluídos.

## Limitações verificadas

- A suíte ampla antiga de notificações tem 17 falhas, com os mesmos IDs já presentes no log do candidato anterior `global-rollout-job-101870277181.log`. Essa suíte não está sendo apresentada como verde. As regressões específicas usam perfis e recibos válidos.
- O check macOS do PR #91 falhou em `test_play_audio_file_skips_sounddevice_on_macos`, fora dos arquivos alterados. O mesmo teste já falhou nos candidatos anteriores: `global-rollout-job-101856109111.log` e `global-rollout-job-101870277077.log`. Resultado atual preservado em `retained-job-101888181550.log`.
- Duas suítes Linux atingiram o limite de 300 segundos por arquivo, sem falha de asserção registrada antes do corte. A execução inicial permanece FAIL por timeout. Somente os arquivos interrompidos foram repetidos com 900 segundos no executor de testes: recuperação, 17 PASS em 476,2 s; reconsideração, 18 PASS em 331,6 s. Os prazos do produto não foram alterados.
- O código final tem 66 aprovações Linux nessas suítes: 49 de suspensão/retomada e 17 de recuperação. Há 34 aprovações adicionais da validação anterior de isolamento/notificação, cujo módulo de isolamento é idêntico ao final, mas que antecede as mudanças de suspensão em `kanban_db` e `nfos_runtime`. Os recibos preservam os hashes e não apresentam as rodadas como uma única execução.
- A VPS registrou 92% de CPU steal nas duas amostras recentes. Fonte: `retained-release-preflight-01.json`. Não foi medida solução dessa limitação de capacidade.
- Às 23:19 UTC, DOV e Concursa tinham dois workers cada, iniciados pelo runtime vigente. Isso demonstra paralelismo naquele instante; não comprova a conclusão de todas as tarefas. Fonte: `parallel-diagnostic-current-06.json`.
- Não houve alteração manual de decisão, card de produto, senha, deploy de produto ou exclusão do DOVTest por esta correção. A aposentadoria deve preservar a decisão do Principal e o histórico.
