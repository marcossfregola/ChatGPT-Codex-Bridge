# ChatGPT–Codex Bridge

Bridge local para uso real controlado: ChatGPT invoca herramientas MCP, el
Bridge coordina Projects y Tasks, y `CodexExecutor` ejecuta Codex local sobre
un repositorio explícito. La versión vigente es `0.1.0` y el HEAD técnico base
es `29d524fdc1f6fed2f59e4ae4b0f7f7ff1880e864`.

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
.venv\Scripts\Activate.ps1
& .\scripts\start_mcp_tunnel.ps1
(Invoke-WebRequest -Uri http://127.0.0.1:8877/readyz -UseBasicParsing).StatusCode
& .\scripts\doctor_mcp_tunnel.ps1
& .\scripts\stop_mcp_tunnel.ps1
```

El arranque sólo se considera listo con `/readyz` HTTP 200. `doctor` es un
flujo manual que debe ejecutarse bajo la identidad Windows que creó la
credencial DPAPI. No se debe ejecutar el doctor desde el sandbox de Codex ni
mostrar la credencial.

El complemento operativo actual es **ChatGPT–Codex Bridge D2**. El complemento
original conserva un schema MCP cacheado anterior a `TaskMode`; no debe
borrarse ni modificarse destructivamente.

## Flujo

```text
ChatGPT
  → Secure MCP Tunnel
  → MCPServer oficial / MCPAdapter
  → Bridge Core
  → CodexExecutor
  → Codex app-server
  → Luna o Terra
  → repositorio del Project
  → journal y evidencia
  → ChatGPT
```

Las ocho tools actuales son:

`get_status`, `create_project`, `create_task`, `run_task`, `get_task`,
`get_task_events`, `get_result` y `commit_checkpoint`.

`create_task` acepta `READ_ONLY` (default) y `AUTONOMOUS_WRITE`. El modo
`AUTONOMOUS_WRITE` requiere autorización explícita y auditoría postflight; no
es aislamiento adversarial del host.

`commit_checkpoint` recibe `task_id` y `message`. Sólo crea un commit local
después de que la Task `AUTONOMOUS_WRITE` terminó, su postflight durable fue
revisado y la auditoría de ChatGPT aprobó el checkpoint. La autorización se
concede por etapa lógica: una cadena `A → A-R1 → A-R2` produce como máximo un
checkpoint sobre la última Task aprobada. No realiza push, tag, release,
merge, rebase, reset ni clean.

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

Para arquitectura, seguridad, lifecycle, continuation y limitaciones, ver
`ARCHITECTURE.md` y `SECURITY.md`.
