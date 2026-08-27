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
- mantiene el runtime y los secretos bajo
  `%LOCALAPPDATA%\ChatGPTCodexBridge`;
- limita profundidad, cantidad y tamaño de evidencia;
- elimina claves sensibles de payloads de notificación y redacta stderr
  sensible.

El runtime nuevo no reutiliza ni modifica carpetas, locks, PIDs, procesos,
perfiles, MCP, túneles o secretos del ChatGPT–OpenCode Bridge.

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

## Tiempos y recuperación

- RPC corto: deadline total de 30 s.
- Turno: timeout de inactividad de 300 s entre mensajes.
- No existe timeout total de Task.
- Cierre del app-server: 5 s y kill sólo del proceso hijo propio si hace falta.
- Un crash deja Tasks `RUNNING` para recuperación determinista a `FAILED` al
  siguiente arranque.

## Limitaciones conocidas

- No hay E2E real de desconexión ChatGPT/MCP.
- No se ha probado un crash real del runtime durante Luna.
- `audit_status` permanece `PENDING`; no existe `post_audit`.
- El Bridge no puede despertar ChatGPT ni iniciar una Task futura.
- No hay retries complejos ni rollback automático.
- El stop script conserva un race benigno de proceso ya terminado.
- El complemento original puede conservar un schema MCP cacheado anterior a
  `TaskMode`; el complemento operativo es ChatGPT–Codex Bridge D2.
- `WAITING_USER` existe en el modelo, pero no tiene flujo activo.
