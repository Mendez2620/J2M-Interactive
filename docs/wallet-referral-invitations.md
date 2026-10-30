# Invitaciones de clientes desde Wallet (8C.3B2)

La URL `/invite/<codigo>` es un identificador público de atribución de alta entropía. No es una sesión ni una prueba de identidad civil: no permite consultar saldos, movimientos, datos personales, pases, passkeys o consentimientos privados. El QR de esa página contiene `/join/<slug>?ref=<codigo>` y es independiente del QR de lealtad usado en caja.

Cada cliente tiene como máximo un `ReferralParticipant` tenant-scoped de tipo `customer_ambassador`. La revisión `6a7b8c9d0e1f` permite crear esa identidad inactiva antes del consentimiento. Una identidad inactiva no resuelve atribuciones. El consentimiento de referidos es opcional, no preseleccionado y separado del marketing. Para tarjetas anteriores, la página permite una aceptación expresa de un toque solo si nunca hubo consentimiento ni revocación. Ese contexto de Wallet es de baja confianza y no autoriza operaciones privadas ni beneficios económicos. Una revocación no puede revertirse desde el enlace público.

Las altas nuevas crean la identidad en la misma transacción que el cliente. La emisión autorizada de un pase antiguo la crea idempotentemente si falta. Las atribuciones siguen naciendo únicamente cuando el invitado completa el registro, con elegibilidad pendiente; no se generan puntos, sellos, movimientos o recompensas.

## Apple y Google Wallet

Los pases nuevos de Google incluyen `LoyaltyObject.appLinkData`; nunca se guarda una URL personal en `LoyaltyClass`. Apple añade un campo HTTPS `Invitar amigos` en `storeCard.backFields`. Se conservan los IDs, seriales, códigos de caja, saldos y módulos existentes.

Esto no demuestra que todos los pases ya instalados reciban el enlace. Google requiere publicar el cambio del objeto correspondiente. Apple requiere que el dispositivo solicite una versión nueva, normalmente después de una notificación PassKit/APNs. La publicación masiva y su observabilidad quedan coordinadas en el issue #29 y requieren autorización separada; no se fuerza reinstalación ni se duplican pases.

## Integración controlada

La migración es aditiva respecto de datos y solo relaja el check de consentimiento para identidades **inactivas**. El downgrade queda bloqueado si existen identidades inactivas sin consentimiento, porque la revisión anterior no puede representarlas sin pérdida. Para staging: verificar respaldo y head `5f6a7b8c9d0e`, fijar el migrador al SHA aprobado, ejecutar una sola vez `flask --app run.py db upgrade 6a7b8c9d0e1f`, validar el constraint/RLS/recuentos y después desplegar web y worker. No relajar `EXPECTED_ALEMBIC_HEAD` ni ejecutar migraciones desde procesos de aplicación.
