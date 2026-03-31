# AGENTS.md — CER Bond Analyzer

Guía para agentes de IA trabajando en este repositorio.

## Proyecto

**CER Bond Analyzer** — Dashboard profesional de análisis de bonos argentinos CER.
Desarrollado para el trading desk de Banco de Córdoba S.A.

- **Stack**: HTML/JS vanilla + Python (`server.py`)
- **Deploy**: Render (free tier), deploy automático desde GitHub
- **Precios**: Google Sheets via service account (`GOOGLE_CREDS_JSON` en Render)
- **Repo**: github.com/maximodubois/cer-bond-analyzer

## Skills disponibles

Antes de ejecutar cualquier tarea, seleccioná el skill correcto:

| Skill | Cuándo usarlo |
|-------|--------------|
| `ceo` | Planificar features complejos o cambios multi-componente |
| `programador` | Escribir, corregir u optimizar código |
| `matematico` | Validar fórmulas, metodología cuantitativa, cálculos financieros |
| `economista` | Validar conceptos de mercado, interpretación financiera, RV |

## Reglas generales

- **Nunca modificar** sin entender las dependencias de la función tocada
- **Siempre XIRR** sobre fórmulas bullet para calcular TIR
- **Siempre lag CER** (convención 16→15 días) en todo cálculo que involucre CER
- **Bounds check obligatorio** en cualquier loop sobre fechas/meses
- **`lp.last`** es el campo exclusivo para LAST — no mezclar con bid/offer
- El archivo `index.html` es monolítico — no separar en múltiples archivos

## Convenciones de código

```js
// Agregar bono CER:
{ ticker: "TXXX", mat: "YYYY-MM-DD", vno: 100, emDate: "YYYY-MM-DD", cerEm: XXXXX, color: "#HEX" }

// Agregar LECAP/BONCAP:
{ ticker: "SXXXXX", mat: "YYYY-MM-DD", vno: 100, tna: 0.XX, color: "#HEX" }
```

## Errores críticos resueltos (no reintroducir)

| ID | Descripción | Fix |
|----|------------|-----|
| TZXM6 | TIR con fórmula bullet → resultado absurdo | Usar XIRR por cashflows |
| X29Y6 | Loop sin bounds check → ejecución sin fin | Guard antes del loop |

## Variables de entorno (Render)

- `GOOGLE_CREDS_JSON` → credenciales service account Google Sheets (no commitear)

## Flujo de deploy

1. Commit y push a `main` en GitHub
2. Render detecta el push y redeploya automáticamente
3. Sin build steps — deploy directo del repo
