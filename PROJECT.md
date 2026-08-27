# Project — ChatGPT–Codex Bridge

## Estado y objetivo

El Bridge es infraestructura local para uso real controlado. Coordina
Projects y Tasks, conserva evidencia durable y ejecuta Codex local detrás de
un contrato estable. El estado técnico base es `0.1.0` en
`389ef55928415470e68309ef01763261439a0cd9`.

El primer proyecto consumidor será el **ComfyUI Orchestrator**, con un
repositorio independiente previsto aproximadamente como:

```text
C:\Codex\ComfyUI-Orchestrator
```

Ese repositorio no se crea ni se modifica como parte del Bridge. El Orquestador
no importa al Bridge como librería: el Bridge es infraestructura externa de
desarrollo.

Es un repositorio local persistente y controlado, no un laboratorio descartable.
Por lo tanto es un destino válido para `AUTONOMOUS_WRITE` siempre que conserve
Git, sea recuperable y permanezca fuera de los protected roots.

## Roles y autoridades

- **Usuario:** alcance, autorización y aprobación final.
- **ChatGPT:** dirección técnica, definición de Tasks y auditoría.
- **Codex:** ejecución local y dueño de su sesión, `CODEX_HOME`, threads y rollouts.
- **Bridge:** Projects, Tasks, lifecycle, correlación, policy, persistencia y evidencia.

## Flujo operativo

```text
crear repo independiente
→ baseline Git limpio para una nueva cadena
→ crear Project
→ crear Task pequeña
→ inspeccionar READ_ONLY
→ autorizar AUTONOMOUS_WRITE si corresponde
→ revisar resultado, journal y postflight
→ definir la siguiente Task sólo sobre evidencia válida
```

Una nueva cadena usa `baseline_kind=clean`. Una continuación válida puede
usar `baseline_kind=continuation` sobre un repositorio dirty únicamente cuando
coincide exactamente con el postflight durable de la Task `AUTONOMOUS_WRITE`
previa válida; no se exige volver a limpiar o descartar el repositorio.

## Incorporar un proyecto nuevo

1. Crear un repositorio independiente.
2. Inicializar Git y crear un baseline limpio.
3. Confirmar que la ruta no sea un protected root del Bridge.
4. Crear el Project mediante `create_project`.
5. Comenzar con una Task pequeña.
6. Usar `READ_ONLY` para inspección.
7. Usar `AUTONOMOUS_WRITE` sólo con autorización explícita.
8. Auditar resultado, eventos y Git postflight antes de avanzar.

Para ComfyUI Orchestrator se aplican exactamente estos pasos sobre su
repositorio separado. El Bridge no crea el repositorio ni decide su contenido.

Una secuencia activa puede continuar técnicamente sin intervención del usuario
entre Tasks: D3 registró `USER_INTERVENTION_BETWEEN_TASKS=0`. El usuario sigue
siendo necesario para decisiones de producto/UX, autorización inicial del modo
y operaciones sensibles cuando corresponda.

## Alcance MVP

- Projects y Tasks persistidos en SQLite.
- Lifecycle `QUEUED`, `RUNNING`, `FINISHED`, `FAILED` y `CANCELLED`.
- `CodexExecutor` detrás del Executor Contract.
- Codex app-server local por stdio.
- MCP oficial v2 con siete tools.
- Event Journal durable y correlación thread/turn.
- `READ_ONLY` y `AUTONOMOUS_WRITE`.
- Git checkpoint/postflight y continuación conservadora.
- Secure MCP Tunnel local independiente.

`WAITING_USER` existe en el modelo, pero no tiene flujo activo en este MVP.

## Fuera de alcance

No forman parte del MVP: GUI, scheduler, Telegram, GitHub, despliegues remotos,
OpenCodeExecutor, multi-executor, persistent threads, dashboard, retries
complejos, rollback automático y una API OpenAI facturable obligatoria.

## Economía y dependencia externa

El Bridge no declara ni requiere una OpenAI API facturable adicional. Utiliza la
sesión/cupo Codex existente, Python local, SQLite, MCP y el Secure MCP Tunnel
configurado. La tarifa del servicio de túnel no está determinada por este
repositorio.

## Evidencia y aprobación

La suite del repositorio valida el comportamiento local. Las demostraciones E2E
D3 y D4 se obtuvieron externamente y se registran como evidencia de cierre, no
como tests reproducibles de la suite. El siguiente paso es dogfooding controlado
con el ComfyUI Orchestrator.
