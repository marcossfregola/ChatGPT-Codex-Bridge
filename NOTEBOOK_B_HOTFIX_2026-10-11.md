# NOTEBOOK-B — cierre local del hotfix rebase (2026-10-11)

**Estado:** despliegue y ciclo operativo B homologados. **Alcance:** runtime local `NOTEBOOK-B` de `BOOK-MARCOS`, independiente del Principal `NOTEBOOK` y del ChatGPT–OpenCode Bridge. No confundir este cierre con una integración de código en `main` de GitHub.

## Objetivo y solución instalada

Ante una divergencia de continuidad Git por avance externo legítimo, el mecanismo `adopt_reconciled_continuation_baseline(mode="rebase")` permite propuesta previa ligada al snapshot/fingerprint y adopción posterior con aprobación humana explícita. Su procedimiento de uso y prueba funcional real original están documentados en [INCIDENT_NOTEBOOK_CONTINUATION_BASELINE_2026-10-10.md](INCIDENT_NOTEBOOK_CONTINUATION_BASELINE_2026-10-10.md). El modo `direct` no es un sustituto automático de `rebase` si cambió HEAD.

Se copió la release local aprobada de A a una carpeta propia de B:
`%LOCALAPPDATA%\ChatGPTCodexBridgeB\releases\principal-reanchor-approved\src`.
El launcher homologado es:
`%LOCALAPPDATA%\ChatGPTCodexBridgeB\scripts\start_runtime_b_candidate.ps1`.
El perfil efectivo es:
`%LOCALAPPDATA%\ChatGPTCodexBridgeB\tunnel-client\profiles\chatgpt-codex-bridge-b-candidate.yaml`.
La extensión real `.yaml` es necesaria; el tunnel-client rechazó `.yaml.proposed`. El entorno del candidato antepone la release B en `PYTHONPATH` para su runtime, conservando identidad y estado exclusivos de B. Se mantuvieron copias de respaldo del launcher y perfil anteriores. No reutilizar destructivamente el checkout compartido ni los procesos/estado del Principal.

SHA-256 de los archivos homologados:
- Launcher candidato: `6538160044120791E0FE62A99C1293AAB8FCC651CDFB951CFE8CF16DF5061846`.
- Perfil candidato: `80245FFC0964A548EBF1D3CAC882A145D194CACAF0E128BAEB3AE55569BE969F`.
- Manifest de la copia de release, obtenido durante el despliegue: `0282E1B1F73DFFA76C2EF8D6E7B61E20DCF32E8998659B1E0F10BD8B4CE377A1`.

## Complemento MCP

Se actualizó la definición de herramientas del complemento Secundario en ChatGPT y se comprobó en un chat nuevo que `adopt_reconciled_continuation_baseline` acepta `source_task_id`, `inspection_task_id`, `mode`, `approved_fingerprint` y `human_approval`. `get_status` respondió con `instance_id=NOTEBOOK-B`, worker activo/idle y sin tarea en ejecución. Este PASS verifica el contrato visible y la conectividad; **no se ejecutó una adopción `rebase` real en B**. La ejecución real documentada corresponde al Principal.

## Accesos directos y cierre operativo

- **Bridge Secundario - START**: el acceso directo existente pasó a invocar `start_runtime_b_candidate.ps1`, manteniendo ejecutable y directorio de trabajo. Su copia anterior quedó respaldada. SHA-256 del acceso directo actualizado: `79F17486E4BCA39D0E11F9EC2CF28240EE3B104EFFAFE2BA45BC16F6A6B83868`.
- **Bridge Secundario - STATUS**: sin cambios; confirma identidad, PID y health/readiness cuando está levantado.
- **Bridge Secundario - STOP**: acceso directo sin cambios; su script destino `%LOCALAPPDATA%\ChatGPTCodexBridgeB\scripts\stop_runtime_b.ps1` fue ajustado para solicitar parada ordenada sólo al worker B, verificar estado/PID/DB propios sin CIM ni terminaciones forzadas, e identificar el túnel para **cierre manual exclusivamente tras cotejar PID, ejecutable y línea de comandos** en Administrador de tareas. SHA-256 del STOP actualizado: `71B4F84D70A1A7E2F13439CA3788E3A86B427C26F9B0E9639316C9899B29BB42`. No automatizar el cierre del túnel por nombre o PID no verificado.

Prueba de apagado real: STOP pidió y confirmó salida ordenada del worker B PID histórico `75524`; tras el cierre manual verificado del túnel B PID histórico `70196`, STATUS devolvió `WORKER=NO_RUNNING_STALE_PID`, `TUNNEL=NO_RUNNING_STALE_PID` y health/readiness sin conexión, resultado esperado al estar apagado. La presencia del sidecar stale fue reconocida y no se borró.

Prueba de arranque posterior por START: `WORKER=STARTED` PID histórico `14392`, `TUNNEL=STARTED` PID histórico `10100`, `INSTANCE=NOTEBOOK-B`, DB B y `RUNTIME_START=OK`. Comprobación posterior de ChatGPT: los MCP de B (`NOTEBOOK-B`) y Principal (`NOTEBOOK`) respondieron, workers idle. Estos PID y el puerto efímero de salud son evidencia histórica y **no parámetros para próximos incidentes**.

## Recuperación futura y restricciones

Primero clasificar si se trata de un problema de continuidad Git o de lifecycle; consultar la sección 18 de [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Un reinicio no corrige una baseline Git persistente desfasada. No eliminar SQLite, usar hard reset, limpiar untracked, ejecutar Git destructivo ni trasladar la tarea al otro puente como atajo. Una adopción real siempre requiere propuesta exacta, revisión y autorización humana separada. Para lifecycle B, STATUS primero; STOP sólo cuando no hay ejecución pendiente, identificación positiva del túnel y START después del apagado confirmado.

**Límite de publicación:** ésta es documentación de un despliegue local ya homologado. No incluye módulos Python de la release, scripts productivos, accesos directos binarios ni modificaciones de base de datos en el repositorio Git. La trazabilidad e integración del código de la release en el `main` formal requieren un diff y una auditoría propios; no afirmar que ya está integrado por este commit documental.
