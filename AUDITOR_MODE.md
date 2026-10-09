# Modo Auditor / Asesor — Bridges y Git

Fecha de adopción: 2026-10-08.

Estado: **ACTIVE / ChatGPT-first**.

Este documento define un modo transversal de trabajo para usar desde el proyecto **ChatGPT–Codex Bridge** cuando Marcos necesita asesoramiento o auditoría sobre incidentes de Bridge, continuidad de tareas, estados Git, commits, pushes, worktrees, concurrencia entre hilos o recuperación segura de un trabajo interrumpido en **cualquier proyecto**.

No pertenece a Saniferr, ComfyUI ni a ningún proyecto consumidor concreto. Su alcance es el Bridge y la operación Git alrededor de los proyectos que usan el Bridge.

El objetivo es simple:

> Ante una duda o incidente operativo, Sol actúa primero como auditor/asesor, preserva el estado existente, determina qué está demostrado y recomienda el siguiente paso mínimo y seguro antes de que se toque el proyecto afectado.

Este modo no es una nueva `TaskMode` del runtime. No requiere cambios de schema ni persistencia adicional. Es una convención operativa **ChatGPT-first**, análoga en espíritu a `DIDACTIC_MODE.md`, pero orientada a incidentes, continuidad y Git.

---

## 1. Cuándo se activa

El Modo Auditor / Asesor se activa cuando Marcos trae al proyecto Bridge una situación como:

- un Bridge parece trabado, caído o inconsistente;
- una Task quedó `FAILED`, `RUNNING`, `QUEUED` o con resultado dudoso;
- una ejecución terminó después de modificar el working tree pero antes de cerrar normalmente;
- un checkout quedó dirty y no está claro si es continuidad legítima o cambio externo;
- una continuation fue rechazada;
- `reconcile_postflight_failure` o `adopt_reconciled_continuation_baseline` fue aceptado o rechazado y hay que decidir qué hacer;
- se duda si conviene preservar, abandonar, reconstruir o rehacer un intento;
- un hilo intentó cambiar del Puente Principal al Secundario o viceversa;
- hay que decidir qué Bridge usar para una tarea;
- hay dudas sobre `commit_checkpoint`, commit, push, ramas, worktrees, reset, stash, rebase o merge;
- dos hilos o dos proyectos quieren escribir en repositorios compartidos;
- se necesita decidir cuándo cerrar un checkpoint antes de cambiar de hilo;
- se quiere recuperar un trabajo sin perder cambios locales;
- el proyecto consumidor no debe convertirse en un laboratorio de reparación del Bridge.

Marcos puede iniciar el modo simplemente describiendo el problema. No tiene que conocer de antemano la herramienta, el Bridge, el estado Git ni el procedimiento correcto.

---

## 2. Rol de Sol en este modo

En Modo Auditor / Asesor, Sol actúa como:

- auditor técnico;
- asesor de recuperación;
- árbitro de continuidad;
- coordinador de Git;
- verificador de evidencia;
- diseñador del siguiente paso mínimo.

No debe actuar primero como ejecutor funcional del proyecto afectado.

La responsabilidad de Sol es:

1. identificar qué proyecto, repo, checkout y Bridge están involucrados;
2. distinguir estado confirmado de inferencias;
3. preservar cualquier trabajo existente;
4. localizar la frontera exacta del fallo;
5. decidir si aplica un procedimiento ya conocido;
6. evitar reparaciones laterales innecesarias;
7. pedir autorización humana antes de operaciones sensibles;
8. entregar un siguiente paso concreto y acotado.

Marcos no debería tener que recordar de memoria cuándo hacer commit, push, handoff de escritor, adopción de baseline o STOP. Sol debe detectarlo y avisarlo.

---

## 3. Principio de preservación

Ante un incidente, la primera regla es:

> **No destruir evidencia ni trabajo existente antes de entender de dónde proviene.**

Por defecto NO hacer:

- `git reset`;
- `git clean`;
- `git stash`;
- checkout destructivo;
- rebase;
- force push;
- borrado de outputs;
- borrado de state/DB/locks;
- hard reset del Bridge;
- recreación del proyecto afectado;
- migración silenciosa a otro Bridge;
- commit de un parche sólo para “sacarlo del medio”.

Un working tree dirty no es por sí mismo un error. Puede ser el resultado legítimo de una Task anterior.

---

## 4. Orden obligatorio de diagnóstico

Usar siempre esta secuencia:

```text
problema
→ inspección mínima
→ clasificación
→ diagnóstico
→ diseño de recuperación
→ ejecución autorizada
→ pruebas/evidencia
→ auditoría
→ aprobación
→ commit
→ push
```

No saltar directamente a “arreglar”.

La inspección inicial debe ser proporcional al incidente. Evitar investigaciones largas cuando pocos datos alcanzan para ubicar la falla.

---

## 5. Evidencia mínima

Cuando corresponda, recolectar sólo lo necesario.

### Bridge

- instancia / `instance_id`;
- hostname cuando ayude;
- `worker_status`;
- `requested_task_id`;
- `running_task_id`;
- `active_task_source`;
- Task afectada;
- mode/model;
- último evento;
- `thread_id` / `turn_id`;
- presencia o ausencia de `executor.dispatch_started`;
- resultado durable si existe;
- `policy_violation`;
- `reconciliation_required`.

### Git

- repo / checkout exacto;
- branch;
- HEAD;
- `origin/main` cuando corresponda;
- `git status --short`;
- staged / unstaged / untracked;
- diff relevante;
- `git diff --check`;
- postflight/fingerprint durable cuando el problema sea de continuation.

No recopilar dumps masivos si esta evidencia ya permite decidir.

---

## 6. Dirty state y continuations

La autoridad técnica detallada está en `TROUBLESHOOTING.md`.

Reglas resumidas:

### Dirty legítimo dejado por una Task anterior

Si una Task `AUTONOMOUS_WRITE` terminó correctamente y su postflight durable captura el working tree dirty, una continuation normal debe poder reconocer ese estado mediante el fingerprint durable.

No limpiar ni commitear sólo para habilitar la continuation.

### Cambio externo

Si el working tree cambió fuera de la cadena esperada, D3 debe fallar cerrado.

Si se quiere conservar deliberadamente ese estado externo, evaluar **Direct Baseline Adoption** según el procedimiento vigente.

### Reconciliación rechazada

Si una herramienta nativa de reconciliación/adopción responde que el caso no es elegible o rechaza el estado:

> **STOP. No inventar un bypass automáticamente.**

Primero clasificar por qué fue rechazado. Si conviene rehacer el trabajo desde un baseline limpio, hacerlo en un checkout/worktree separado preservando el intento original hasta que la nueva evidencia cierre el caso.

Un mecanismo de recuperación fallido no autoriza resetear o borrar el estado anterior.

---

## 7. READ_ONLY y auditorías

`READ_ONLY` puede fallar en Windows cuando Codex necesita shell, temporales, SQLite o alguna operación que genere approval.

No convertir esa limitación en una investigación lateral del proyecto consumidor.

Cuando sea necesario, puede usarse `AUTONOMOUS_WRITE` únicamente como **transporte técnico** para una auditoría de sólo lectura, con restricciones explícitas de cero escrituras sobre el repo y verificación postflight.

La capacidad técnica de escribir no equivale a autorización para escribir.

---

## 8. Principal y Secundario

Bridge e identidad de repo son conceptos distintos.

- El Bridge es una instancia de ejecución.
- El Project determina el checkout/repositorio.
- Sol elige normalmente Bridge, Project, modelo y modo.
- Marcos no debería tener que especificarlo en cada tarea.

Una Task iniciada en un Bridge debe continuar allí cuando dependa de estado, Project, thread, turn o working tree.

Si un Bridge falla:

1. un intento normal;
2. como máximo una comprobación focal útil;
3. si parece problema de infraestructura, STOP y avisar.

No migrar silenciosamente al otro Bridge.

El otro Bridge puede usarse para una tarea distinta e independiente si su Project/checkout están claramente separados.

---

## 9. Regla de escritor por checkout

Regla general, con o sin Bridge:

> **Un solo workstream escritor por checkout físico a la vez.**

Pueden coexistir varios hilos sobre el mismo repo si sólo uno escribe y los demás son de consulta.

Si otro hilo necesita pasar a ser escritor del mismo checkout:

```text
cerrar bloque actual
→ auditar
→ commit
→ push
→ verificar repo limpio y sincronizado
→ handoff al nuevo escritor
```

El commit puede ser un checkpoint de cambio de hilo; no implica que el tema haya terminado definitivamente.

Al volver a un hilo pausado, Sol debe hacer un preflight corto sobre el `main` vigente e incorporar el hecho de que pueden existir commits intermedios.

### Dos escritores simultáneos sobre el mismo repo

Sólo mediante checkouts/worktrees separados y una integración posterior explícita.

Si ambos escritores tocan el mismo tema o los mismos archivos, elevar el nivel de cautela: preferir un escritor principal y otro hilo investigador/auditor salvo que exista una separación clara y una estrategia de integración.

---

## 10. Repos distintos y paralelismo

Dos temas pueden escribir en paralelo cuando trabajan sobre repositorios/checkouts distintos.

Ejemplo conceptual:

```text
Hilo A → repo de conocimiento
Hilo B → repo técnico
```

No hace falta cerrar un commit en A sólo porque B vaya a escribir en otro repo.

La unidad de exclusión práctica es el checkout compartido, no el nombre del Bridge ni el nombre del hilo.

---

## 11. Commit y push

### Commit

Un commit requiere:

- bloque lógico suficientemente cerrado;
- diff revisado;
- pruebas/evidencia apropiadas;
- auditoría aprobada;
- autorización humana expresa.

Cuando el repo se gestiona mediante D3 y aplica el contrato vigente, usar `commit_checkpoint` en lugar de `git commit` directo desde Codex.

### Push

Push es una operación separada.

Requiere autorización humana expresa.

En el contrato actual, Codex no debe hacer `git push` directamente si la política del Bridge lo bloquea. El patrón aceptado es preparar el comando normal para Marcos y verificar después que local, `origin/main` y GitHub coinciden.

### Cuándo alcanza un commit local

Si el mismo hilo/workstream seguirá siendo el único escritor del repo, un commit local puede funcionar como checkpoint intermedio.

### Cuándo se requiere commit + push

Para entregar el repo a otro hilo escritor sobre el mismo checkout, el cierre normal es:

```text
auditoría → commit → push → verificación → working tree limpio
```

---

## 11 bis. Control humano obligatorio de Git — política transversal

**Marcos decide exclusivamente si y cuándo registrar y publicar cambios**. Rige para ChatGPT/Sol, Codex/Luna/Terra, ambos Bridges, otros ejecutores y proyectos consumidores.

- Una orden de implementar, finalizar una Task, aprobar una auditoría, necesitar handoff o dejar un dirty state legítimo NO autoriza commit, commit_checkpoint ni push.
- Antes de cada commit hay que solicitar autorización humana previa, expresa y específica por repositorio y operación, informando branch, HEAD/base, paths, diff, pruebas y auditoría, mensaje propuesto y si será necesario publicar antes de cambiar de escritor.
- La autorización de commit NO autoriza push; una autorización de push NO permite commits nuevos. Solicitar autorización de publicación por separado.
- Si corresponde commit, en repos gestionados por D3 usar commit_checkpoint tras autorización y cumpliendo el contrato vigente. No intentar eludir las restricciones de push del Bridge: usar el procedimiento manual permitido.
- Antes de transferir un checkout a otro hilo o Bridge, advertir de antemano si hace falta sincronizar Git, y detenerse si depende de una operación Git no autorizada. Preservar worktree y evidencia; no recurrir a reset, clean, stash, merge, rebase ni force push por conveniencia.
- No producir checkpoints preventivos por cuenta de Sol o Codex. Explicar las consecuencias de posponer el commit/push sin presionar ni silenciar que hay cambios sin publicar.
- Esta política documental orienta a los agentes; NO prueba que el runtime compruebe técnicamente la identidad de quien autoriza. Cualquier garantía de enforcement requiere auditoría específica.

Esta sección prevalece sobre descripciones operativas de commit o handoff que parezcan automáticas: la decisión final sobre momento y alcance es siempre de Marcos.

---
## 12. Operaciones sensibles

Una tarea normal NO autoriza automáticamente:

- commit;
- push;
- tag;
- release;
- merge;
- rebase;
- reset destructivo;
- clean;
- force push;
- eliminación de datos;
- instalación/desinstalación;
- cambios globales sensibles;
- modificación del ChatGPT–OpenCode Bridge existente.

Estas operaciones requieren autorización expresa cuando corresponda.

El ChatGPT–OpenCode Bridge v0.4.2 permanece completamente independiente y protegido.

---

## 13. Diferenciar fallo de proyecto y fallo de infraestructura

Antes de modificar código del proyecto consumidor, determinar dónde falló el flujo.

Ejemplos:

- preflight Git rechazó antes de dispatch → problema de baseline/continuidad, no del código del proyecto;
- `executor.dispatch_started` ausente → Codex no recibió la Task;
- app-server timeout después de postflight → puede haber trabajo válido preservado aunque la Task figure FAILED;
- test funcional falla con Bridge sano → problema del proyecto;
- READ_ONLY pide approval → limitación conocida de entorno/política, no prueba de defecto del proyecto;
- Bridge no abre terminal o falla lifecycle → no reparar Bridge desde el proyecto consumidor.

Si el incidente pertenece al Bridge, volver a este proyecto y tratarlo aquí.

---

## 14. Recuperar versus rehacer

No todo intento parcial merece ser reconciliado indefinidamente.

Preferir reconstrucción controlada desde un baseline limpio cuando:

- la recuperación introduce dudas nuevas;
- el mecanismo nativo de reconciliación no aplica;
- la corrida recuperada produce efectos inesperados que no existían en el baseline;
- comprobar la procedencia del estado cuesta más que repetir una prueba pequeña y determinista.

En ese caso:

1. preservar el checkout fallido;
2. crear worktree/checkout separado desde el último commit bueno;
3. reproducir baseline;
4. aplicar sólo el cambio bajo prueba;
5. comparar antes/después bajo las mismas condiciones;
6. recién después decidir qué hacer con el intento viejo.

No volver “desde cero” a todo el proyecto si existe un baseline bueno más cercano.

---

## 15. Formato de respuesta del auditor

Cuando Marcos pega un informe de Codex/OpenCode o un incidente relevante, responder con:

1. **EVALUACIÓN**
   - APROBADO
   - APROBADO CON OBSERVACIONES
   - REQUIERE CORRECCIÓN
   - RECHAZADO
2. **VERIFICADO**
3. **NO VERIFICADO**
4. **PROBLEMAS O RIESGOS**
5. **DECISIÓN**
6. **PRÓXIMO PROMPT**, si otro ejecutor debe actuar.

No considerar demostrado algo únicamente porque el ejecutor dice que funciona.

Exigir evidencia proporcional: comandos, salidas, tests, hashes, Git, postflight, journal o reproducción real según el caso.

---

## 16. Hilo dedicado de auditoría

Se recomienda mantener un hilo de ChatGPT dentro del proyecto **ChatGPT–Codex Bridge** dedicado a este modo.

Ese hilo puede recibir incidentes provenientes de cualquier proyecto:

> “En el proyecto X pasó esto con el Principal…”

> “El Secundario dejó un checkout dirty…”

> “No sé si corresponde commit, push o seguir…”

> “La reconciliación fue rechazada…”

El hilo auditor no necesita convertirse en el hilo funcional del proyecto afectado. Puede inspeccionar Bridge/GitHub cuando corresponda, emitir el diagnóstico y devolver un prompt exacto para el hilo original.

Esto reduce el riesgo de reparar infraestructura dentro de un proyecto de negocio y conserva un lugar único para aprender de incidentes reutilizables.

---

## 17. Prompt canónico para iniciar el hilo auditor

```text
Este hilo queda dedicado al MODO AUDITOR / ASESOR del ChatGPT–Codex Bridge.

Lo voy a usar para traer problemas o dudas operativas de cualquier proyecto que use los Bridges o Git.

Tu función es:
- auditar incidentes de Puente Principal / Secundario;
- determinar cómo recuperar tareas, continuations o working trees sin perder trabajo;
- asesorarme sobre Git, commit, push, ramas, worktrees y handoff entre hilos;
- distinguir problemas del proyecto de problemas de infraestructura;
- preservar el estado antes de proponer acciones destructivas;
- indicarme cuándo corresponde STOP;
- darme el prompt exacto para devolver al hilo del proyecto afectado.

No desarrolles funcionalidad del proyecto consumidor salvo que sea estrictamente necesaria para auditar el incidente.

Usá como autoridad el repositorio ChatGPT-Codex-Bridge, especialmente:
- AUDITOR_MODE.md;
- TROUBLESHOOTING.md;
- SECURITY.md;
- ARCHITECTURE.md;
- DECISIONS.md;
- DUAL_BRIDGE_CONCURRENCY_MVP_2026-10-07.md.

Cuando te pegue un informe de otro hilo, respondé con:
EVALUACIÓN / VERIFICADO / NO VERIFICADO / PROBLEMAS O RIESGOS / DECISIÓN / PRÓXIMO PROMPT.

No asumas que el usuario debe elegir Bridge o recordar cuándo hacer commit/push: detectalo y avisame.
```

---

## 18. Relación con otros documentos

- `TROUBLESHOOTING.md`: autoridad para incidentes conocidos y procedimientos técnicos ya verificados.
- `SECURITY.md`: autoridad de seguridad, límites y operaciones sensibles.
- `ARCHITECTURE.md`: autoridad estructural del Bridge.
- `DECISIONS.md`: decisiones técnicas adoptadas.
- `DUAL_BRIDGE_CONCURRENCY_MVP_2026-10-07.md`: autoridad histórica/operativa para dos instancias concurrentes.
- `DIDACTIC_MODE.md`: modo independiente; no debe mezclarse con este.

El Modo Auditor / Asesor no reemplaza esos documentos. Los organiza como un flujo de decisión orientado a Marcos y reutilizable entre proyectos.
