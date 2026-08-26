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

## Etapa 1E-B / 1E-B-R1 — MCP oficial — COMPLETADA / SELLADA

MCP local sobre Bridge Core mediante el MCP Python SDK oficial v2 y transporte
stdio. El `MCPAdapter` conserva la frontera de aplicación; el SDK se ocupa del
protocolo, lifecycle, schemas, JSON-RPC y framing. Incluye wiring a un
CodexExecutor, persistencia SQLite reutilizable en una ruta estable de
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`, consulta del journal
y una ejecución concurrente como máximo. `--db-path` permite laboratorios
aislados.

1E-B-R1 migra el wire manual al SDK oficial y valida un flujo fake y un flujo
real MCP → Luna con reapertura durable de SQLite. El túnel y la conexión con
ChatGPT quedaron para 1F.

No se planifican todavía versiones 0.2/0.3.

## Etapa 1F-B — Secure MCP Tunnel local independiente — PREPARADA / DOCTOR MANUAL PENDIENTE

La etapa prepara un runtime nuevo bajo `%LOCALAPPDATA%\ChatGPTCodexBridge`, sin
reutilizar estado, perfiles, procesos, logs, secretos ni rutas del
ChatGPT–OpenCode Bridge. El perfil dedicado usa el tunnel ID no secreto
`tunnel_6a8ef626bf008191a6294996145747e5`, credencial referenciada como
`env:CONTROL_PLANE_API_KEY`, MCP stdio sobre la copia nueva del tunnel-client,
base SQLite estable y health local en `127.0.0.1:8877`.

Se agregan scripts acotados de start/stop y un doctor manual. El doctor debe
ejecutarse desde PowerShell bajo la identidad Windows normal que creó la
credencial DPAPI; Codex no lo ejecuta ni intenta acceder al plaintext. El
arranque sólo se considera listo con `/readyz` HTTP 200.

Quedan fuera de esta etapa: interacción con ChatGPT, creación del complemento,
Luna, Project/Task de validación y cualquier modificación al Bridge OpenCode.

## Etapa 1F-C — complemento ChatGPT — PENDIENTE

Se iniciará únicamente después de validar manualmente el doctor, el runtime,
la negociación MCP y la independencia de ambos bridges.
