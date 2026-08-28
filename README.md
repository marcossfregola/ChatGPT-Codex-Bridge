# ChatGPT–Codex Bridge

Bridge local para uso real controlado: ChatGPT invoca herramientas MCP, el
Bridge coordina Projects y Tasks, y un execution worker persistente entrega
las Tasks aceptadas a `CodexExecutor` sobre un repositorio explícito. La
versión vigente es `0.1.0` y el HEAD técnico base de D3-R2-A es
`66db63d8d7a2a3737fe6bf3cbf0c98ee94037db0`.

El consumidor previsto del MVP es el desarrollo del Orquestador ComfyUI. El
ChatGPT–OpenCode Bridge existente es infraestructura independiente y no se
reutiliza ni se modifica.

## Requisitos

- Windows y PowerShell.
- Python 3.13 o superior.
- El entorno `.venv` del repositorio.
- Sesión/cupo Codex existente y el ejecutable local de Codex.
- Secure MCP Tunnel configurado bajo `%LOCALAPPDATA%\ChatGPTCodexBridge`.

## Quickstart

Desde `C:\Codex\ChatGPT-Codex-Bridge`:

```powershell
pwsh -NoProfile -File .\scripts\start_runtime.ps1
(Invoke-WebRequest -Uri http://127.0.0.1:8877/readyz -UseBasicParsing).StatusCode
& .\scripts\doctor_mcp_tunnel.ps1
& .\scripts\doctor_execution_worker.ps1
pwsh -NoProfile -File .\scripts\stop_runtime.ps1 -TunnelRuntimeAlias <managed-alias>
```

El arranque sólo se considera listo con `/readyz` HTTP 200 y un worker activo.
`start_runtime.ps1` inicia primero el worker y después el túnel; es idempotente
y reporta arranques parciales. `stop_runtime.ps1` solicita primero el cierre
controlado del worker mediante su stop-file y luego detiene un túnel supervisado
por `tunnel-client runtimes stop`. El alias gestionado debe pasarse
explícitamente: un perfil directo no ofrece un mecanismo local de parada
graceful verificable y el script se niega a terminar procesos. Los wrappers
requieren PowerShell 7 (`pwsh -NoProfile`). Los `doctor` son flujos manuales de
sólo lectura que deben ejecutarse bajo la identidad Windows que creó la credencial
DPAPI. No se debe ejecutar el doctor desde el sandbox de Codex ni mostrar la
credencial.

El runtime operativo de esta etapa es **ChatGPT–Codex Bridge D3-R2-B**. El
complemento original conserva un schema MCP cacheado anterior a `TaskMode`; no
debe borrarse ni modificarse destructivamente.

## Flujo

```text
ChatGPT
  → Secure MCP Tunnel
  → MCPServer oficial / MCPAdapter
  → Bridge Core
  → SQLite durable (request)
  → execution worker persistente
  → CodexExecutor
  → Codex app-server
  → Luna o Terra
  → repositorio del Project
  → journal y evidencia
  → ChatGPT
```

Las tools actuales son:

`get_status`, `create_project`, `create_task`, `run_task`, `get_task`,
`get_task_events`, `get_result`, `resolve_task_reconciliation` y
`commit_checkpoint`.

`create_task` acepta `READ_ONLY` (default) y `AUTONOMOUS_WRITE`. El modo
`AUTONOMOUS_WRITE` requiere autorización explícita y auditoría postflight; no
es aislamiento adversarial del host.

`commit_checkpoint` recibe `task_id` y `message`. Sólo crea un commit local
después de que la Task `AUTONOMOUS_WRITE` terminó, su postflight durable fue
revisado y la auditoría de ChatGPT aprobó el checkpoint. La autorización se
concede por etapa lógica: una cadena `A → A-R1 → A-R2` produce como máximo un
checkpoint sobre la última Task aprobada. No realiza push, tag, release,
merge, rebase, reset ni clean.

Una ejecución cuyo resultado no puede demostrarse después de una pérdida del
worker permanece `RUNNING` con `task.reconciliation_required`; no se convierte
automáticamente en `FINISHED`. La única resolución administrativa disponible es
`resolve_task_reconciliation(..., resolution="FAILED")`, que no reanuda ni
re-ejecuta la Task.

Flujo aprobado: `AUTONOMOUS_WRITE → FINISHED → postflight durable → auditoría
ChatGPT → commit_checkpoint → commit local → repo clean → siguiente Task`.
Si una etapa requiere corrección, `A → correction required → A-R1 continuation
→ audit approved → checkpoint de A-R1`; la Task original no se checkpointa.

El repositorio objetivo puede ser persistente: no tiene que ser descartable.
Debe ser local, controlado, versionado con Git, recuperable y estar fuera de
los protected roots. Una nueva cadena parte de `baseline_kind=clean`; una
continuación válida puede usar `baseline_kind=continuation` sobre un estado
dirty que coincida exactamente con el postflight durable de la Task anterior.

El flujo normal es crear un Project con un repositorio independiente, crear
una Task pequeña, inspeccionar inicialmente en `READ_ONLY`, autorizar
`AUTONOMOUS_WRITE` sólo cuando corresponda y revisar el resultado y el journal
antes de continuar. Una secuencia técnica ya autorizada puede encadenar Tasks
sin intervención entre ellas, como demuestra D3; tras la auditoría aprobada,
`commit_checkpoint` deja el repositorio clean para la siguiente Task. Las
decisiones importantes de producto/UX y las operaciones sensibles siguen
requiriendo al usuario cuando corresponda.

La evidencia E2E D3/D4 se obtuvo externamente y se documenta en
`STATUS.md`; no forma parte de la suite unitaria reproducible.

### Dispatch y polling

`run_task` sólo valida y persiste una solicitud `task.execution_requested` en
SQLite. Devuelve la aceptación sin esperar a Codex ni al resultado remoto. Un
único worker persistente reclama las solicitudes explícitas en orden y escribe
`task.execution_claimed`, `task.started` y la transición terminal. El cliente
consulta `get_task`, `get_task_events` y `get_result`; no hay long-polling ni
una tool pública `cancel_task`.

Una Task histórica que quedó `QUEUED` sin `task.execution_requested` es un
zombie y no se ejecuta automáticamente. Una Task `RUNNING` sin evidencia
terminal durable suficiente queda pendiente de reconciliación; no se infiere el
resultado por PID, silencio o desaparición del lock.

## Runtime D3-R2-B

El worker usa la misma base y perfil de D3 bajo
`%LOCALAPPDATA%\ChatGPTCodexBridge`. Además de la base
`state\bridge.sqlite3`, mantiene sidecars de PID, estado JSON, señal de stop y
lock con scope de esa base. `doctor_execution_worker.ps1` informa la
consistencia PID/state, el proceso verificado, el lock y las Tasks
`QUEUED` solicitadas o `RUNNING`.

El worker no inicia MCP ni el túnel y no comparte rutas, procesos, perfiles,
locks o secretos con `ChatGPTOpenCodeBridge` o `VisorVideosDevBridge`. El túnel
puede reiniciarse sin reiniciar el worker; reiniciar MCP tampoco reclama de
nuevo una Task `RUNNING` legítima.

El sidecar `state.json` se publica mediante un archivo temporal propio, flush y
`fsync`, seguido de un `replace` atómico con reintentos acotados para el
`PermissionError` transitorio de Windows. Si la publicación sigue fallando, el
worker conserva SQLite como autoridad durable, registra el error y continúa; un
sidecar de observabilidad no puede detener ni marcar fallida la ejecución.

Para arquitectura, seguridad, lifecycle, continuation y limitaciones, ver
`ARCHITECTURE.md` y `SECURITY.md`.
