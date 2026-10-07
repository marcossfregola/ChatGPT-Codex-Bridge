# MVP de concurrencia de dos Puentes — plan, decisiones y handoff

**Fecha:** 2026-10-07  
**Proyecto:** ChatGPT–Codex Bridge  
**Estado:** **GO para Etapa 1 — relevamiento READ_ONLY.**  
**Todavía NO hay GO para crear clones ni implementar cambios.**

Este documento concentra el contexto y las decisiones tomadas para habilitar el uso concurrente del **Puente Principal** y el **Puente Secundario** sin convertir el Bridge en una infraestructura compleja ni obligar al usuario a administrar Git cotidianamente.

Debe usarse como handoff para el próximo hilo antes de iniciar cualquier implementación.

---

## 1. Objetivo

Poder usar simultáneamente dos instancias independientes del ChatGPT–Codex Bridge sobre workstreams distintos de un mismo proyecto, especialmente Saniferr.

Ejemplo normal:

```text
Puente Principal   → Base Maestra
Puente Secundario  → Gastos
```

El usuario evitará deliberadamente usar ambos Puentes al mismo tiempo sobre el mismo tema, pero la arquitectura igualmente debe impedir pisadas silenciosas si por accidente ambos terminan tocando un archivo, documento o destino compartido.

El objetivo NO es construir una plataforma general de concurrencia, CI/CD, scheduler, gestor de branches o sistema de locks.

---

## 2. Nombres y uso operativo

Convención vigente:

- **Puente Principal** = instancia lógica principal.
- **Puente Secundario** = segunda instancia independiente.
- Si el usuario no especifica otra cosa, se usa el Puente Principal.
- Si un workstream ya empezó en una instancia, debe continuar preferentemente en esa misma instancia.
- Los modelos Luna/Terra/Sol son independientes de la identidad del Puente.

Ambos Puentes ya fueron validados operativamente en paralelo en notebook, con runtime, DB, worker, túnel y CODEX_HOME independientes. Esa validación demuestra que existen dos slots de ejecución reales. Los identificadores operativos sensibles o detalles locales de túnel no deben incorporarse a GitHub innecesariamente; el estado vivo debe verificarse con las herramientas del Bridge cuando haga falta.

El ChatGPT–OpenCode Bridge existente sigue siendo infraestructura independiente y protegida. No modificarlo, moverlo, detenerlo, reconfigurarlo ni reutilizarlo destructivamente.

---

## 3. Criterio rector de producto

La arquitectura nueva sólo sirve si usar dos Puentes sigue siendo simple.

La interacción cotidiana esperada debe seguir siendo algo como:

> “Principal: Base Maestra.”  
> “Secundario: Gastos.”

El usuario NO debería tener que pensar normalmente en:

- clones;
- branches;
- worktrees;
- staging;
- merges;
- sincronización;
- carpetas temporales;
- comandos Git complejos.

Se acepta que, en situaciones excepcionales, ChatGPT entregue uno o dos comandos PowerShell seguros y específicos para copiar y pegar.

Regla:

> **Automatizar lo frecuente; comando manual simple para lo excepcional; intervención humana sólo ante conflicto real o riesgo de pérdida.**

Criterio de descarte:

> Si una mejora agrega más que unos pocos segundos habituales o exige intervención manual frecuente al inicio/final de cada tarea, debe justificar muchísimo valor para quedarse.

---

## 4. Reversibilidad obligatoria

La versión actualmente operativa de los Puentes debe permanecer como baseline recuperable durante todo el proyecto.

No aceptar una estrategia que obligue a completar todas las etapas para volver a usar los Puentes.

Cada etapa debe ser:

- incremental;
- verificable;
- reversible;
- conservable o descartable de forma independiente cuando tenga sentido.

Antes de cualquier modificación operativa debe quedar definido:

1. estado conocido bueno anterior;
2. qué se modifica;
3. qué NO se modifica;
4. cómo se prueba;
5. cómo se vuelve atrás si falla.

No realizar migraciones destructivas.

Cuando una etapa toque configuración/runtime de los Puentes, preferir validar primero sobre una instancia o entorno aislado antes de comprometer ambas.

No es obligatorio llegar al 100 % de automatización. Una versión intermedia estable puede declararse solución definitiva si mejora claramente el uso real, aunque casos excepcionales sigan requiriendo PowerShell o intervención humana.

Criterio de freno:

> Si continuar una mejora compromete la posibilidad de volver rápidamente al estado operativo anterior, detenerse y rediseñar.

---

## 5. Arquitectura provisional preferida

La hipótesis preferida, pendiente de confirmación por relevamiento, es:

```text
GitHub / origin/main
       memoria durable y curada
          ↑               ↑
          │               │
clon permanente      clon permanente
Principal             Secundario
     ↑                    ↑
Puente Principal     Puente Secundario
```

Para Saniferr se prefieren provisionalmente **dos clones permanentes** por sobre `git worktree`, por claridad operativa y aislamiento más fácil de entender y diagnosticar.

NO crear un clone/worktree por tarea.

NO crear branches manuales por conversación.

NO duplicar todo Saniferr.

NO implementar todavía un sistema general de workspaces en el Bridge si dos `Project.repo_path` independientes resultan suficientes.

---

## 6. Regla de uso simultáneo

Los Puentes se usarán normalmente sobre workstreams distintos.

Ejemplo:

```text
Principal  → Base Maestra
Secundario → Gastos
```

El usuario tomará el recaudo de no iniciar deliberadamente ambos Puentes sobre el mismo tema al mismo tiempo.

Esta regla reduce mucho el riesgo, pero no reemplaza el aislamiento, porque dos workstreams distintos pueden tocar accidentalmente:

- README;
- STATUS/temas abiertos;
- checklist;
- catálogos;
- criterios metodológicos;
- scripts comunes;
- SQLite;
- temporales;
- outputs con nombre fijo.

La arquitectura debe detectar o aislar esas colisiones reales sin construir locks generales.

---

## 7. GitHub y comienzo de workstreams

GitHub / `origin/main` sigue siendo la memoria durable y curada.

Antes de iniciar un **workstream nuevo** que dependa de criterios/documentación del repo debe hacerse un preflight corto.

Objetivo del preflight:

- confirmar workspace/clon correcto;
- branch;
- HEAD;
- estado del worktree;
- relación básica con `origin/main`;
- registrar el SHA inicial.

Esto debe hacerse al comienzo de un workstream nuevo, **no en cada turno** de una tarea ya iniciada.

Una tarea que comienza desde SHA X puede continuar desde X aunque `main` avance durante su ejecución.

No sincronizar continuamente una tarea en curso.

---

## 8. Sincronización Git y problema actual de HEAD

El Bridge actual considera policy violation cualquier cambio de HEAD dentro de una tarea `AUTONOMOUS_WRITE`.

Ya se observó un caso real donde una actualización técnicamente válida:

```text
git pull --ff-only origin main
```

dejó el repositorio correctamente alineado y limpio, pero la tarea terminó administrativamente como `PolicyViolationError: HEAD changed`.

Por lo tanto:

- una tarea genérica `AUTONOMOUS_WRITE` no debe improvisar `pull`, merge, rebase, reset, clean, stash ni otras maniobras para actualizarse;
- no debilitar la protección genérica de HEAD;
- si la sincronización es trivial y segura puede resolverse más adelante mediante una operación específica del Bridge;
- para el MVP también es aceptable que ChatGPT dé un comando PowerShell puntual de fast-forward-only cuando corresponda.

No documentar como principio permanente que “el Bridge no puede cambiar HEAD”. La situación correcta es: **la tarea genérica actual no está autorizada a hacerlo; una operación Git especializada podría existir en el futuro si demuestra ser necesaria y segura.**

---

## 9. Cierre de un workstream

Contrato conceptual mínimo:

```text
SHA inicial
→ cambios propios
→ main vigente
→ validación
→ integración simple o STOP
```

Cada workstream debe conservar como mínimo:

- Puente usado;
- clon/workspace usado;
- SHA inicial;
- archivos modificados.

No permitir que Codex elija espontáneamente entre:

- merge;
- rebase;
- cherry-pick;
- reset;
- stash;
- clean;

para resolver la integración.

Regla:

- caso trivial → procedimiento simple;
- conflicto textual → STOP;
- divergencia → STOP;
- situación Git no clara → STOP;
- conflicto semántico → revisión humana/Sol.

Para excepciones poco frecuentes es aceptable resolver con comandos PowerShell puntuales preparados según el estado real de ese momento.

No construir desde el día uno un motor capaz de resolver todos los estados Git.

---

## 10. Documentos transversales / hotspots

Git puede fusionar dos cambios textualmente compatibles y aun así producir incoherencia semántica.

Regla simple para el MVP:

> Si un documento canónico cambió en `main` desde el SHA inicial del workstream **y** el workstream también lo modificó, debe revisarse antes de publicar aunque Git no reporte conflicto textual.

Ejemplos en Saniferr:

- `TEMAS_ABIERTOS.md`;
- `CHECKLIST_MENSUAL_FUENTES.md`;
- `CATALOGO_ANALISIS_VALIDADOS.md`;
- `CATALOGO_HERRAMIENTAS_CODEX.md`;
- README;
- documentos generales de arquitectura/metodología.

No crear infraestructura especial de locks para esto.

---

## 11. Fuera de Git: frontera provisional

Dos clones resuelven el problema del checkout Git, pero no necesariamente toda la concurrencia local.

Criterio provisional:

```text
fuentes originales
→ compartidas

herramientas estables homologadas
→ compartidas

temporales / staging / outputs intermedios
→ aislar sólo si pueden colisionar

código reusable en desarrollo
→ evitar edición concurrente

recursos externos con estado mutable
→ inspeccionar y aislar sólo donde haga falta
```

No duplicar `01_fuentes`, snapshots Nativo, bases pesadas ni todo `C:\Codex Estadisticas Saniferr`.

El relevamiento debe buscar no sólo rutas compartidas sino también **destinos compartidos**:

- `latest.*`;
- SQLite abierta para escritura;
- JSON fijo;
- CSV/XLSX fijo;
- carpetas temporales fijas;
- manifests;
- caches;
- locks;
- archivos de estado;
- outputs mensuales comunes;
- configuraciones mutables compartidas.

Dos scripts pueden estar en carpetas diferentes y colisionar igualmente si escriben sobre el mismo destino.

---

## 12. Dependencias Saniferr ya verificadas

En el GitHub de Saniferr se verificaron al menos estos scripts:

- `02_codigo/relevamiento_tecnico_nativo_etapa1.py`
- `02_codigo/generar_catalogo_etapa2.py`

con construcción explícita:

```python
ROOT = Path(r"C:\Codex Estadisticas Saniferr")
REPO = ROOT / "saniferr-analisis-conocimiento"
```

Está verificada la dependencia al nombre/ruta actual del checkout.

Pendiente del relevamiento:

- determinar si esos scripts siguen siendo operativos o son históricos;
- buscar otras dependencias absolutas equivalentes;
- medir el impacto real de introducir dos clones.

La evaluación final de Sol en el proyecto Saniferr fue:

- **GO para relevamiento READ_ONLY**;
- dos clones permanentes siguen siendo la opción preferida;
- costo esperado **MEDIO-BAJO** si las colisiones reales son pocas y localizables;
- todavía NO hay GO para implementar.

---

## 13. Problema independiente: READ_ONLY y approvals

El problema del worktree compartido y el problema de approvals en `READ_ONLY` son distintos.

Código actual del Bridge:

```text
READ_ONLY
approvalPolicy = on-request
approvalsReviewer = user
sandbox = read-only
networkAccess = false
```

El cliente del Bridge rechaza cualquier server request inesperada y puede fallar con:

```text
ServerRequestError:
unexpected server request 'item/commandExecution/requestApproval';
automatic approval is disabled
```

Por lo tanto una inspección inocua puede quedar bloqueada aunque no intente escribir.

Hipótesis a evaluar en etapa separada:

```text
approvalPolicy = never
sandbox = readOnly
networkAccess = false
```

Pero no implementarla sin pruebas.

Debe demostrarse al menos:

- `git status` funciona;
- `git rev-parse HEAD` funciona;
- lectura de archivos funciona;
- comandos de inspección inocuos funcionan;
- escritura sigue bloqueada;
- red sigue bloqueada;
- no aparecen solicitudes de approval;
- el preflight Windows/Git index sigue funcionando.

---

## 14. Ejecutores disponibles

No todo debe hacerse mediante los Bridges.

Herramientas disponibles:

### ChatGPT / Sol

Usar para:

- arquitectura;
- diagnóstico;
- planificación;
- auditoría;
- decisiones;
- redacción de prompts.

### Codex local

Disponible directamente en la PC.

Preferirlo cuando resulte más simple para:

- inspección local;
- modificación del propio Bridge;
- ejecución de tests;
- comandos técnicos;
- tareas donde no conviene depender del componente que se está cambiando.

En particular, al modificar el propio Bridge, Codex local puede ser preferible para implementación/tests internos.

### Puente Principal / Secundario

Usarlos cuando se necesite:

- validar comportamiento real ChatGPT → Bridge → Codex;
- probar end-to-end;
- comprobar concurrencia real;
- aprovechar el circuito ya operativo.

La elección del ejecutor debe priorizar:

> simplicidad + seguridad + rapidez + calidad de evidencia.

No usar OpenCode ni tocar el Bridge OpenCode existente salvo decisión expresa.

---

## 15. Plan por etapas

No ejecutar todo de corrido.

Cada etapa sigue:

```text
problema
→ inspección
→ diagnóstico/diseño
→ implementación
→ pruebas
→ evidencia
→ auditoría
→ aprobación
→ commit cuando corresponda
```

No avanzar automáticamente a la siguiente etapa.

### Etapa 1 — Relevamiento real

**Complejidad esperada:** BAJA-MEDIA.

Inspección READ_ONLY de:

- checkout Saniferr;
- referencias absolutas;
- supuestos de checkout único;
- `01_fuentes`;
- `02_codigo`;
- `02_codigo\scripts`;
- `03_resultados`;
- staging;
- temporales;
- SQLite;
- manifests;
- caches;
- locks;
- outputs fijos;
- destinos compartidos;
- relación actual `Project.repo_path` del Bridge.

Objetivo: detectar únicamente qué impediría el MVP sencillo.

No implementar.

### Etapa 2 — Cerrar diseño físico

**Complejidad:** BAJA.

Con evidencia de Etapa 1 decidir:

- rutas exactas de clones;
- qué queda compartido;
- qué debe aislarse;
- si dos `Project.repo_path` distintos bastan;
- rollback.

### Etapa 3 — Resolver READ_ONLY / approvals

**Complejidad:** MEDIA.

Estudiar/corregir approvals de inspección sin debilitar el sandbox read-only.

### Etapa 4 — Crear y asignar los dos clones permanentes

**Complejidad:** BAJA.

Sólo después de aprobación.

Un clon para Principal y otro para Secundario.

Sin clones por tarea.

### Etapa 5 — Preflight de workstream

**Complejidad:** BAJA-MEDIA.

Chequeo rápido, fail-closed y de pocos segundos.

### Etapa 6 — Aislar sólo colisiones reales externas

**Complejidad:** MEDIA / VARIABLE.

Modificar únicamente destinos que el relevamiento demuestre peligrosos.

No duplicar carpetas preventivamente.

### Etapa 7 — Cierre e integración segura

**Complejidad:** MEDIA.

Formalizar caso simple vs STOP.

No automatizar estados Git raros salvo necesidad real.

### Etapa 8 — Prueba concurrente y certificación

**Complejidad:** MEDIA.

Probar Principal y Secundario simultáneamente sobre workstreams controlados, evidencia, auditoría, rollback y cierre.

---

## 16. Criterio de GO / STOP

Después de la Etapa 1 debe existir un punto de decisión fuerte.

**GO fuerte** si el resultado se parece a:

- dos clones;
- pocas referencias a adaptar;
- dos o tres destinos mutables a separar;
- preflight corto;
- sin ceremonia manual frecuente.

**STOP / rediseñar** si aparecen:

- decenas de scripts a modificar;
- reorganización de todo Saniferr;
- necesidad de duplicar fuentes/bases;
- muchos destinos globales difíciles de aislar;
- varios minutos habituales por tarea;
- administración frecuente de Git por parte del usuario;
- necesidad de construir locks/branches/orquestación compleja.

No convertir cada riesgo descubierto en una función nueva del Bridge.

---

## 17. Próximo paso autorizado

**Únicamente Etapa 1 — relevamiento READ_ONLY.**

Todavía NO está autorizado:

- crear clones;
- crear worktrees;
- crear branches;
- modificar archivos;
- modificar GitHub por implementación;
- pull/merge/rebase/reset/stash/clean;
- cambiar runtime;
- cambiar policy;
- implementar aislamiento;
- implementar integración.

Antes de ejecutar Etapa 1, decidir qué ejecutor conviene usar. Codex local es una opción especialmente válida para inspección del filesystem y del propio Bridge.

La Etapa 1 debe entregar:

1. diagnóstico ejecutivo;
2. obstáculos reales para el MVP;
3. rutas y destinos concretos que pueden colisionar;
4. dependencias del checkout único;
5. qué puede seguir compartido;
6. qué necesita aislamiento;
7. si dos `Project.repo_path` distintos bastan;
8. adaptaciones imprescindibles;
9. excepciones resolubles con PowerShell;
10. qué NO vale la pena automatizar;
11. costo final BAJO / MEDIO / ALTO;
12. recomendación GO MVP / GO CON CORRECCIONES / NO-GO;
13. evidencia concreta de comandos, archivos y salidas de inspección.

---

## 18. Regla final para el nuevo hilo

El nuevo hilo debe empezar leyendo este documento y trabajar desde aquí.

No reabrir decisiones ya cerradas salvo que la evidencia real las contradiga.

La prioridad es:

> **arquitectura preparada para crecer + MVP mínimo + uso cotidiano simple + reversibilidad permanente.**
