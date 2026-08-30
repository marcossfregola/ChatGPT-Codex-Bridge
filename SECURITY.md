# Security — ChatGPT–Codex Bridge MVP

El MVP está destinado a uso real controlado. No proporciona aislamiento
adversarial del host y no debe presentarse como autonomía desatendida
permanente.

## A. Protección técnica

El Bridge:

- canonicaliza `repo_path` y exige que sea la raíz del worktree Git;
- rechaza el repositorio del Bridge y las raíces protegidas
  `ChatGPTCodexBridge`, `ChatGPTOpenCodeBridge` y `VisorVideosDevBridge`;
- captura checkpoint y postflight Git;
- compara branch, HEAD, staged, unstaged, untracked, diffs y fingerprints
  SHA-256;
- rechaza divergencias externas y evidencia truncada;
- registra una única transición terminal por Task;
- usa `MCPInstanceLock` para single-instance por base SQLite;
- usa `ExecutionWorkerLock` para un único owner de ejecución por base;
- mantiene sidecars de PID/state/stop acotados a la DB y verifica el ejecutable
  del PID antes de operar sobre él;
- detiene el worker mediante una señal explícita y grace period, sin kill
  global ni servidor HTTP de control;
- mantiene el runtime y los secretos bajo
  `%LOCALAPPDATA%\ChatGPTCodexBridge`;
- limita profundidad, cantidad y tamaño de evidencia;
- elimina claves sensibles de payloads de notificación y redacta stderr
  sensible.

El runtime nuevo no reutiliza ni modifica carpetas, locks, PIDs, procesos,
perfiles, MCP, túneles o secretos del ChatGPT–OpenCode Bridge.

El túnel y MCP son transporte/request boundaries, no execution owners. `run_task`
persiste una solicitud bounded en SQLite; el worker persistente es el único
componente que reclama y entrega la Task a Codex. Las respuestas se obtienen
por polling de `get_task`, `get_task_events` y `get_result`. No existe
`cancel_task` pública, scheduler ni ejecución automática de Tasks históricas
`QUEUED` sin una solicitud durable.

## B. Restricción contractual a Codex

Para `AUTONOMOUS_WRITE`, el objetivo recibe explícitamente estas restricciones:

```text
NO commit, NO push, NO tag/release.
NO merge/rebase/reset/clean.
NO install/uninstall.
NO modificaciones a otros repositorios o Bridges.
No operaciones destructivas no solicitadas.
```

Estas instrucciones son una restricción contractual y deben acompañarse con
auditoría postflight. No son un mecanismo de aislamiento del sistema operativo.

### Checkpoint local posterior a auditoría

`commit_checkpoint` sólo se acepta para la última Task aprobada de una etapa
lógica, después de verificar el postflight durable. El Bridge Core/Policy
comprueba branch y HEAD exactos, estado Git, fingerprints SHA-256 y paths
exactos; prepara un índice temporal, neutraliza hooks y signing, instala el
índice real de forma atómica y verifica el resultado final.

El resultado es únicamente un commit local. No depende de GitHub ni del
keyring, no hace push, tag, release, merge, rebase, reset ni clean, y la
identidad Git se pasa sólo con `git -c`. Una cadena `A → A-R1 → A-R2` puede
producir como máximo un checkpoint sobre la última Task aprobada; la operación
no autoriza commits adicionales y `Luna`/`CodexExecutor` no commitean.

## C. Riesgo aceptado del MVP

`AUTONOMOUS_WRITE` usa exactamente:

```text
approvalPolicy=never
sandbox=danger-full-access
```

**PROTECTED ROOTS NO SON UN SANDBOX.** Una vez iniciado Codex con
`danger-full-access`, el Bridge no proporciona aislamiento adversarial del
host. El modo sólo debe utilizarse en repositorios locales controlados y con
revisión posterior del resultado y del postflight. "Controlado" no significa
"descartable": un repositorio persistente como ComfyUI Orchestrator es válido
si permanece versionado, recuperable y fuera de los protected roots.

No existe rollback automático. Los cambios quedan disponibles para auditoría;
la continuación sólo se permite cuando el estado Git coincide exactamente con
la evidencia durable previa.

## Modos

### `READ_ONLY`

```text
approvalPolicy=on-request
sandbox read-only
red deshabilitada en el turno
```

Es el modo recomendado para inspección inicial.

### `AUTONOMOUS_WRITE`

```text
approvalPolicy=never
sandbox=danger-full-access
```

Requiere autorización explícita y sólo aplica al repositorio del Project.
Una vez autorizada una secuencia activa, las Tasks pueden continuar sin una
intervención humana entre cada Task; la autorización de decisiones de producto
o de operaciones sensibles sigue perteneciendo al usuario cuando corresponda.

## Secretos y runtime

La credencial del Secure MCP Tunnel se almacena como archivo DPAPI externo al
repositorio. Los scripts start/doctor la recuperan con `ConvertTo-SecureString`,
la mantienen temporalmente en memoria y la entregan sólo al proceso hijo como
`CONTROL_PLANE_API_KEY`. No se imprime ni se escribe el plaintext.

El stdout del servidor MCP queda reservado al protocolo. Los diagnósticos se
envían por stderr y el tunnel-client usa su log dedicado. No se documentan ni
se incorporan claves de runtime al repositorio.

Un secreto escrito explícitamente por un usuario dentro del objetivo de una
Task no puede ser eliminado automáticamente de la fila Task; no deben incluirse
credenciales en objetivos ni prompts.

Los scripts `start_runtime.ps1` y `stop_runtime.ps1` requieren PowerShell 7 y
operan únicamente sobre `%LOCALAPPDATA%\ChatGPTCodexBridge`. El wrapper de
arranque puede dejar un estado parcial (worker vivo, túnel fallido) y lo
reporta; el wrapper de parada solicita primero el cierre del worker mediante
stop-file y sólo después usa `tunnel-client runtimes stop` con un alias gestionado
explícito. Un perfil directo no ofrece una parada graceful local verificable: el
script se niega a terminar procesos. El doctor del worker es de sólo lectura y
no elimina sidecars ni detiene procesos.

### Emergency hard reset

`scripts/reset_bridge.ps1` es una operación deliberadamente destructiva sólo
para el estado interno del Bridge. Antes de actuar comprueba PowerShell 7, el
checkout, la raíz canónica `%LOCALAPPDATA%\ChatGPTCodexBridge`, el Python del
`.venv`, el perfil, los binarios y la credencial. Cada PID se verifica contra el
ejecutable instalado, la línea de comando obtenida por CIM y la base/runtime
correspondiente; si la command line no está disponible, la identidad es ambigua,
el reset falla y no detiene ningún proceso. Nunca se termina un proceso por
nombre global. Un túnel directo puede terminarse de forma forzada únicamente
después de esa identidad exacta;
un túnel gestionado se intenta detener mediante `runtimes status/stop` y se
comprueba el mismo PID.

Tras detener los componentes, el directorio `state` se mueve atómicamente a un
directorio forense único dentro de `state.archive`. El archivo nunca se lee,
restaura ni mezcla con el estado nuevo. La base vacía se crea al arrancar el
worker con `SQLiteBridgeStore`; una consulta SQLite de sólo lectura exige schema
vigente y cero `projects`, `tasks`, `task_events` y tareas `QUEUED/RUNNING`.
`secrets`, `tunnel-client`, el perfil, el checkout y los repositorios externos no
son objetivos de limpieza. Si falla cualquier preflight, identidad, archive,
arranque, readiness o doctor, las últimas líneas son `BRIDGE_RESET=FAIL` y
`READY_FOR_CHATGPT=NO`, con código de salida distinto de cero.

## Tiempos y recuperación

- RPC corto: deadline total de 30 s.
- Turno: timeout de inactividad de 300 s entre mensajes.
- No existe timeout total de Task.
- Cierre del app-server: 5 s y kill sólo del proceso hijo propio si hace falta.
- El stop del worker deja de reclamar, solicita `cancel_active`, espera un
  grace period acotado y persiste `task.cancelled` si había una Task RUNNING.
- Un crash deja Tasks `RUNNING` para recuperación determinista basada sólo en
  evidencia durable; si el resultado no puede demostrarse, conserva
  `task.reconciliation_required` y no inventa `FINISHED`. Las Tasks `QUEUED` sin
  `task.execution_requested` se ignoran.
- La prueba aislada con el Codex app-server real clasificó
  `REAL CHILD TERMINATES RELIABLY` al morir el owner y cerrarse stdin; no se
  añadió Job Object.

## Limitaciones conocidas

- No hay E2E real de desconexión ChatGPT/MCP.
- El harness real cubre la terminación del app-server por EOF, pero no un
  crash productivo del runtime durante Luna ni la desconexión ChatGPT/MCP.
- `audit_status` permanece `PENDING`; no existe `post_audit`.
- El Bridge no puede despertar ChatGPT ni iniciar una Task futura.
- No hay retries complejos, scheduler ni rollback automático.
- No hay múltiples workers ni una cancelación pública; el stop operativo es
  sólo el control local del worker.
- El stop script conserva un race benigno de proceso ya terminado.
- El complemento original puede conservar un schema MCP cacheado anterior a
  `TaskMode`; el runtime operativo de esta etapa es ChatGPT–Codex Bridge
  D3-R2-B.
- `WAITING_USER` existe en el modelo, pero no tiene flujo activo.
