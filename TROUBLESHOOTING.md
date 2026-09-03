# Troubleshooting — ChatGPT–Codex Bridge D3

Este documento es el runbook operativo para incidentes conocidos de D3. Su objetivo es evitar reinvestigar desde cero problemas ya diagnosticados, resueltos o clasificados.

Ante un incidente, usar este orden:

1. identificar el síntoma;
2. comprobar si está cubierto aquí;
3. recolectar la evidencia mínima indicada;
4. aplicar únicamente el procedimiento conocido;
5. escalar a investigación nueva sólo si la evidencia contradice este runbook o el caso no está cubierto.

La evidencia actual del repositorio/runtime prevalece sobre antecedentes históricos. No asumir que una herramienta, schema o limitación sigue igual si el código o una prueba actual demuestra lo contrario.

## 1. Estado rápido de incidentes conocidos

| Síntoma | Clasificación | Estado | Acción principal |
| --- | --- | --- | --- |
| Una continuation encuentra el working tree dirty dejado por la task anterior | Continuation legítima | RESUELTO / VERIFICADO DINÁMICAMENTE | Continuar normalmente; D3 debe reconocer el postflight anterior mediante working-tree fingerprint v1 |
| El working tree cambió fuera de D3 entre tasks | Divergencia externa | COMPORTAMIENTO FAIL-CLOSED CORRECTO / VERIFICADO | D3 debe rechazar en preflight antes de `executor.dispatch_started` |
| Se quiere aceptar deliberadamente un cambio externo | Reconciliación explícita | RESUELTO / VERIFICADO DINÁMICAMENTE | Usar Direct Baseline Adoption con `mode="direct"` |
| ChatGPT no muestra una tool/parámetro MCP nuevo aunque el MCP local sí | Schema MCP stale del complemento | RESUELTO OPERACIONALMENTE / VERIFICADO | ChatGPT → Configuración → Complementos → ChatGPT–Codex Bridge D3 → Actualizar |
| Se duda si el MCP local publica el schema correcto | Diagnóstico de schema | PROCEDIMIENTO VERIFICADO | MCP efímero + DB temporal + `tools/list` |
| READ_ONLY necesita temporales, SQLite o fixtures | Limitación Codex/Windows | DEFER / LIMITACIÓN EXTERNA ACTUAL | No asumir repo-RO + temp-WRITE; usar un modo apropiado para la auditoría/test |
| `instance_id = UNCONFIGURED` después de un arranque elevado | Identidad local | PENDIENTE | No confundir con caída de D3; revisar environment heredado antes de investigar otra cosa |
| `start_runtime.ps1` o `stop_runtime.ps1` falla con Access Denied | Lifecycle Windows | PROCEDIMIENTO OPERATIVO CONOCIDO | Confirmar cero tasks activas y reintentar los scripts normales elevados; no saltar a hard reset |
| `active_task` muestra una task FINISHED | Estado histórico | NORMAL | Mirar `active_task_source`, `worker_status`, `requested_task_id` y `running_task_id` |
| Codex intenta `rg` y no existe | Herramienta auxiliar ausente | NO ES FALLO D3 | Permitir fallback como `Select-String`; no instalar software automáticamente |
| Se necesita saber si una task llegó al executor | Observabilidad P3 | RESUELTO / VERIFICADO | Revisar presencia/ausencia de `executor.dispatch_started` |

## 2. Continuation sobre dirty state legítimo

**Estado:** `RESUELTO / VERIFICADO DINÁMICAMENTE`.

Una task `AUTONOMOUS_WRITE` puede terminar correctamente dejando cambios legítimos sin commit. Ese working tree dirty no es por sí mismo un error.

D3 captura un postflight Git completo y calcula un `working_tree_fingerprint` v1. En la siguiente continuation, el preflight debe reconocer ese estado como el estado esperado dejado por la task anterior.

Comportamiento esperado:

```text
task A FINISHED
→ postflight/fingerprint v1
→ repo legítimamente dirty
→ task B continuation
→ baseline_kind=continuation
→ previous_task_id=task A
→ executor.dispatch_started
→ ejecución normal
```

Se verificó dinámicamente una cadena real con dos continuations consecutivas:

```text
001 crea dirty state
→ 002 continuation directa
→ 003 continuation directa
→ repo limpio
```

No debe requerirse:

- task READ_ONLY auxiliar;
- Direct Baseline Adoption;
- hashes o fingerprints introducidos manualmente;
- reconciliation manual;
- limpieza previa del working tree.

Si este caso vuelve a producir `ContinuationBaselineError`, no limpiar ni adoptar nada automáticamente. Primero verificar que el working tree actual coincide realmente con el postflight durable de la task fuente.

## 3. Cambio externo inesperado entre tasks

**Estado:** `COMPORTAMIENTO FAIL-CLOSED CORRECTO / VERIFICADO DINÁMICAMENTE`.

Si un usuario, programa, otra instancia o cualquier proceso modifica el working tree después del postflight de una task, ese estado deja de coincidir con el baseline esperado.

D3 debe detenerse antes de ejecutar Codex.

Secuencia esperada:

```text
task fuente FINISHED
→ postflight/fingerprint conocido
→ cambio externo
→ continuation solicitada
→ preflight detecta mismatch
→ ContinuationBaselineError o equivalente
→ task FAILED
→ NO executor.dispatch_started
→ NO thread_id nuevo
→ NO turn_id nuevo
```

Este rechazo no es un bug: es la protección correcta contra cambios cuya procedencia D3 no puede garantizar.

### Evidencia mínima de un rechazo correcto

- `phase = preflight`;
- `baseline_kind = continuation`;
- error de mismatch (`ContinuationBaselineError` o equivalente);
- `executor.dispatch_started` ausente;
- `thread_id = null`;
- `turn_id = null`;
- working tree externo preservado.

Si aparece `executor.dispatch_started`, la task ya alcanzó el handoff al executor y el incidente no pertenece a esta categoría.

## 4. Direct Baseline Adoption

**Estado:** `RESUELTO / VERIFICADO DINÁMICAMENTE`.

Cuando un cambio externo fue detectado y se decide explícitamente conservarlo, usar la adopción directa del baseline.

Contrato esperado:

```text
adopt_reconciled_continuation_baseline(
    source_task_id=<task fuente válida>,
    mode="direct"
)
```

En direct mode:

- `inspection_task_id` es opcional y no debe ser necesario;
- no se crea una task READ_ONLY auxiliar;
- no se proporcionan hashes o fingerprints manualmente;
- D3 captura por sí mismo el estado Git;
- D3 realiza la doble captura prevista para reducir riesgo TOCTOU;
- valida project/repo, branch, HEAD, source task y políticas;
- persiste provenance durable de la adopción;
- registra `adoption_mode = direct`;
- el baseline adoptado pasa a ser autoridad para la siguiente continuation.

Una continuation que falló por mismatch **no** pasa a ser automáticamente la nueva source válida. La source sigue siendo la última task autónoma legítima sobre la cual se realiza la adopción.

### Regresión verificada

Se verificó el flujo completo:

```text
source FINISHED
→ cambio externo deliberado
→ continuation rechazada antes de dispatch
→ Direct Baseline Adoption
→ continuation post-adoption FINISHED
→ segunda continuation
→ limpieza
→ repo exactamente limpio
```

Criterios verificados:

```text
UNEXPECTED_EXTERNAL_CHANGE_REJECTED=YES
REJECTED_BEFORE_EXECUTOR_DISPATCH=YES
DIRECT_BASELINE_ADOPTION=PASS
NO AUXILIARY TASK
CONTINUATION_AFTER_DIRECT_ADOPTION=PASS
FINAL_REPO_CLEAN=YES
```

## 5. Schema MCP stale en ChatGPT

**Estado:** `RESUELTO OPERACIONALMENTE / VERIFICADO`.

### Síntoma

El código y el MCP local contienen una tool o parámetro nuevo, pero ChatGPT sigue mostrando una firma anterior.

Caso verificado: `adopt_reconciled_continuation_baseline` existía localmente con:

- `source_task_id` obligatorio;
- `inspection_task_id` opcional;
- `mode` opcional;

mientras ChatGPT seguía mostrando el contrato legacy sin `mode` y con `inspection_task_id` obligatorio.

### Lo que NO resolvió el problema

Se verificó que por sí solos no alcanzaron:

- abrir un chat nuevo;
- reiniciar worker;
- reiniciar MCP;
- reiniciar tunnel-client;
- stop/start completo del runtime.

El MCP local podía estar `CURRENT` mientras ChatGPT seguía `STALE`.

### Procedimiento correcto

Si hay sospecha de schema stale:

1. Confirmar el contrato visible en ChatGPT.
2. Si hace falta aislar el problema, verificar `tools/list` del MCP local con el procedimiento de la sección siguiente.
3. Si el MCP local está `CURRENT`, **no seguir investigando código ni runtime**.
4. En ChatGPT abrir:

```text
Configuración
→ Complementos
→ ChatGPT–Codex Bridge D3
→ Actualizar
```

5. Abrir/verificar el catálogo visible nuevamente.

Este refresh del complemento fue el paso que hizo aparecer correctamente `mode` e hizo opcional `inspection_task_id`.

### Regla operativa

Después de modificar el schema público de una tool MCP, incluir el refresh del complemento ChatGPT dentro del procedimiento de despliegue/verificación. No asumir que reiniciar el runtime refresca las definiciones visibles de tools.

## 6. Diagnóstico seguro del schema MCP local

**Estado:** `PROCEDIMIENTO VERIFICADO`.

Usar este diagnóstico sólo cuando sea necesario separar “MCP local incorrecto” de “ChatGPT tiene metadata stale”.

Procedimiento seguro:

- usar el Python existente de `.venv`;
- ejecutar un MCP server efímero;
- usar una DB temporal separada;
- no tocar la DB productiva;
- no detener el runtime productivo;
- obtener `tools/list`;
- inspeccionar literalmente el `inputSchema` de la tool afectada.

Clasificación:

```text
LOCAL_MCP_SCHEMA=CURRENT
```

si el schema local contiene los campos esperados.

Si el MCP local está CURRENT y ChatGPT no, el problema está aguas abajo del MCP local. No modificar producción para “forzar” una solución sin evidencia adicional.

En el diagnóstico realizado, las interfaces soportadas del tunnel-client/control plane no expusieron literalmente el catálogo de tools ni sus `inputSchema`, por lo que esa capa se clasificó como:

```text
TUNNEL_SCHEMA_NOT_EXPOSED
```

No inferir que el tunnel-client guarda schema stale si no existe evidencia observable.

## 7. READ_ONLY + temporales / P2

**Estado:** `DEFER / LIMITACIÓN EXTERNA CODEX-WINDOWS ACTUAL`.

Problema: ciertas auditorías/tests necesitan mantener el repo sin escrituras pero crear temporales, SQLite, fixtures u otros archivos fuera del repo.

En el entorno actual no quedó demostrado un modo nativo confiable de:

```text
repo = READ_ONLY
temp externo = WRITE
```

Las investigaciones con permission profiles, elevación, workspace-write, `windows.sandbox="unelevated"` y variantes relacionadas quedaron `DEFER`, no eliminadas.

### Regla operativa

- Usar `READ_ONLY` para inspecciones que realmente no necesitan escribir.
- Si una auditoría/test necesita temporales, elegir un modo que permita esa ejecución y prohibir explícitamente modificaciones al repo cuando corresponda.
- No asumir que READ_ONLY permite crear temporales sólo porque estén fuera del repo.
- No reabrir automáticamente investigaciones de ACL, AppContainer, elevation hacks o equivalentes.
- No considerar un fallo de creación de temp como defecto funcional del proyecto auditado hasta separar el problema de sandbox/entorno.

## 8. `instance_id = UNCONFIGURED` después de arranque elevado

**Estado:** `PENDIENTE`.

`get_status` obtiene la identidad mediante `CHATGPT_CODEX_BRIDGE_INSTANCE_ID` del environment block del proceso y devuelve `UNCONFIGURED` si no está presente o está vacío.

Se observó este caso:

```text
User-scope environment variable = PC
proceso elevado: variable ausente en su environment block
get_status.instance_id = UNCONFIGURED
```

Esto es compatible con herencia incompleta/refresco del environment block al arrancar elevado.

### Qué significa

`UNCONFIGURED` no implica por sí solo:

- worker caído;
- túnel caído;
- MCP caído;
- tasks en ejecución;
- corrupción de DB.

Evaluar separadamente `worker_active`, `worker_status`, health/readiness y requested/running tasks.

### Qué hacer hoy

Registrar el incidente y corregirlo en una tarea separada. No documentar todavía una solución como canónica hasta implementarla y verificarla.

## 9. Stop/start normal y Access Denied

**Estado:** `PROCEDIMIENTO OPERATIVO CONOCIDO`.

Scripts normales:

```powershell
pwsh -NoProfile -File .\scripts\stop_runtime.ps1
pwsh -NoProfile -File .\scripts\start_runtime.ps1
```

Antes de un reinicio comprobar:

- worker activo/estado;
- `requested_task_id = null`;
- `running_task_id = null`;
- repo del Bridge limpio cuando corresponda.

Se observaron fallos no elevados por Access Denied al consultar CIM o escribir logs de lifecycle. Si ocurre:

1. no asumir corrupción;
2. verificar que no haya task solicitada/en ejecución;
3. reintentar **los mismos scripts normales** con elevación;
4. verificar PIDs nuevos, doctors, health y readiness después.

No usar `reset_bridge.ps1` como primera respuesta a un Access Denied de lifecycle.

El hard reset sigue siendo un procedimiento de emergencia separado, con pérdida deliberada del estado durable activo y sus propias validaciones. Consultar `README.md` antes de usarlo.

## 10. `active_task` histórica no significa task activa

`get_status` puede mostrar la última task terminada como `active_task` con:

```text
active_task_source = historical
execution_status = FINISHED
```

Eso no significa que esté ejecutándose.

Para saber si existe trabajo activo mirar principalmente:

```text
worker_status
requested_task_id
running_task_id
```

Estado operativo idle esperado:

```text
worker_active = true
worker_status = idle
requested_task_id = null
running_task_id = null
```

## 11. `rg` ausente dentro de Codex

La ausencia de `rg` en un entorno no es un fallo del Bridge.

Si Codex intenta `rg`, falla y continúa dentro de la misma task usando una alternativa disponible como PowerShell `Select-String`, la ejecución puede seguir siendo válida.

No instalar software automáticamente sólo para resolver esta ausencia. Registrar el fallback únicamente si afecta el resultado o se vuelve un costo repetitivo relevante.

## 12. `executor.dispatch_started` y P3

**Estado:** `RESUELTO / VERIFICADO EN USO REAL` para la frontera durable elegida.

D3 registra `executor.dispatch_started` inmediatamente antes de `executor.run` después de claim, start y preflight/policy.

Orden conceptual:

```text
task.execution_claimed
→ task.started
→ preflight/policy
→ executor.dispatch_started
→ executor.run / Codex
```

Interpretación:

- **ausente:** D3 no alcanzó la frontera durable de handoff al executor;
- **presente:** el Core alcanzó esa frontera y comenzó la entrega al executor;
- **no significa por sí solo** que un proceso, thread o turn haya empezado físicamente.

Para demostrar ejecución real de Codex, complementar con `thread_id`, `turn_id`, eventos del turn y terminal.

Recovery de claims versionados:

- claim nuevo + sin marker → `not_reached`;
- marker válido → `reached`;
- claim legacy sin versión → `unknown`.

Existe un riesgo residual aceptado: una ventana física pequeña entre recibir un `thread_id`/`turn_id` de Codex y persistirlo mediante callback. D3 mantiene postura fail-closed; no se agregó una capa redundante que no cerrara realmente esa ventana.

## 13. D3 se trabó: diagnóstico en 30 segundos

```text
D3 parece trabado
│
├─ 1. get_status
│    ├─ worker_active=false / health no ready
│    │    └─ diagnosticar lifecycle/runtime
│    └─ worker_active=true
│         ├─ requested_task_id != null
│         │    └─ revisar claim/worker/events
│         ├─ running_task_id != null
│         │    └─ revisar last_event + executor.dispatch_started + thread/turn
│         └─ ambos null
│              └─ no hay ejecución activa; active_task puede ser histórica
│
├─ 2. Falló una continuation
│    ├─ dirty producido exactamente por task anterior
│    │    └─ continuation normal debería funcionar por fingerprint v1
│    └─ hubo cambio externo
│         ├─ rechazo antes de executor.dispatch_started
│         │    └─ comportamiento correcto
│         └─ se quiere conservar el cambio
│              └─ Direct Baseline Adoption mode="direct"
│
├─ 3. ChatGPT no ve una tool/parámetro nuevo
│    ├─ opcional: MCP local tools/list
│    ├─ MCP local STALE → investigar checkout/runtime local
│    └─ MCP local CURRENT → Actualizar complemento D3 en ChatGPT
│
├─ 4. READ_ONLY falla creando temp/SQLite/fixture
│    └─ P2 conocido; no concluir defecto del proyecto ni reinvestigar ACL desde cero
│
└─ 5. Stop/start falla con Access Denied
     └─ cero tasks activas → mismos scripts normales elevados → doctors/health
```

## 14. Evidencia mínima antes de escalar un incidente

Recolectar sólo lo necesario para ubicar la falla:

### Identidad/runtime

- `instance_id`;
- `hostname`;
- `worker_active`;
- `worker_status`;
- worker PID;
- tunnel health/readiness cuando corresponda;
- `requested_task_id`;
- `running_task_id`.

### Task

- `project_id`;
- `task_id`;
- mode/model cuando sea relevante;
- `execution_status`;
- source/previous task si es continuation;
- último evento y terminal;
- `reconciliation_required`;
- `policy_violation`.

### Frontera executor

- evento de preflight/policy relevante;
- presencia/ausencia de `executor.dispatch_started`;
- `thread_id`;
- `turn_id`;
- estado terminal del turn;
- disponibilidad de resultado durable.

### Git

- repo_path;
- branch;
- HEAD;
- `origin/main` si corresponde;
- `git status --short`;
- staged;
- `git diff --check`;
- working-tree fingerprint v1 cuando el incidente sea continuation/baseline.

No recopilar enormes dumps si estos datos ya localizan claramente la frontera del fallo.

## 15. Acciones que NO deben hacerse automáticamente

Ante un incidente común:

- NO hacer hard reset por defecto;
- NO borrar DB, state, locks o sidecars para “probar”;
- NO limpiar un working tree dirty sin entender su procedencia;
- NO hacer `git reset`, `clean`, `stash`, rebase o force push como workaround;
- NO crear una READ_ONLY auxiliar para Direct Baseline Adoption;
- NO proporcionar hashes/fingerprints manualmente si D3 debe capturarlos;
- NO instalar herramientas sólo porque falta `rg`;
- NO modificar el ChatGPT–OpenCode Bridge existente;
- NO usar commit/push/tag/release/merge/rebase sin autorización expresa;
- NO asumir que reiniciar D3 refresca el schema del complemento ChatGPT;
- NO confundir `active_task_source=historical` con ejecución activa;
- NO considerar `UNCONFIGURED` como prueba de que el worker está caído.

## 16. Regresiones operativas verificadas

Estas regresiones constituyen evidencia de comportamiento, no una suite que deba repetirse ante cada incidente.

### READ_ONLY smoke real

Verificó:

- selección de Project correcto;
- ejecución Codex/Luna real;
- `executor.dispatch_started`;
- thread/turn;
- terminal/result durable;
- repo sin cambios;
- worker retornando a idle.

### Dirty continuation

```text
AUTONOMOUS_WRITE 001
→ dirty legítimo
→ continuation 002
→ dirty legítimo nuevo
→ continuation 003
→ clean
```

Resultado: PASS sin auxiliary task, baseline manual ni reconciliation.

### Cambio externo fail-closed

```text
source FINISHED
→ modificación externa deliberada
→ continuation
→ ContinuationBaselineError en preflight
→ NO executor.dispatch_started
→ NO thread/turn
```

Resultado: PASS.

### Direct Baseline Adoption

```text
cambio externo rechazado
→ adopt_reconciled_continuation_baseline(mode="direct")
→ adoption durable
→ continuation normal
→ segunda continuation
→ cleanup
```

Resultado: PASS sin inspection task auxiliar.

### Schema stale de ChatGPT

```text
MCP local tools/list = CURRENT
ChatGPT = STALE
→ chat nuevo: sigue STALE
→ runtime completo reiniciado: sigue STALE
→ Complementos → D3 → Actualizar
→ ChatGPT = CURRENT
```

Resultado: procedimiento operativo verificado.

## 17. Cuándo abrir una investigación nueva

Abrir una investigación nueva sólo si ocurre al menos una de estas condiciones:

- el síntoma no está cubierto en este runbook;
- la evidencia actual contradice un comportamiento marcado como verificado;
- cambió una dependencia externa relevante (Codex, Windows, MCP SDK, tunnel-client, ChatGPT complement/plugin behavior);
- el workaround conocido dejó de funcionar;
- aparece riesgo de pérdida de datos, seguridad o ejecución fuera del Project autorizado.

Cuando se cierre un incidente nuevo, actualizar este runbook con:

- síntoma reproducible;
- clasificación;
- estado (`RESUELTO`, `DEFER`, `PENDIENTE`, `COMPORTAMIENTO CORRECTO`);
- evidencia mínima;
- procedimiento recomendado;
- acciones que no deben repetirse.

El objetivo es que el próximo incidente empiece por evidencia y procedimiento conocido, no por volver a descubrir la historia del Bridge.
