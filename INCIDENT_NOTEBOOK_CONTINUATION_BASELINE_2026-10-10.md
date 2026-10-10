# Incidente operativo 2026-10-10 — NOTEBOOK: ContinuationBaselineError tras avance externo de Git

**Estado:** CERRADO en NOTEBOOK, verificado con una Task real `AUTONOMOUS_WRITE`.  
**Alcance:** exclusivamente Puente Principal `NOTEBOOK`; `NOTEBOOK-B` y ChatGPT–OpenCode Bridge no se tocaron.  
**Importante:** este documento registra un incidente y su recuperación *local*. A fecha del incidente, el `main` remoto del Bridge estaba en `7d30e80cece9a81dfeb04452fd650f84822166ea` y **no contenía necesariamente el hotfix `rebase` desplegado en NOTEBOOK**. No interpretar este runbook como disponibilidad de la función en cualquier clon ni aplicar `git pull` sobre un checkout operativo con cambios preexistentes.

## Síntoma y causa

Una Task de escritura terminó correctamente con un postflight durable en `main@42a21a08325efe376c3785ba1ba6030d4b3bba40` (origen `task-c7de13cd5a30432989dc7047e72c3360`). Desde otros hilos se publicaron commits legítimos y el repositorio de conocimiento de Saniferr avanzó hasta `main@4cc38b089c7c932a119b77a21566821f53f1f8d5`, conservando 17 archivos untracked y sin archivos tracked modificados. Una nueva Task `AUTONOMOUS_WRITE` falló en preflight: `ContinuationBaselineError: current Git state does not match the previous autonomous postflight`. Reiniciar el worker/túnel no borra ni corrige la baseline persistida en SQLite.

El modo preexistente `direct` no permitía adoptar un HEAD distinto del postflight fuente: una captura nueva calculaba `policy_violation=true`. `READ_ONLY` podía ser una salida limitada para análisis, **no** recuperaba `AUTONOMOUS_WRITE`.

Se desarrolló en worktree aislado una operación administrativa `adopt_reconciled_continuation_baseline(mode="rebase")`: propuesta ligada a instancia, fuente, destino y snapshot Git completo; avance fast-forward en la misma rama; doble captura, huellas de untracked, aprobación explícita y validación fail-closed en la siguiente preflight. `direct` quedó sin cambios. Las pruebas focales informaron 10 PASS; posteriormente, cinco pruebas focales del hotfix Unicode también pasaron. La adopción no garantiza una transacción atómica conjunta Git+SQLite: si Git cambia después de persistir, la siguiente preflight debe bloquear antes del dispatch.

### Dos obstáculos resueltos durante el despliegue

1. ChatGPT conservaba una definición MCP antigua. La release declaraba los argumentos de entrada `approved_fingerprint` y `human_approval`; `proposal_fingerprint` es un **campo de salida**, no el nombre del argumento de aprobación. Se utilizó **Gestionar aplicación → Actualizar herramientas** en ChatGPT y se comprobó el contrato desde un chat nuevo. No confundir un rechazo de revisión automática por falta de autorización de adopción con un error de servidor.
2. La preparación `rebase` falló con `rebase requires a fingerprint for every untracked file`. Git devolvía la ruta `Metodología-fuentes.png` escapada/entrecomillada (`Metodolog\\303\\255a-fuentes.png`); el parser intentaba leer una ruta inexistente. Corrección mínima en `src/chatgpt_codex_bridge/policy.py`: ejecutar esa captura de `git status` con `core.quotePath=false` **sólo para ese comando**, y añadir una prueba Unicode. La captura real volvió a producir huellas para los 17 untracked. No se borraron, movieron ni indexaron.

## Despliegue local que funcionó

Se preparó una release de código estable y exclusiva para A:

`%LOCALAPPDATA%\ChatGPTCodexBridge\releases\principal-reanchor-approved\src`

El launcher A `%LOCALAPPDATA%\ChatGPTCodexBridge\scripts\desktop_a_lifecycle.ps1` antepone dicha ruta a `PYTHONPATH` **sólo durante START y para sus procesos hijos**. Utiliza el intérprete existente. Se respaldó el launcher y `policy.py`, se compararon hashes de origen/destino, y únicamente NOTEBOOK tuvo STOP/START oficial. Los módulos `execution_worker`, `mcp_server` y `policy` importaron desde esa release. `get_status` confirmó `instance_id=NOTEBOOK`, worker activo e idle. La instalación y el perfil de NOTEBOOK-B, su SQLite y el checkout de código compartido original quedaron intactos.

**No usar el worktree temporal de Codex como instalación permanente. No copiar indiscriminadamente este código al checkout compartido.** Una integración futura del hotfix al repositorio formal requiere revisión/diff propios; este cierre documental no la realiza.

## Procedimiento corto si vuelve a aparecer

**No ejecutar `reset_bridge.ps1`, borrar/mover SQLite, hacer `git reset/clean`, eliminar untracked, cambiar de Bridge automáticamente ni reintentar `direct` a ciegas.** El reset de emergencia documentado en README archiva el estado y crea otra base: es un procedimiento diferente y no debe usarse para una simple divergencia de continuidad.

1. Verificar mediante D3 `get_status` que la instancia sea `NOTEBOOK`, el proyecto/ruta sea el correcto, worker idle y sin tareas pendientes/en ejecución. Confirmar qué Task `FINISHED` `AUTONOMOUS_WRITE` tiene el último postflight válido; **no reutilizar automáticamente los IDs y SHA históricos de este incidente**. Confirmar el Git actual: misma rama, avance fast-forward desde la fuente, `HEAD=origin/main` si ese es el destino aprobado, índice/tracked limpios y snapshot completo de untracked. Ante discrepancia o huellas incompletas: STOP, no limpiar.
2. En una conversación con **schema MCP actualizado**, pedir exclusivamente la **propuesta**:
   `adopt_reconciled_continuation_baseline(source_task_id="<TASK_FINISHED_VALIDADA>", mode="rebase")`
   sin `approved_fingerprint` y con `human_approval=false`. Debe responder `proposal=true`, `adopted=false`, HEAD fuente y destino, recuento/huellas de untracked y `proposal_fingerprint`. **La llamada preparatoria no autoriza adopción.** Si no existe `rebase` en el servidor o su contrato MCP, STOP.
3. Mostrar la propuesta exacta al propietario y obtener **autorización humana expresa** para ese fingerprint y la instancia. Sólo entonces hacer la **segunda llamada oficial**, sin cambiar el origen:
   `adopt_reconciled_continuation_baseline(source_task_id="<MISMA_TASK>", mode="rebase", approved_fingerprint="<FINGERPRINT_COMPLETO_DEVUELTO>", human_approval=true)`.
   El booleano es una declaración del cliente MCP, **no** una prueba criptográfica de identidad. Si Git cambió desde la propuesta, esperar rechazo; preparar otra. Nunca improvisar un reanclaje manual de SQLite.
4. Confirmar `adopted=true` y el evento durable `reconciliation.baseline_adopted` (nuevo HEAD, `adoption_mode=rebase`, `baseline_kind=reconciled_continuation`, fuente histórica y fingerprint). Probar una Task mínima `AUTONOMOUS_WRITE` **con autorización aparte**, de consulta Git solamente, sin modificar archivos ni ejecutar scripts de negocio. Exigir `policy.git_checkpoint` aceptado, `executor.dispatch_started`, ejecución real, `policy.postflight` sin violaciones, `FINISHED`, HEAD y untracked intactos. Terminar; no realizar tests indefinidos.

**Regla operativa:** esto debe resolverse con una propuesta y una aprobación, sin reparaciones generales. Un reinicio simple puede reparar un proceso caído, pero no restablece una baseline persistente desfasada.

## Evidencia específica de cierre, NO reutilizable como parámetros futuros

- Instancia/proyecto: `NOTEBOOK` / `project-saniferr-lectura-contable-puntual`, repo `C:\Codex Estadisticas Saniferr\saniferr-analisis-conocimiento`.
- Origen: `task-c7de13cd5a30432989dc7047e72c3360`, postflight `2623198`, HEAD histórico `42a21a08325efe376c3785ba1ba6030d4b3bba40`.
- Destino: `main@4cc38b089c7c932a119b77a21566821f53f1f8d5`; 17 untracked, cero tracked modificados.
- Fingerprint aprobado para **esta única recuperación**: `e747309635fca3362d6eb26c53da58d45483bcadf43cc824d3b68325a195b1eb`.
- Adopción oficial D3: `adopted=true`, `adoption_mode=rebase`; evento `2634166`, con `policy_violation=false` y `baseline_kind=reconciled_continuation`. Historial de tareas conservado.
- Prueba posterior real: `2634171` checkpoint aceptado; `2634172` dispatch; `2634174` thread; `2634180` turn; `2634294` comandos Git exit 0; `2634428` postflight PASS sin archivos cambiados, fingerprints de 17 untracked y `reconciliation_required=false`; cierre `2634429` FINISHED. No commit, push, modificaciones a fuentes de Saniferr ni intervención en NOTEBOOK-B.

**Alcance de la evidencia:** confirma la recuperación efectiva de ese proyecto/instancia/estado, no certifica todos los escenarios futuros ni implica que el hotfix ya esté incorporado al `main` del repositorio.
