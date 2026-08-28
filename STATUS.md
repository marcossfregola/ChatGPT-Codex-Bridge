# Status — ChatGPT–Codex Bridge

```text
Proyecto: ChatGPT–Codex Bridge
Versión: 0.1.0
Estado: D3-R2-B APTO PARA E2E REAL (preparación completada)
Rama: main
HEAD técnico base R2-A: 66db63d8d7a2a3737fe6bf3cbf0c98ee94037db0
Suite: 207 tests OK (unittest discover -s tests)
```

## Etapas completadas

- 1A — bootstrap.
- 1B — Codex app-server executor.
- 1C — Projects/Tasks y persistencia SQLite.
- 1D — Event Journal durable.
- 1E-A — Bridge Core, Executor Contract y CodexExecutor.
- 1E-B — MCP Bridge.
- 1F-B — Secure MCP Tunnel independiente.
- 1F-C / E2E real — complemento y flujo ChatGPT → túnel → MCP.
- D1 — lifecycle, recovery y single-instance.
- D2 — `AUTONOMOUS_WRITE` y Git checkpoint/postflight.
- D2-CONT — continuación sobre estado Git verificado.
- D2-CONT-R2 — baseline limpio y fallos terminales.
- D3 — cadena adaptativa multi-Task.
- D4 — long-run controlado.
- D3-R2-A — dispatch durable y execution worker persistente auditados.
- D3-R2-B — lifecycle del worker, wrappers PS7, doctor, observabilidad y
  preparación E2E completados; sin commit.
- 1H-B / 1H-B-R1 — checkpoint commits locales auditados.
- 1H-C — cadena real de checkpoints A→B→C.

## MCP vigente

El servidor usa el MCP Python SDK oficial v2 y expone exactamente ocho tools:

```text
get_status
create_project
create_task
run_task
get_task
get_task_events
get_result
commit_checkpoint
```

Los modos de Task son:

```text
READ_ONLY
AUTONOMOUS_WRITE
```

No existe `post_audit` ni una tool pública de cancelación. `audit_status` queda
en `PENDING` hasta que exista una auditoría externa o una capacidad futura.

## Evidencia E2E externa

La siguiente evidencia fue obtenida externamente y se documenta aquí para el
cierre; no fue reproducida por la suite unitaria del repositorio.

### D3 adaptativa

Se completaron tres Tasks `AUTONOMOUS_WRITE` en cadena:

```text
Task A → auditoría
Task B definida después de la auditoría A → auditoría
Task C definida después de la auditoría B → auditoría

TASK_B_DEFINED_AFTER_A_AUDIT=true
TASK_C_DEFINED_AFTER_B_AUDIT=true
USER_INTERVENTION_BETWEEN_TASKS=0
TASKS_COMPLETED_AUTONOMOUSLY=3
```

### D4 long-run

```text
task-1f-d4-long-real
espera real: 75.160 s
Task total: aproximadamente 94 s
resultado: D4_LONG_OK
run_task retornó normalmente
USER_INTERVENTION=0
```

### 1H-C — checkpoint commits reales

```text
HEAD0: 5b7880e19f48a7525ae1b33f5ab07cf745c37b7e
HEAD_A: 72c95e3f67182237ff1b34b1e1e9bfb8649bece0
HEAD_B: 77ac4d46b7715e9624bd04a9c991ee72c80a1466
HEAD_C: bf5ed3eb4e9d84564937d40d654c682904ae5a9f
CHECKPOINTS_CREATED=3
A necesitó una continuation correctiva.
B y C no.
CLEAN_AFTER_EACH_CHECKPOINT=true
PUSH=0
USER_INTERVENTION_BETWEEN_STAGES=0
```

La autorización se precisó por etapa lógica: la cadena A→A-R1 produjo un
único checkpoint sobre A-R1 antes de continuar con B y C.

## Runtime

El runtime se instala de forma independiente bajo:

```text
%LOCALAPPDATA%\ChatGPTCodexBridge
```

La base default es
`%LOCALAPPDATA%\ChatGPTCodexBridge\state\bridge.sqlite3`. El readiness del
túnel se verifica con `http://127.0.0.1:8877/readyz` HTTP 200. Los scripts
versionados son `scripts/start_runtime.ps1`, `scripts/stop_runtime.ps1`,
`scripts/start_execution_worker.ps1`, `scripts/stop_execution_worker.ps1`,
`scripts/doctor_execution_worker.ps1` y los scripts del túnel.

El arranque completo inicia primero el único execution worker y después el
túnel; ambos componentes son idempotentes y un fallo parcial se informa sin
reiniciar el componente que quedó vivo. La parada detiene túnel/MCP y luego
envía la señal de stop al worker. El worker mantiene sidecars de PID, state,
stop y lock junto a `bridge.sqlite3`, no depende del túnel y no comparte nada
con los Bridges protegidos.

`run_task` es dispatch rápido: persiste `task.execution_requested` y el
resultado se observa haciendo polling con `get_task`, `get_task_events` y
`get_result`. No hay long-polling ni `cancel_task` pública. Un zombie histórico
`QUEUED` sin request no se ejecuta; un `RUNNING` sin owner vivo se recupera
fail-closed como `FAILED` al reiniciar el worker.

El runtime operativo de esta etapa es **ChatGPT–Codex Bridge D3-R2-B**. El
complemento original conserva un schema MCP cacheado anterior a `TaskMode`; no
debe borrarse ni modificarse destructivamente.

## Evidencia D3-R2-B

La suite local verificó start idempotente, double-start, stop controlado,
wrappers de runtime, doctor read-only, lock único, requests pendientes,
recovery fail-closed de `RUNNING`, zombies ignorados y el retorno rápido de
`run_task` frente a un executor deliberadamente lento. El harness real aislado
del Codex app-server usó un `CODEX_HOME` temporal sin Project ni Task; al matar
abruptamente al owner, el child terminó por el cierre de stdin:

```text
REAL_ORPHAN_PROBE: REAL CHILD TERMINATES RELIABLY
```

Por esa evidencia no se agregó Job Object. El resultado prepara el runtime para
el E2E largo posterior, pero no ejecuta todavía ese E2E ni reinicia el runtime
productivo.

## Limitaciones MVP

- No hay timeout total de Task; el timeout de turno es de inactividad de 300 s.
- No hay E2E real de desconexión ChatGPT/MCP.
- No se ha probado un crash productivo del runtime durante Luna.
- `audit_status` permanece `PENDING` y no existe `post_audit`.
- El Bridge no puede despertar ChatGPT espontáneamente.
- `AUTONOMOUS_WRITE` usa `approvalPolicy=never` y `sandbox=danger-full-access`.
- Los protected roots son policy, no un sandbox adversarial.
- No hay rollback automático, retries complejos, scheduler ni múltiples
  workers.
- No existe una tool pública `cancel_task`; el stop es un control local del
  worker con grace period.
- El stop script conserva un race benigno cuando el proceso ya terminó.
- `WAITING_USER` existe en el modelo, pero no tiene flujo activo.

## Próximo uso

1H queda cerrada. El Bridge queda **APTO PARA USO REAL CONTROLADO /
DOGFOODING** mediante el desarrollo del **ComfyUI Orchestrator** en un
repositorio separado. El hardening posterior debe guiarse por incidentes
reales, no por complejidad preventiva.
