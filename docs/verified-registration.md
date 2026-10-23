# Registro verificado de clientes

El alta pública crea primero un desafío temporal y solo crea el cliente después de consumir correctamente el código enviado por correo. Los códigos se almacenan únicamente como HMAC, caducan, limitan intentos y solicitudes, y no producen enlaces Wallet antes de verificar el correo.

La aceptación de Términos y Aviso de Privacidad es obligatoria, versionada e independiente del consentimiento opcional de marketing. Las cuentas históricas conservan sus datos y permanecen sin marcar como verificadas hasta completar un proceso futuro expresamente diseñado para ellas.

## Validaciones operativas pendientes

- El transporte SMTP en memoria solo se utiliza en pruebas. Antes del despliegue debe comprobarse la entrega, latencia y reputación del SMTP real con direcciones controladas.
- La migración debe ejecutarse de forma controlada y validarse en una copia de PostgreSQL antes de Railway. No se ejecuta automáticamente ni se aplicó a Supabase durante esta fase.
- La experiencia final de correo y Wallet requiere validación manual en dispositivos físicos y con las credenciales externas del entorno de staging.
