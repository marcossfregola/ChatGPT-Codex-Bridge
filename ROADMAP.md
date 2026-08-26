# Roadmap — ChatGPT–Codex Bridge

## Etapa 1A — COMPLETADA / SELLADA

Bootstrap formal y documentación.

## Etapa 1B — COMPLETADA / SELLADA

Spike mínimo del CodexExecutor.

Objetivo futuro:

```text
proceso Python/Node
→ app-server
→ initialize
→ account/read
→ thread/start Luna
→ turn/start
→ eventos
→ cierre
```

## Etapa 1C — COMPLETADA / SELLADA

Persistencia mínima y modelo Project/Task.

## Etapa 1D — COMPLETADA / SELLADA

Observabilidad y event journal.

## Etapa 1E-A — COMPLETADA / SELLADA

Bridge Core, Executor Contract async, CodexExecutor y ejecución real con
journal durable.

## Etapa 1E-B / 1E-B-R1 — MCP oficial — ACTUAL / PENDIENTE DE AUDITORÍA

MCP local sobre Bridge Core mediante el MCP Python SDK oficial v2 y transporte
stdio. El `MCPAdapter` conserva la frontera de aplicación; el SDK se ocupa del
protocolo, lifecycle, schemas, JSON-RPC y framing. Incluye wiring a un
CodexExecutor, persistencia SQLite reutilizable en una ruta estable de
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`, consulta del journal
y una ejecución concurrente como máximo. `--db-path` permite laboratorios
aislados.

1E-B-R1 migra el wire manual al SDK oficial y valida un flujo fake y un flujo
real MCP → Luna con reapertura durable de SQLite. El túnel y la conexión con
ChatGPT quedan para una etapa posterior.

No se planifican todavía versiones 0.2/0.3.
