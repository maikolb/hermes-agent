# Profile plugins: hermes-project-factory

Fonte de verdade versionada dos plugins profile-local do perfil `hermes-project-factory` (resgate P1, 28/08/2026: estes artefatos viviam apenas no runtime da VPS).

Instalação: copiar o diretório do plugin para `HERMES_HOME/profiles/hermes-project-factory/plugins/` do alvo. Paridade runtime↔repo é verificada por hash no gate de promoção; divergência no runtime sem PR correspondente é regressão (ver AOF `framework/docs/multiagent-git-flow.md`).

Fora deste diretório de propósito: `aof-route-policy` NÃO é versionado aqui. Origem canônica: repo AgentOperatingFramework (`adapters/hermes/plugin/aof-route-policy/`), instalado com fingerprint por home (runtime hash-pinado). Vendorizar aqui criaria segunda fonte de verdade que a próxima instalação do AOF sobrescreveria.
