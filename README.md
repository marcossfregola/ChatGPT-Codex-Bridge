# ChatGPT–Codex Bridge

ChatGPT–Codex Bridge es un proyecto local para coordinar tareas técnicas entre ChatGPT, un Bridge de coordinación y un executor Codex local.

## Flujo objetivo

```text
ChatGPT → Bridge → Codex local → repositorio → evidencia → Bridge → ChatGPT
```

Estado actual: bootstrap formal y pre-implementación.

El primer consumidor previsto es el Orquestador ComfyUI.

El ChatGPT–OpenCode Bridge existente es un sistema independiente y protegido; este repositorio no reutiliza ni modifica su estado, procesos, túneles o MCP.
