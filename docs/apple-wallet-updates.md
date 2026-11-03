# Apple Wallet: PassKit, APNs y publicación controlada

La revisión `9d0e1f2a3b4c` añade credenciales PassKit separadas del QR de caja, marcadores monotónicos firmados, registros de entrega del servidor y una cola persistente de notificaciones APNs. Los pases conservan `passTypeIdentifier`, `serialNumber`, barcode, puntos y sellos. Una notificación APNs solo indica a Wallet que consulte el servicio; no demuestra instalación ni descarga. `last_served_at` y `last_served_update_tag` registran únicamente que el servidor entregó esa versión del pase, sin atribuirla a todos los dispositivos.

Los pases nuevos reciben un `authenticationToken` HMAC de alta entropía derivado mediante `APPLE_PASS_AUTH_KEYS`. El primer valor firma nuevas credenciales y los valores antiguos se conservan durante una rotación controlada. Nunca se usa el QR, teléfono, ID de cliente, cookie o token Wallet temporal. Los pases históricos emitidos con el QR no se migran silenciosamente: sus tokens antiguos no autorizan el nuevo servicio. Deben reemitirse en una transición autorizada; no se afirma que puedan actualizarse automáticamente.

> **Estado de despliegue (2026-11-03):** las migraciones hasta `9d0e1f2a3b4c` y el backend de 8C.3C2 ya están en staging. `APPLE_WALLET_MODE=disabled` se fijó explícitamente; NO hay certificados oficiales ni worker APNs dedicado. Antes de cambiarlo a `real`, consultar la titularidad y obligaciones multiempresa en [8D.1A — Modelo de emisor y activación](apple-wallet-issuer-readiness.md). Los requisitos del acuerdo de Apple prevalecen sobre cualquier ejemplo técnico de esta guía.

## Configuración de staging

- `APPLE_WALLET_MODE=real` (use `disabled` hasta completar toda la configuración; `mock` solo en desarrollo/pruebas).
- `APPLE_PASS_TYPE_ID` y `APPLE_TEAM_ID`, coincidentes con el certificado.
- `APPLE_CERT_PATH`, `APPLE_KEY_PATH` y `APPLE_WWDR_PATH`, montados desde almacenamiento secreto persistente y nunca incluidos en la imagen o el repositorio.
- `APPLE_KEY_PASSWORD`, si la clave privada está cifrada.
- `APPLE_PASS_AUTH_KEYS`, anillo de claves aleatorias de al menos 32 bytes, separado por comas y gestionado como secreto.
- `APPLE_WEB_SERVICE_URL`, URL HTTPS pública absoluta que termina en el servicio PassKit.
- `APPLE_APNS_HOST=https://api.push.apple.com` (sandbox únicamente durante pruebas autorizadas con cuentas de desarrollo).
- `APPLE_WALLET_BATCH_SIZE`, `APPLE_WALLET_POLL_SECONDS` y `APPLE_APNS_TIMEOUT_SECONDS`.

Ejecute un servicio dedicado con `J2M_PROCESS_TYPE=apple-wallet-worker` y `flask --app run.py run-apple-wallet-worker`. No necesita healthcheck HTTP; el healthcheck del contenedor valida el comando del proceso. Web y worker no ejecutan migraciones automáticamente.

La publicación desde Super Admin es independiente de Guardar. Crea un lote auditado y trabajos por dispositivo; cancelación, leases recuperables, seis intentos, backoff e invalidación APNs 410 son persistentes. Las excepciones operativas liberan el lease mediante fencing y se reintentan de forma acotada; si Apple Wallet queda temporalmente `disabled`, los trabajos se pausan sin consumir intentos. El worker no mantiene una transacción ORM abierta durante HTTP. El pase se genera al solicitarlo usando siempre el diseño y saldo vigentes, por lo que una notificación antigua no puede reconstruir un diseño anterior.

## Activación posterior (NO repetir migración)

La migración `9d0e1f2a3b4c` **ya se aplicó y verificó en staging**. No repetirla ni bajar versiones. Para activar Apple Wallet primero resolver el emisor autorizado y los datos de soporte exigidos en [8D.1A](apple-wallet-issuer-readiness.md); luego preparar certificados, claves y secretos fuera del repositorio; ejecutar el preflight de solo lectura; crear el worker Apple dedicado y realizar un ensayo con un solo pase autorizado antes de activar una publicación. Mantener `APPLE_WALLET_MODE=disabled` hasta una aprobación explícita de la activación real.

Sin Apple Developer, certificados válidos, HTTPS público, iPhone físico y un pase real instalado solo pueden validarse estructura, firma con certificados de prueba y APNs simulado. La integración física permanece pendiente hasta una prueba controlada; CI nunca contacta Apple.
