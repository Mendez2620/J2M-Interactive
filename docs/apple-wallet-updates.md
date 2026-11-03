# Apple Wallet: PassKit, APNs y publicación controlada

La revisión `9d0e1f2a3b4c` añade credenciales PassKit separadas del QR de caja, marcadores monotónicos firmados, registros de descarga y una cola persistente de notificaciones APNs. Los pases conservan `passTypeIdentifier`, `serialNumber`, barcode, puntos y sellos. Una notificación APNs solo indica a Wallet que consulte el servicio; no demuestra instalación ni descarga. El servidor distingue trabajos completados de `last_downloaded_version`.

Los pases nuevos reciben un `authenticationToken` HMAC de alta entropía derivado mediante `APPLE_PASS_AUTH_KEYS`. El primer valor firma nuevas credenciales y los valores antiguos se conservan durante una rotación controlada. Nunca se usa el QR, teléfono, ID de cliente, cookie o token Wallet temporal. Los pases históricos emitidos con el QR no se migran silenciosamente: sus tokens antiguos no autorizan el nuevo servicio. Deben reemitirse en una transición autorizada; no se afirma que puedan actualizarse automáticamente.

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

La publicación desde Super Admin es independiente de Guardar. Crea un lote auditado y trabajos por dispositivo; cancelación, leases recuperables, seis intentos, backoff e invalidación APNs 410 son persistentes. El worker no mantiene una transacción ORM abierta durante HTTP. El pase se genera al solicitarlo usando siempre el diseño y saldo vigentes, por lo que una notificación antigua no puede reconstruir un diseño anterior.

## Despliegue posterior

Antes de desplegar: confirmar respaldo y head `8c9d0e1f2a3b`, fijar el migrador al SHA aprobado, ejecutar una sola vez `flask --app run.py db upgrade 9d0e1f2a3b4c`, verificar FKs tenant-scoped, RLS y ausencia de grants públicos, y después desplegar web y el worker Apple. No realizar downgrade si existe historial.

Sin Apple Developer, certificados válidos, HTTPS público, iPhone físico y un pase real instalado solo pueden validarse estructura, firma con certificados de prueba y APNs simulado. La integración física permanece pendiente hasta una prueba controlada; CI nunca contacta Apple.
