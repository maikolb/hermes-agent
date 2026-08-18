# Hermes Revision Protocol

Autoridade canônica de regressões operacionais deste checkout.

## REG-2026-08-17-001 — Titan pareado/conectado sem responder após reboot

- Status: closed — validated-target
- Cenário reproduzível: após queda de energia e novo pareamento, a tarefa `Hermes_Gateway` iniciava, mas o bridge Node lançado pelo gateway morria com exit 1. Um bridge Node iniciado diretamente ficava `connected`, porém sem a allowlist do perfil e recusava inbound. Quando o inbound finalmente chegou, uma saudação simples caiu em Sol/High e tentou compactar uma sessão antiga de 392 mensagens (~330 mil tokens).
- Causa raiz: o broker Windows devolvia ao adapter o PID/lifecycle do runner `pythonw`, não do Node long-lived que o adapter precisa monitorar, registrar e encerrar. O processo temporário não carregava o ambiente do perfil. Em paralelo, `smart_model_routing.platforms` continha apenas `telegram`, deixando WhatsApp no modelo baseline.
- Correção: capacidade interna `direct_hidden_child_env()` para subprocessos long-lived manterem PID/stdio/lifecycle reais sob desktop privado + Job Object; adapter WhatsApp opta explicitamente por essa capacidade. Perfil default inclui `whatsapp` no smart model router.
- Prevenção vinculada:
  - `tests/test_windows_process_broker.py::test_direct_hidden_child_marker_is_capability_scoped`
  - `tests/gateway/test_whatsapp_connect.py::TestWindowsBridgeLifecycle`
  - `scripts/whatsapp-bridge/bridge.native.test.mjs`
  - `docs/EXECUTION_CONTRACT.md`
- Comando de validação: Python 3.11 com `venv/Lib/site-packages` em `PYTHONPATH`, executando `pytest tests/test_windows_process_broker.py tests/gateway/test_whatsapp_connect.py -q`; depois restart real de `Hermes_Gateway`, `/health=connected`, PID file igual ao listener Node e monitor com `VisibleWindows=0`.
- Evidência final: 43 testes broker/WhatsApp/setup + 1 contrato zero-UI passaram; restart real passou; tarefa está `Running`; bridge está `connected`; allowlist e modo bot estão configurados; zero janelas novas em 20 s. Inbound novo foi roteado para Luna/Low, resposta ficou pronta em 11,4 s e o ledger marcou a entrega como `delivered`.

## REG-2026-08-18-001 — Roteamento rápido sem quality gate empírico

- Status: open — mitigation validated-local; empirical calibration pending
- Cenário reproduzível: uma saudação curta era automaticamente enviada para Luna/Low ou Haiku/Low, e tarefas comuns para Terra/Sonnet, apenas pelos sinais estruturais/lexicais do pedido. Os testes provavam a matriz e a velocidade do classificador, mas não comparavam a qualidade das respostas com Sol/Fable em tarefas representativas.
- Causa raiz: o primeiro desenho tratou complexidade estimada como autorização de downgrade e usou o microbenchmark local (~tempo de decisão) como evidência de desempenho do roteador. Isso não mede acurácia, cobertura de edge cases, qualidade do resultado nem calibração do reasoning.
- Mitigação imediata: ausência de política de benchmark revision-bound agora falha fechado para GPT-5.6 Sol/XHigh e Claude Fable/XHigh. Escolha explícita do usuário continua soberana. As seis configurações declaram `quality_policy: conservative`.
- Hardening: a palavra `benchmarked` na configuração não é autoridade. O código contém uma allowlist de hashes de benchmark intencionalmente vazia; só uma revisão de fonte que fixe a evidência aprovada pode habilitar rota menor.
- Correção definitiva planejada: benchmark global sanitizado por classe de tarefa, com repetição, gates determinísticos, avaliação cega e comparação de latência somente entre rotas que atingirem o mesmo quality bar. Rotas menores serão allowlisted por classe e vinculadas ao hash do router/harness/evidência; qualquer drift/expiração volta ao fail-safe forte.
- Prevenção vinculada:
  - `tests/gateway/test_smart_model_routing.py::test_missing_benchmark_policy_fails_closed_to_strongest_lane`
  - `tests/gateway/test_smart_model_routing.py::test_benchmarked_label_without_revision_bound_hash_still_fails_closed`
  - `docs/EXECUTION_CONTRACT.md`
  - harness global de qualidade a ser criado em `C:\Users\maiko\agent-ops\benchmarks\hermes-model-router-quality-20260818`
- Comando de validação atual: `venv\Scripts\python.exe -m pytest -q tests\gateway\test_smart_model_routing.py`; verde definitivo exige também o benchmark revision-bound e smoke do runtime consumindo sua allowlist.
- Evidência atual: 30 testes focados verdes; nenhuma rota automática menor é autorizada enquanto o benchmark permanece pendente.
