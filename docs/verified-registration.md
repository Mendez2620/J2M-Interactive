# Registro verificado de clientes

El alta pública crea primero un desafío temporal y solo crea el cliente después de consumir correctamente el código enviado por correo. Los códigos se almacenan únicamente como HMAC, caducan, limitan intentos y solicitudes, y no producen enlaces Wallet antes de verificar el correo.

La aceptación de Términos y Aviso de Privacidad es obligatoria, versionada e independiente del consentimiento opcional de marketing. Las cuentas históricas conservan sus datos y permanecen sin marcar como verificadas hasta completar un proceso futuro expresamente diseñado para ellas.

Cada solicitud conserva la fecha UTC de aceptación, la versión visible y un SHA-256 del documento presentado. Si cambia un documento, debe cambiar también su variable de versión. Los desafíos anteriores a esta evidencia se invalidan durante la migración y deben solicitarse nuevamente.

`REGISTRATION_IDENTITY_HMAC_KEY` es una clave dedicada, estable e independiente de `SECRET_KEY` y `JWT_SECRET_KEY`. Producción no arranca si falta, es corta o reutiliza alguna de esas claves. Rotar la clave de sesión Flask no altera las reservas de identidad; la clave de identidad no debe rotarse sin una migración explícita de sus HMAC.

Las respuestas públicas válidas usan siempre HTTP 202 y el mismo cuerpo para identidades nuevas, existentes o limitadas. La solicitud HTTP nunca llama a SMTP: el desafío y un trabajo de correo con el código cifrado se confirman en una sola transacción y un worker independiente realiza la entrega. Las identidades existentes, incluidas las cuentas históricas sin `email_verified_at`, no generan correo de registro; la interfaz remite al flujo de recuperación verificada sin confirmar que exista una cuenta.

`REGISTRATION_EMAIL_ENCRYPTION_KEY` protege temporalmente el código del outbox y debe ser estable, exclusiva y distinta de las claves de sesión, JWT e identidad. No debe rotarse mientras existan trabajos pendientes. Consulte [Operación del worker de correos de registro](registration-email-worker.md).

## Validaciones operativas pendientes

- El transporte SMTP en memoria solo se utiliza en pruebas. Antes del despliegue debe comprobarse entrega, reintentos y reputación con SMTP real. Las pruebas locales reducen diferencias previsibles, pero no constituyen una garantía absoluta contra análisis temporal en redes reales.
- La migración debe ejecutarse de forma controlada y validarse en una copia de PostgreSQL antes de Railway. No se ejecuta automáticamente ni se aplicó a Supabase durante esta fase.
- La experiencia final de correo y Wallet requiere validación manual en dispositivos físicos y con las credenciales externas del entorno de staging.
