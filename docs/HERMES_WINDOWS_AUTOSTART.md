# Hermes Windows Autostart

## Objetivo

Manter os três gateways Hermes já definidos como permanentes nesta máquina disponíveis após queda de energia, reinício do Windows ou crash do processo, sem abrir console ou competir com a interface do usuário.

Esta é uma solução temporária para o host Windows. Na VPS, a autoridade deve migrar para serviços Linux/container por perfil.

## Perfis gerenciados

| Perfil | Tarefa | Início após login |
|---|---|---:|
| `default` | `Hermes_Gateway` | 30 s |
| `hermes-ceogame` | `Hermes_Gateway_hermes-ceogame` | 45 s |
| `hermes-project-factory` | `Hermes_Gateway_hermes-project-factory` | 60 s |

`bench-supervisor`, `hermes-darkfactory` e `hermes-exocortex` não fazem parte desta rotina e permanecem dormentes.

## Contrato operacional

- Cada perfil possui uma tarefa independente e uma única ação direta `pythonw.exe -> launcher .pyw`.
- A tarefa usa `MultipleInstances=IgnoreNew`, `StartWhenAvailable=true`, `ExecutionTimeLimit=PT0S` e reinício a cada minuto, até 999 tentativas, quando o processo encerra com falha.
- O gateway e o adaptador Telegram continuam responsáveis pela reconciliação de PID/birth marker, lock profile-scoped e recuperação de polling/rede.
- Os delays escalonados evitam que três bots disputem CPU e rede imediatamente no login.
- A rotina começa na primeira sessão do usuário após ligar a máquina. Ela não roda como `SYSTEM` antes do login e não armazena senha.
- Nenhuma mensagem Telegram, comando slash ou chamada de teste é emitida pela instalação ou pelo health check.

## Auditoria e reconciliação

Rotina idempotente:

`C:\Users\maiko\AppData\Local\hermes\scripts\ensure-hermes-gateway-autostart.ps1`

- Sem parâmetros: auditoria read-only; exit `0` somente quando os três perfis estão prontos e não há atalho duplicado ativo.
- `-Apply`: registra ou corrige tarefas ausentes/não conformes e só então move atalhos legados para backup recuperável.
- `-StartMissing`: opcional; inicia somente perfil sem PID vivo. Não é necessário durante operação saudável.
- `-EvidencePath`: aceita somente arquivo dentro de `C:\Users\maiko\AppData\Local\hermes\evidence`.

Evidência atual:

- Baseline anterior: `C:\Users\maiko\AppData\Local\hermes\evidence\gateway-autostart-20260817.json` (`1/3` antes, `3/3` após aplicação).
- Auditoria final: `C:\Users\maiko\AppData\Local\hermes\evidence\gateway-autostart-final-20260817.json` (`3/3`).
- Health global final: `C:\Users\maiko\.codex\reports\hermes-multiteam-autostart-owned-20260817.json` (`validated-local`, três gateways vivos, zero failures).

## Evidência pós-queda de 17/08/2026

- O Windows iniciou às 12:06; a sessão do usuário disparou o perfil principal às 12:15 e o Telegram conectou em polling.
- CEOGame e Project Factory também ficaram vivos com PID, birth marker e `HERMES_HOME` coerentes.
- Ambos registraram erros transitórios de rede após a queda e o próprio adaptador confirmou `polling restarted after network error`; Project Factory também recuperou um conflito transitório de polling.
- O monitor persistente examinou 267 eventos de janela no intervalo que cobriu os três starts e encontrou zero janela visível pertencente a `pythonw.exe`, zero ancestral `pythonw.exe` e zero console visível descendente.
- CEOGame e Project Factory foram transferidos dos processos iniciados pelos atalhos para processos pertencentes às tarefas agendadas. As duas tarefas ficaram `Running`, com PID novo, birth marker e home corretos; CEOGame conectou ao Telegram e Project Factory se recuperou automaticamente de um timeout inicial e reconectou.
- Na janela da transição task-owned, o listener processou 138 eventos de janela, com `droppedEvents=0`; nenhuma janela ou console foi atribuído aos dois novos processos ou descendentes.
- Após restaurar o pareamento do Titan, o perfil default foi reiniciado apenas pela tarefa `Hermes_Gateway`: o bridge Node gerenciado assumiu a porta, `/health` ficou `connected`, o PID file correspondeu ao listener e o monitor encontrou `VisibleWindows=0` por 20 s.
- Uma mensagem nova percorreu inbound, smart routing (`Luna/Low`) e delivery ledger (`delivered`) em 11,4 s. A auditoria idempotente subsequente confirmou `3/3` tarefas conformes e `Running`.

## Backup e rollback

Backup da aplicação inicial:

`C:\Users\maiko\AppData\Local\hermes\backups\gateway-autostart-20260817-125324`

Ele preserva o XML anterior de `Hermes_Gateway` e cópias dos dois atalhos legados. Um rollback deve ser feito somente de forma profile-scoped: exportar primeiro o estado atual, remover apenas a tarefa alvo e restaurar apenas o atalho equivalente. Nunca apagar locks ou matar `pythonw.exe` por nome.

Snapshot idempotente das três definições finais:

`C:\Users\maiko\AppData\Local\hermes\backups\gateway-autostart-20260817-130133`

## Limitações conhecidas

- A prova atual cobre task definitions, a ação real disparada pelo Task Scheduler, processos task-owned, recuperação de polling e zero-UI. O evento de logon em si só ganhará prova pós-instalação no próximo login/reboot natural; a ação que ele dispara já foi validada no alvo.
- A tarefa histórica `Hermes_Gateway` mantém `RunLevel=Highest`. A tentativa de reduzi-la sem elevação foi recusada pelo Windows e nenhuma UAC foi aberta. Isso não bloqueia disponibilidade, mas a redução para `Limited` fica para uma janela administrativa controlada.
- `hermes-exocortex` ainda possui metadata histórica stale; permanece fora do conjunto always-on e não interfere nestes três gateways.

## Migração futura para VPS

Na VPS, substituir as três tarefas por três unidades `systemd` ou containers independentes, mantendo homes, credenciais, boards e bancos separados. Exigir `Restart=always`, backoff, healthcheck, logs persistentes e uma autoridade única por perfil antes de desligar a rotina Windows.
