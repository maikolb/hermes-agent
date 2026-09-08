# NFOS: preservar o aviso de reavaliação

O CI completo do PR92 revelou uma regressão no texto: o renderizador do encerramento sobrescrevia o aviso de reavaliação administrativa, mostrando "Worker bloqueado". A correção mantém o mesmo responsável pelo envio e o mesmo recibo durável, mas aplica o texto de encerramento apenas a eventos de encerramento efetivo. A reavaliação continua breve, sem expor instruções internas.

Não há alteração em decisões do Principal, cards, SHAs, evidências, permissões ou regras de publicação. Este candidato inclui o PR92 e substitui sua ativação pendente.

| Requisito | Resultado | Evidência |
|---|---|---|
| Reproduzir o defeito antes da correção | PASS | Runner canônico, 4 testes passaram e 1 falhou em test_kanban_alert_integrity.py. O aviso dizia Worker bloqueado em vez de em reavaliação. |
| Preservar aviso correto e ocultar texto interno | PASS local | O mesmo teste passou após a mudança; testes e expectativas preservados. |
| Preservar envio único e confirmação independente pelo Principal | PASS local | test_nfos_terminal_receipt e test_kanban_notifier_durable passaram. |
| Preservar mensagens de encerramento e contratos de homologação/candidato | PASS local | 38 testes passaram, zero falhas, em seis arquivos, 35,5 segundos. |
| Lint e diff | PASS | Ruff 0.15.10 e git diff --check. |
| Mesmo código no Linux | RUNNING | Executor isolado, sem banco ou decisões de produção. |
| Ativação nos gateways | NOT_RUN | A versão anterior permanece ativa até encerramento natural dos workers e validação deste candidato. |
| Evidência visual real | PASS | Captura headless do resultado executado abaixo; os logs originais acompanham o arquivo. Não é comprovação de UI de produto. |

![Captura real dos testes](outputs/nfos-12h/reassessment-homolog-evidence/contracts.png)

O CI geral do PR92 não passou. O CI do PR91 já registrava as mesmas dez falhas e 26 erros de configuração de perfil em test_kanban_tools.py, além das falhas de áudio citadas anteriormente. A regressão de texto aqui corrigida foi identificada por comparação e reproduzida localmente. Há outras falhas fora dos arquivos alterados e variações de temporização; elas não são apresentadas como resolvidas nem a suíte completa como verde. Fontes locais: outputs/nfos-12h/reassessment-job-101888181535.log e outputs/nfos-12h/closeout-job-101921924412.log.

O orçamento desta retomada foi ampliado explicitamente em cinco pontos percentuais: parada em 15% usado, na mesma janela, preservando a margem até 16%. Sem interferência manual nos cards de produto.
