# Operación del worker de correos de registro

El endpoint público guarda el desafío y su trabajo de correo en `registration_email_jobs` dentro de la misma transacción. El código queda cifrado en reposo y SMTP se ejecuta exclusivamente fuera de la solicitud HTTP.

## Ejecución

- Un lote controlado: `flask --app run.py process-registration-emails`
- Worker continuo: `flask --app run.py run-registration-email-worker`

En un contenedor dedicado configure `J2M_PROCESS_TYPE=registration-worker`. El
worker no publica HTTP: no configure `healthcheckPath`, no asigne dominio y use
reinicio `ON_FAILURE`. El `railway.toml` compartido omite expresamente la ruta
HTTP; `/api/ready` se configura únicamente en el servicio web. La imagen comprobará que PID 1 ejecuta el
comando esperado cuando el runtime respete el `HEALTHCHECK`; Railway reiniciará
el contenedor si el comando termina con error. Supervise también la cadencia de
métricas y la antigüedad del backlog, porque liveness no detecta por sí sola un
proceso bloqueado.

El transporte en memoria de pruebas funciona sin Railway. En staging, el worker debe ejecutarse como un proceso independiente del servidor web, con la misma base PostgreSQL y las mismas variables de correo y cifrado. No se configuró ni inició ningún servicio real en esta fase.

Variables operativas:

- `REGISTRATION_EMAIL_ENCRYPTION_KEY`: secreto estable y exclusivo de al menos 32 caracteres.
- `REGISTRATION_EMAIL_POLL_SECONDS`: intervalo entre ciclos.
- `REGISTRATION_EMAIL_BATCH_SIZE`: máximo de trabajos reclamados por ciclo.
- `REGISTRATION_EMAIL_MAX_ATTEMPTS`: límite de intentos.
- `REGISTRATION_EMAIL_STALE_SECONDS`: tiempo para recuperar un reclamo interrumpido.
- `REGISTRATION_EMAIL_RETENTION_SECONDS`: retención de trabajos terminales antes de su limpieza.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` y `SMTP_USE_TLS`: transporte ya existente.

## Estados y recuperación

Los trabajos pasan por `pending`, `processing` y `completed`. Un desafío vencido, consumido, reemplazado o perteneciente a un negocio suspendido termina `cancelled`; un payload no descifrable o un trabajo que agota reintentos termina `failed`. Los errores temporales vuelven a `pending` con backoff. Un reclamo `processing` abandonado vuelve a ser elegible tras el umbral de recuperación. Al terminar en `completed`, `cancelled` o `failed`, se elimina inmediatamente el código cifrado; se conservan estado, conteo de intentos, tipo de error y fechas para auditoría operativa.

Cada reclamación genera un `lease_token` aleatorio y lo guarda mediante actualización condicional. Completar, cancelar o reprogramar exige simultáneamente el estado `processing` y el token vigente; un worker cuyo lease caducó no puede sobrescribir al nuevo propietario. La transacción de reclamación se confirma antes de SMTP y no queda ninguna transacción abierta durante la llamada externa.

SMTP no ofrece confirmación transaccional de extremo a extremo: el fencing protege el estado del outbox, pero si el proceso pierde el lease durante una llamada lenta o muere después de que el servidor acepte el mensaje, puede repetirse el mismo código. El código y su caducidad no cambian, y nunca se genera automáticamente un cliente. Esta entrega al menos una vez es una limitación pendiente de validar con el proveedor real; no se afirma entrega exactamente una vez.

Los logs y la salida CLI contienen únicamente tipos de error y conteos agregados; no incluyen códigos, destinatarios, tokens ni credenciales. Para diagnóstico, revise conteos de `pending` y `failed` mediante el comando de lote y los logs agregados. No consulte ni copie `encrypted_code`.

## Staging pendiente

Antes de habilitar el flujo en staging se debe aplicar la migración de forma controlada, configurar el secreto exclusivo, iniciar una única réplica del worker y probar reinicio, latencia y fallos con direcciones controladas. Después puede evaluarse más de una réplica sobre PostgreSQL. No se ejecutaron migraciones ni se modificaron Railway o Supabase reales.
