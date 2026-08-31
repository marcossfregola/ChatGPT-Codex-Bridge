# Architecture — ChatGPT–Codex Bridge v0.1

## Estado

Arquitectura implementada y validada para un **MVP apto para uso real
controlado**, con dispatch durable D3-R2-B y un único execution worker
persistentemente controlado. El repositorio no pretende ser un sistema de
aislamiento adversarial ni un scheduler autónomo.

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
  ↓ task.execution_requested (SQLite)
ExecutionWorker (único owner)
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
Codex. Las ocho tools son `get_status`, `create_project`, `create_task`,
`run_task`, `get_task`, `get_task_events`, `get_result` y `commit_checkpoint`.

### Bridge Core

`BridgeCore` es autoridad sobre Projects, Tasks, dispatch, lifecycle, policy,
correlación y transiciones. `run_task` persiste una solicitud durable y no
ejecuta Codex dentro del request MCP. Depende de `SQLiteBridgeStore` y del
`Executor Contract`; no depende de MCP ni del protocolo app-server.

### Execution worker y ownership

`ExecutionWorker` es el único owner de la ejecución. Reclama atómicamente la
Task `QUEUED` más antigua que tenga `task.execution_requested`, registra el
owner persistente y ejecuta una sola Task por vez. El lock
`<db>.execution-worker.lock` y los sidecars `<db>.execution-worker.pid`,
`state.json` y `stop` pertenecen exclusivamente a esa base. El worker se
puede iniciar sin túnel ni MCP y sobrevive a su reinicio.

El stop escribe una señal de control acotada; el worker deja de reclamar,
solicita `cancel_active` al executor, persiste `task.cancelled` cuando la
cancelación alcanza una Task `RUNNING` y sale dentro del grace period. No hay
servidor HTTP de control, kill por nombre global, scheduler, múltiples workers
ni una tool pública `cancel_task`.

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

El journal conserva lifecycle, dispatch (`task.execution_requested` y
`task.execution_claimed`), correlación, notificaciones Codex, checkpoints,
postflight, policy violations, resultados y errores acotados. El dispatch usa
el schema v3 existente; no agrega tablas ni migra el schema para R2-B. No
replica `CODEX_HOME`, credenciales, sesiones ni rollouts Codex.

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
raíz. El perfil externo del tunnel-client usa `channel: main`, el mismo Tunnel
ID histórico autorizado y MCP stdio. La readiness se comprueba con
`tunnel-client health --port 8877 --pid-file <tunnel.pid>
--require-control-plane-poll --json`; no depende de `health.url`.

El lifecycle normal se opera con `start_runtime.ps1` y `stop_runtime.ps1`.
`start_runtime.ps1` inicia worker y túnel en ese orden, sin duplicar los que ya
estén vivos, arrancando el túnel directo con `tunnel-client run
--profile-file <profile-file> --pid.file <tunnel.pid>`. `stop_runtime.ps1`
solicita primero el cierre controlado del worker mediante stop-file y luego
ejecuta el Stop directo del túnel. Ese Stop usa el PID sidecar
`%LOCALAPPDATA%\ChatGPTCodexBridge\tunnel-state\tunnel.pid`, valida que el PID
vivo corresponda exactamente al executable instalado y, sólo después, termina
exclusivamente ese PID y su árbol con `taskkill.exe /PID <verified_pid> /T /F`.
Un PID ausente o stale se trata de forma idempotente; un PID inválido o un
executable ajeno falla cerrado. No hay búsqueda ni terminación por nombre
genérico. El Tunnel ID sólo debe estar operativo en una máquina a la vez.
`doctor_execution_worker.ps1` es sólo lectura y muestra PID/state, proceso,
lock, DB y Tasks solicitadas o en ejecución.

El protocolo de emergencia `scripts/reset_bridge.ps1` es deliberadamente
distinto: tras verificar PID, ejecutable, command line CIM y perfil, puede
terminar el túnel directo de esta instalación durante la recuperación. Si CIM no
entrega una identidad única, falla cerrado y no detiene el proceso. Después
mueve el directorio `state` completo a `state.archive` y arranca el worker sobre
una base nueva; nunca recupera ni reconcilia la base archivada. Este protocolo
no cambia el lifecycle normal directo descrito arriba.
El protocolo no enumera ni modifica repositorios de Projects y conserva
credenciales, perfil y binarios.

El runtime no reutiliza rutas, procesos, locks, perfiles ni secretos del
ChatGPT–OpenCode Bridge ni de VisorVideosDevBridge.

## Lifecycle real

Estados implementados:

```text
QUEUED + execution_requested → RUNNING → FINISHED
                 ↘ FAILED
                 ↘ CANCELLED
```

`WAITING_USER` existe en el modelo y se considera activo para `get_status`,
pero no tiene flujo activo en el MVP.

- `QUEUED → RUNNING` es una actualización condicional y agrega `task.started`
  atómicamente.
- `run_task` devuelve la aceptación durable rápidamente; el worker agrega
  `task.execution_claimed` antes de `task.started` y el cliente hace polling
  con `get_task`, `get_task_events` y `get_result`.
- Una Task histórica `QUEUED` sin `task.execution_requested` permanece ignorada;
  no existe ejecución automática de zombies.
- Preflight fallido produce `policy.violation` y un único `task.failed`.
- Cada transición terminal verifica que no exista otra terminalidad y se
  confirma en una transacción SQLite.
- La cancelación conserva `task.cancelled` y propaga `CancelledError`.
- Un executor fallido conserva `task.failed`.
- Al arrancar, las Tasks `RUNNING` huérfanas se recuperan fail-closed como
  `FAILED`, con `task.recovered` y `task.failed`. Una Task `RUNNING` legítima
  no se relanza por reiniciar MCP.
- Las Tasks terminales no se pueden relanzar.
- `MCPInstanceLock` impide dos servidores sobre la misma DB y
  `ExecutionWorkerLock` impide dos owners de ejecución sobre la misma DB.

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

## Checkpoint commits locales

Después de un postflight durable `FINISHED` y de una auditoría ChatGPT
`APPROVED`, `commit_checkpoint(task_id, message)` valida nuevamente el Project,
la última Task de la etapa lógica y el estado Git. Compara branch, HEAD,
fingerprints y paths exactos; prepara un índice temporal, neutraliza hooks y
signing, y crea un único commit local con identidad Git command-scoped. Luego
instala atómicamente el índice real y verifica que el repositorio quede clean.

La autorización cubre una etapa lógica, no un `task_id` rígido: una cadena
`A → A-R1 → A-R2` sólo puede producir un checkpoint sobre `A-R2`, si es la
última Task aprobada. El checkpoint no hace push, tag, release, merge, rebase,
reset ni clean; `Luna`/`CodexExecutor` tampoco reciben permiso para commitear.

## App-server: aprobaciones y tiempos

`READ_ONLY` usa `approvalPolicy=on-request`, reviewer de usuario, sandbox
read-only y red deshabilitada en el turno.

`AUTONOMOUS_WRITE` usa `approvalPolicy=never` y `sandbox=danger-full-access`.
Esto es una decisión pragmática para el MVP, no un aislamiento fuerte.

Los RPC cortos tienen deadline total de 30 s. La espera de turno tiene un
timeout de inactividad de 300 s entre mensajes, no un timeout total de Task.
El cierre espera 5 s y luego mata únicamente el proceso hijo propio si es
necesario. El harness aislado de R2-B observó que el app-server real termina
fiablemente cuando su owner muere y se cierra el stdin (`REAL CHILD TERMINATES
RELIABLY`); por eso no se agregó un Job Object ni containment adicional.

## Observabilidad y límites

`get_status` incorpora la señal bounded del worker (`worker_active`,
`worker_pid`, `worker_owner`, `requested_task_id` y `running_task_id`), además de
`instance_id` configurado localmente mediante
`CHATGPT_CODEX_BRIDGE_INSTANCE_ID` y `hostname` obtenido del sistema local.
El `instance_id` se recorta y toma `UNCONFIGURED` cuando falta o está en blanco;
no hay una identidad de máquina hardcodeada. `doctor_execution_worker.ps1`
verifica el proceso y la DB con más detalle.
`get_task_events` expone el journal y `get_result` recupera la respuesta final
y la evidencia Git. No se persisten rollouts Codex, credenciales, raw stderr,
duración total, retry history ni una auditoría ChatGPT automática.

Las demostraciones E2E D3 y D4 fueron obtenidas externamente; la suite local no
reproduce esas Tasks reales. Sus datos de cierre están en `STATUS.md`.
