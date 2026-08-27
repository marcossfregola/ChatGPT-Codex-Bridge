# Architecture — ChatGPT–Codex Bridge v0.1

## Estado

Arquitectura implementada y validada para un **MVP apto para uso real
controlado**. El repositorio no pretende ser un sistema de aislamiento
adversarial ni un scheduler autónomo.

## Fronteras

```text
ChatGPT
  ↓
Secure MCP Tunnel
  ↓ stdio
MCPServer oficial v2
  ↓
MCPAdapter
  ↓
Bridge Core
  ↓ Executor Contract
CodexExecutor
  ↓ stdio://
Codex app-server
  ↓
Luna o Terra
```

### MCP server y adapter

`mcp_server.py` construye un `MCPServer` del MCP Python SDK v2. El SDK posee
initialize, schemas, JSON-RPC, framing, lifecycle, errores y transporte stdio.
`MCPAdapter` es la frontera de aplicación y no importa el SDK ni el wire de
Codex. Las siete tools son `get_status`, `create_project`, `create_task`,
`run_task`, `get_task`, `get_task_events` y `get_result`.

### Bridge Core

`BridgeCore` es autoridad sobre Projects, Tasks, lifecycle, policy,
correlación y transiciones. Depende de `SQLiteBridgeStore` y del
`Executor Contract`; no depende de MCP ni del protocolo app-server.

### Projects y Tasks

Un Project identifica un repositorio. Una Task conserva objetivo, executor,
model, mode, estados, timestamps y referencias opcionales `thread_id` y
`turn_id`. Task, thread y turn son identidades distintas.

Los modos son:

- `READ_ONLY`: inspección sin escritura.
- `AUTONOMOUS_WRITE`: escritura autorizada en un repositorio controlado, con
  checkpoint y postflight.

### SQLite y Event Journal

SQLite contiene únicamente estado propio del Bridge: Projects, Tasks y un
journal append-only `task_events`. Cada evento tiene `event_id` autoincremental,
`source`, `kind`, payload JSON y timestamp UTC. Los eventos se recuperan en
orden de `event_id`.

El journal conserva lifecycle, correlación, notificaciones Codex, checkpoints,
postflight, policy violations, resultados y errores acotados. No replica
`CODEX_HOME`, credenciales, sesiones ni rollouts Codex.

### Policy

La policy canonicaliza el repositorio, exige la raíz Git correcta, rechaza
protected roots, captura branch/HEAD, estado Git y fingerprints SHA-256, y
compara el postflight. También limita payloads y elimina claves sensibles de
notificaciones.

### Executor Contract y CodexExecutor

El contrato sólo expone `ExecutionRequest`, `ExecutionResult`, callbacks de
correlación/notificación y `run`. `CodexExecutor` traduce el contrato al
app-server y garantiza el cierre del proceso que él mismo inició.

### Codex app-server

El cliente inicia:

```text
codex app-server --listen stdio://
```

Correlaciona respuestas, registra notificaciones, observa
`thread/started`, `turn/started` y `turn/completed`, y soporta
`turn/interrupt`. EOF, JSON inválido, respuestas inesperadas y timeouts son
errores explícitos.

### Secure MCP Tunnel

El runtime independiente vive en:

```text
%LOCALAPPDATA%\ChatGPTCodexBridge
```

La base default es
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`; `--db-path` permite
laboratorios aislados. Los scripts versionados start/stop/doctor usan sólo esa
raíz. El perfil externo del tunnel-client usa `channel: main`, el tunnel ID
autorizado, MCP stdio y readiness local en `127.0.0.1:8877/readyz`.

El runtime no reutiliza rutas, procesos, locks, perfiles ni secretos del
ChatGPT–OpenCode Bridge ni de VisorVideosDevBridge.

## Lifecycle real

Estados implementados:

```text
QUEUED → RUNNING → FINISHED
                 ↘ FAILED
                 ↘ CANCELLED
```

`WAITING_USER` existe en el modelo y se considera activo para `get_status`,
pero no tiene flujo activo en el MVP.

- `QUEUED → RUNNING` es una actualización condicional y agrega `task.started`
  atómicamente.
- Preflight fallido produce `policy.violation` y un único `task.failed`.
- Cada transición terminal verifica que no exista otra terminalidad y se
  confirma en una transacción SQLite.
- La cancelación conserva `task.cancelled` y propaga `CancelledError`.
- Un executor fallido conserva `task.failed`.
- Al arrancar, las Tasks `RUNNING` huérfanas se recuperan como `FAILED`, con
  `task.recovered` y `task.failed`.
- Las Tasks terminales no se pueden relanzar.
- `MCPInstanceLock` impide dos servidores sobre la misma DB.

## AUTONOMOUS_WRITE y continuation

Antes de ejecutar, el Bridge registra un `policy.git_checkpoint`.

Un repositorio limpio usa:

```text
baseline_kind=clean
previous_task_id=null
```

Un repositorio dirty sólo continúa si coincide exactamente con el postflight
durable de una Task `AUTONOMOUS_WRITE` previa, `FINISHED` y sin policy
violation. Se comparan branch, HEAD, staged, unstaged, untracked, diffs,
cached diffs y fingerprints de contenido. Evidencia truncada, legacy dirty sin
fingerprints o cualquier cambio externo se rechaza.

El postflight registra branch/HEAD finales, archivos cambiados, untracked y
policy violation. No hace commit, reset, clean ni rollback automático.

## App-server: aprobaciones y tiempos

`READ_ONLY` usa `approvalPolicy=on-request`, reviewer de usuario, sandbox
read-only y red deshabilitada en el turno.

`AUTONOMOUS_WRITE` usa `approvalPolicy=never` y `sandbox=danger-full-access`.
Esto es una decisión pragmática para el MVP, no un aislamiento fuerte.

Los RPC cortos tienen deadline total de 30 s. La espera de turno tiene un
timeout de inactividad de 300 s entre mensajes, no un timeout total de Task.
El cierre espera 5 s y luego mata únicamente el proceso hijo propio si es
necesario.

## Observabilidad y límites

`get_task_events` expone el journal y `get_result` recupera la respuesta final y
la evidencia Git. No se persisten rollouts Codex, credenciales, raw stderr,
duración total, retry history ni una auditoría ChatGPT automática.

Las demostraciones E2E D3 y D4 fueron obtenidas externamente; la suite local no
reproduce esas Tasks reales. Sus datos de cierre están en `STATUS.md`.
