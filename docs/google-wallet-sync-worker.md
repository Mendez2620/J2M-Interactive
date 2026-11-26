# Operación del worker de Google Wallet

## Worker permanente

El stack local define el worker `google-wallet-worker` en `compose.selfhost.yml`.
En modo real, el trabajo de outbox se confirma en la misma transacción que la compra o el canje.
El fallo del proveedor no revierte esa operación contable.

Crear un segundo servicio desde el mismo repositorio y la misma imagen Docker, separado del servicio web. Configurarlo con:

- Comando de inicio: `flask --app run.py run-google-wallet-sync-worker`
- Las mismas variables `DATABASE_URL`, `DATABASE_SSLMODE`, `SECRET_KEY`, `JWT_SECRET_KEY`, `PUBLIC_BASE_URL`, `GOOGLE_WALLET_MODE`, `GOOGLE_ISSUER_ID` y `GOOGLE_SERVICE_ACCOUNT_JSON` que el servicio web.
- `GOOGLE_WALLET_SYNC_POLL_SECONDS=10`
- `GOOGLE_WALLET_SYNC_BATCH_SIZE=50`
- `GOOGLE_WALLET_SYNC_ALERT_PENDING=100`
- `GOOGLE_WALLET_SYNC_ALERT_STALE_SECONDS=300`
- Política de reinicio ante fallo. El worker no publica un puerto HTTP y no necesita un dominio.
- `J2M_PROCESS_TYPE=google-wallet-worker`.
- El worker usa la comprobación de proceso del contenedor. `/api/ready`
  corresponde únicamente al servicio web.

Cada reclamación local usa un token aleatorio. El worker procesa exclusivamente
las filas devueltas por el UPDATE condicional y acepta el resultado solo si
el token todavía coincide. El advisory lock PostgreSQL es de sesión. La
reserva persistente del objeto protege la escritura incluso si PostgreSQL
pierde esa sesión. Recuperar un trabajo local abandonado después de cinco
minutos no elimina una reserva remota incierta.

La migración `c3d4e5f6a7b8` agrega el token de reclamación. Durante una actualización
o restauración mantenga Google Wallet deshabilitado hasta verificar el estado
del proveedor, las reservas recientes, la afinidad de sesión PostgreSQL y
la ausencia de escritores con versiones anteriores. Un resultado local
completado registra cobertura del movimiento, sin acreditar la instalación
de la tarjeta o el momento en que un dispositivo recibe el cambio.

## Métricas y alertas

Cada ciclo registra solo agregados: procesados, pendientes, procesando, reintentando, completados, fallidos, antigüedad del trabajo más antiguo y fechas de último intento/éxito. No se registran tokens QR, datos de clientes ni credenciales.

Crear alertas sobre los eventos `google_wallet_worker_backlog` y `google_wallet_worker_cycle_failed`. El contador `uncertain_objects` muestra escrituras iniciadas o inciertas con reserva activa. El outbox aplica espera exponencial acotada. Un reintento local frente a 429, 5xx o pérdida de respuesta no permite reenviar una escritura mientras la reserva siga incierta. Los 401/403 normalmente requieren corregir credenciales o permisos. Un objeto 404 se recupera mediante la creación segura ya implementada.

Antes de una llamada al proveedor, el worker verifica que cada trabajo reclamado, su transacción y su cliente correspondan al mismo negocio y cliente. Los vínculos inconsistentes se marcan como fallidos sin llamada remota atribuible a esos trabajos; los trabajos válidos del mismo lote conservan su procesamiento normal. El evento agregado `google_wallet_sync_invalid_linkage` permite investigar inconsistencias sin registrar identificadores ni datos privados. Esta comprobación no repara ni elimina movimientos y no sustituye la verificación contable de reconciliación.

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
