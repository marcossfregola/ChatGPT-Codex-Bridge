# D3 — Modo Didáctico

**Estado:** D3-DID-3 — cierre documental de v1 como ChatGPT-first.

**Contrato v1:** `DIDACTIC_MODE_V1=CHATGPT_FIRST`

Este archivo es la autoridad funcional única del Modo Didáctico del
ChatGPT–Codex Bridge D3. `README.md`, `PROJECT.md`, `ARCHITECTURE.md`,
`SECURITY.md`, `DECISIONS.md`, `STATUS.md`, `ROADMAP.md` y
`TROUBLESHOOTING.md` pueden enlazarlo o resumirlo, pero no deben duplicar este
contrato ni definir una variante propia.

## 1. Alcance y separación de conceptos

El Bridge puede acompañar una conversación técnica en dos modos de interacción:

- `NORMAL`: respuesta técnica directa, con el nivel de detalle habitual.
- `DIDACTIC`: la misma dirección y auditoría técnica, más explicaciones
  puntuales que ayuden a comprender decisiones, riesgos y evidencia.

El modo pertenece a la sesión/conversación. Por lo tanto, sesiones distintas
pueden coexistir en `NORMAL` y `DIDACTIC`, incluso cuando usan el mismo Bridge
local.

En v1, `NORMAL`/`DIDACTIC` se resuelve exclusivamente en el contexto, las
instrucciones y la conversación de ChatGPT. No se transporta el modo al Bridge,
no se persiste allí y no se exige que el runtime lo conozca. Sólo una necesidad
demostrada, una decisión posterior y evidencia propia podrían revisar este
límite.

`InteractionMode != TaskMode`. `TaskMode` sigue siendo la política de ejecución
de una Task del Bridge y conserva únicamente `READ_ONLY` y
`AUTONOMOUS_WRITE`. El Modo Didáctico no cambia sandbox, `approvalPolicy`,
permisos, lifecycle, worker, executor, política Git, pruebas ni la semántica
de ejecución de Codex.

Las expresiones de sesión `D3_MODE=NORMAL` y `D3_MODE=DIDACTIC` son ejemplos de
una convención de interacción para una futura integración; no son una variable
de runtime implementada por esta etapa ni una configuración por proyecto,
computadora o instancia.

## 2. Forma de una respuesta DIDACTIC

La respuesta conserva primero la actualización técnica necesaria para avanzar.
Cuando una explicación aporta valor, se agrega después el bloque:

```text
PARA ENTENDER QUÉ SIGNIFICA
```

Ese bloque debe priorizar, según corresponda:

- por qué se eligió una solución y qué alternativa se descartó;
- qué riesgo controla una validación o una restricción;
- qué evidencia confirma algo y qué observación todavía no constituye prueba;
- cómo se relacionan componentes, estados, contratos y decisiones.

No se fuerzan explicaciones elementales cuando el usuario ya domina el tema ni
se interrumpe una operación técnica segura para convertir cada paso en una
clase. Las explicaciones intermedias son temporales y sirven al turno actual.

## 3. Perfil de aprendizaje del usuario

El perfil didáctico es conceptual y pertenece al usuario, no a una instalación
del Bridge. Los niveles permitidos son:

- `MASTERED`: el usuario indicó explícitamente que puede trabajar el concepto
  sin explicación adicional.
- `FAMILIAR`: reconoce el concepto y puede seguirlo, pero puede beneficiarse de
  una breve referencia.
- `LEARNING`: necesita explicación, ejemplos o comprobaciones adicionales.

La separación de ownership es obligatoria:

```text
modo              -> sesión/conversación
perfil            -> usuario
instancia Bridge  -> computadora
```

No se crea un perfil por Project, Task, `instance_id`, hostname ni copia local
de cada PC. Tampoco se crea un perfil duplicado por instancia simultánea.

### Persistencia v1 y conclusión de D3-DID-2

En v1, el perfil es una referencia conceptual que ChatGPT puede aplicar durante
la conversación usando el contexto disponible, la memoria y las señales
explícitas del usuario cuando existan. El Bridge no mantiene una base
estructurada del perfil, no lo sincroniza y no lo usa como dependencia técnica.

El spike controlado de D3-DID-2 evaluó las alternativas consideradas para una
autoridad compartida y no justificó implementar ninguna en esta versión. Por lo
tanto, el estado normativo es:

```text
GLOBAL_PROFILE_PERSISTENCE=DEFERRED_UNLESS_NEEDED
```

Esto no promete persistencia determinista, acceso programático a una cuenta,
consistencia transaccional, CAS ni sincronización multi-PC. Tampoco convierte
una memoria, archivo local, repositorio, servidor o nube en autoridad del
Bridge. Una etapa futura sólo podrá reabrir esta decisión si el dogfooding
produce una necesidad concreta y una auditoría propia la aprueba.

### Alcance multi-PC y concurrencia

La separación conceptual sigue siendo:

```text
modo              -> sesión/conversación
perfil            -> usuario
instancia Bridge  -> computadora
```

PC, notebook y futuras instancias pueden usar `NORMAL` o `DIDACTIC` en sus
respectivas conversaciones porque el modo pertenece a la sesión. En v1 el
Bridge no guarda ni intercambia perfiles entre ellas, no crea copias locales y
no requiere CAS. La continuidad del perfil entre conversaciones es una
capacidad best-effort de ChatGPT y de las señales explícitas del usuario, no un
contrato de ejecución ni una garantía de sincronización.

### Actualización explícita

El perfil no se actualiza por inferencia. No bastan una explicación mostrada,
la ausencia de preguntas, la cantidad de veces que apareció un concepto ni una
suposición de ChatGPT.

Una actualización requiere una señal explícita del usuario, por ejemplo:

- “esto ya lo entendí”;
- “esto todavía me cuesta”;
- “pasá X a familiar”.

La señal debe identificar el concepto y el cambio pretendido cuando no sea
inequívoco. La confirmación explícita puede mover un concepto entre
`MASTERED`, `FAMILIAR` y `LEARNING`; la exposición por sí sola no lo hace.

## 4. Marcadores didácticos

Los marcadores son señales de feedback del usuario, no órdenes de ejecución:

| Marcador | Tratamiento |
| --- | --- |
| `[APRENDER]` | Indica interés o necesidad de refuerzo. No ordena una operación ni convierte automáticamente el concepto en `MASTERED`. |
| `[NO_ENTENDI]` | Pide una explicación distinta, más concreta o con otro ejemplo. Puede justificar conservar o marcar el concepto como `LEARNING` cuando el concepto queda explícito. |
| `[PROFUNDIZAR]` | Pide más profundidad técnica. No cambia automáticamente el nivel del perfil. |
| `[YA_LO_SE]` | Es una señal explícita de incorporación. Puede promover a `MASTERED` cuando el concepto referido es inequívoco. |

Un bloque marcado describe feedback didáctico. Puede ser interpretado
exclusivamente por ChatGPT: no es autorización, no es una Task, no es una
instrucción Git y no se envía automáticamente a Codex ni se inserta en sus
requests de ejecución. Tampoco reduce criterios de aprobación, seguridad,
pruebas o auditoría. Si el mismo mensaje contiene instrucciones operativas,
éstas se analizan por separado con las reglas normales de alcance,
autorización, seguridad y auditoría.

## 5. Aprendizajes de este turno

Al cierre de un turno `DIDACTIC` se puede incluir, de forma breve y opcional,
como comportamiento exclusivamente de ChatGPT:

```text
Aprendizajes de este turno
```

Debe conservar sólo lecciones útiles y temporales del turno: conceptos
aclarados, decisiones entendidas o preguntas abiertas. No es una transcripción,
no es un registro obligatorio, no requiere almacenamiento ni runtime y no
actualiza automáticamente el perfil.

## 6. Degradación y fallos didácticos

La falta de continuidad o precisión del contexto didáctico no es un fallo del
Bridge. Si un concepto no puede clasificarse con seguridad, ChatGPT puede
dejarlo sin clasificar, pedir una señal explícita o repetir una explicación. La
redundancia, una explicación insuficiente o la pérdida de una preferencia no
pueden alterar `TaskMode`, Codex, el repositorio, Git, los outputs, la seguridad,
las aprobaciones ni la auditoría. El trabajo técnico continúa con las reglas
normales.

## 7. Journal didáctico

El Event Journal actual del Bridge registra eventos de Tasks y no es un perfil
de aprendizaje ni un journal didáctico. D3-DID-3 no agrega tablas, eventos,
tools ni persistencia para este fin.

```text
DIDACTIC_JOURNAL=DEFERRED
```

Si en el futuro se justifica un journal, deberá ser separado del estado y de
`task_events`, opcional, conciso, centrado en el usuario y distinto de un
transcript. Su diseño requerirá una decisión posterior y no puede inferirse de
la existencia del Event Journal técnico.

## 8. Límites no negociables

El Modo Didáctico no:

- cambia `TaskMode`, Codex, prompts técnicos normales, sandbox,
  `approvalPolicy`, Git, lifecycle, worker, executor, MCP tools ni runtime;
- habilita auto-aprobaciones, omite auditorías, pausa o bloquea Tasks por el
  estado del perfil;
- crea perfiles por proyecto, Task, computadora, `instance_id` o hostname;
- depende de que un perfil esté disponible para permitir el trabajo técnico;
- convierte el Bridge en LMS, sistema de cursos, motor de inferencia ni
  subsistema de sincronización compleja.

Esta etapa modifica únicamente documentación. Cualquier persistencia, interfaz
MCP, lectura/escritura de perfil o integración de sesión sólo se evaluaría en
una etapa posterior si existe necesidad demostrada, con diseño, pruebas y
evidencia propios; no forma parte de v1.

## 9. Dogfooding y reapertura controlada

El siguiente paso es observar el uso humano del modo durante el desarrollo
técnico real, inicialmente en el **ComfyUI Orchestrator**. Se observará, sin
telemetría ni logs nuevos, si aparecen repetición frecuente de conceptos
`MASTERED`, pérdida relevante de feedback entre conversaciones, dificultad para
usar `LEARNING` o una necesidad real de continuidad estructurada entre equipos.

Sólo incidentes concretos y repetibles pueden justificar reabrir la decisión de
persistencia. Hasta entonces no se crea almacenamiento, sincronización ni un
journal didáctico.
