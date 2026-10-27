# Operación del worker de Google Wallet

## Causa del backlog

Las transacciones guardan de forma atómica un trabajo en `google_wallet_sync_jobs`, pero el servicio web de Railway solo inicia Gunicorn. El comando anterior `process-google-wallet-sync` procesa un lote y termina; si ningún servicio o tarea programada lo invoca, los trabajos permanecen en `pending` con `attempts = 0`.

Esta conclusión describe el flujo del repositorio. La configuración real de Railway debe comprobarse en su panel antes del despliegue.

## Servicio independiente en Railway

Crear un segundo servicio desde el mismo repositorio y la misma imagen Docker, separado del servicio web. Configurarlo con:

- Comando de inicio: `flask --app run.py run-google-wallet-sync-worker`
- Las mismas variables `DATABASE_URL`, `DATABASE_SSLMODE`, `SECRET_KEY`, `JWT_SECRET_KEY`, `PUBLIC_BASE_URL`, `GOOGLE_WALLET_MODE`, `GOOGLE_ISSUER_ID` y `GOOGLE_SERVICE_ACCOUNT_JSON` que el servicio web.
- `GOOGLE_WALLET_SYNC_POLL_SECONDS=10`
- `GOOGLE_WALLET_SYNC_BATCH_SIZE=50`
- `GOOGLE_WALLET_SYNC_ALERT_PENDING=100`
- `GOOGLE_WALLET_SYNC_ALERT_STALE_SECONDS=300`
- Política de reinicio ante fallo. El worker no publica un puerto HTTP y no necesita un dominio.
- `J2M_PROCESS_TYPE=google-wallet-worker`.
- Sin `healthcheckPath` HTTP en el servicio Railway. El `railway.toml`
  compartido omite la ruta para que ningún worker la herede; `/api/ready` se
  configura solo en el servicio web. La imagen valida que PID 1 ejecute el
  worker y la política `ON_FAILURE` cubre la terminación del proceso.

Una réplica es suficiente inicialmente. Varias réplicas son compatibles: cada actualización queda cercada por un advisory lock transaccional de PostgreSQL por cliente. Un trabajo abandonado en `processing` vuelve a ser elegible después de cinco minutos. Los fallos temporales usan reintentos con espera exponencial; los fallos permanentes quedan en `failed`. La llamada externa tiene timeout y nunca bloquea la transacción de puntos o sellos.

## Métricas y alertas

Cada ciclo registra solo agregados: procesados, pendientes, procesando, reintentando, completados, fallidos, antigüedad del trabajo más antiguo y fechas de último intento/éxito. No se registran tokens QR, datos de clientes ni credenciales.

Crear alertas sobre los eventos `google_wallet_worker_backlog` y `google_wallet_worker_cycle_failed`. Investigar además cualquier negocio que muestre `con error` o `reintentando` en Super Admin. Códigos 429, 5xx y errores de red son reintentables; 401/403 normalmente requieren corregir credenciales o permisos. Un objeto 404 se recupera mediante la creación segura ya implementada.

El healthcheck de proceso no demuestra progreso. Alerte también por ausencia de
ciclos y por antigüedad creciente del trabajo más antiguo para detectar un
worker vivo pero bloqueado.

## Recuperación controlada del backlog existente

1. Verificar que las migraciones estén aplicadas y tomar un respaldo administrado de PostgreSQL.
2. Confirmar las credenciales de Google Wallet sin imprimirlas y mantener el servicio web activo.
3. Desplegar una sola réplica del worker y observar métricas agregadas.
4. Permitir que el worker drene el backlog. Los trabajos del mismo cliente se agrupan y Google recibe el saldo actual, no una suma ni un saldo histórico.
5. Verificar que `pending` llegue a cero o que los restantes tengan un motivo de reintento/fallo identificable.
6. Comprobar manualmente una tarjeta existente. No volver a ejecutar el ledger, no borrar trabajos, no modificar saldos y no reemitir tarjetas.

El comando de un solo lote continúa disponible para diagnóstico controlado, pero no sustituye al worker permanente.

## Verificación real

En un entorno autorizado con credenciales reales: partir de una tarjeta que muestre 0/8, acreditar un sello desde el escáner web, comprobar una sola transacción y una sola entrada contable, esperar a que el trabajo quede `completed` y confirmar que la misma tarjeta muestra 1/8. Repetir el mismo request id debe conservar un único efecto. Esta comprobación externa no se considera realizada por las pruebas automatizadas del repositorio.
