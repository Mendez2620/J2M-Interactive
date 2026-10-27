# Registro directo de clientes sin correo

El alta pública solicita nombre, teléfono internacional, aceptación de Términos y Aviso de Privacidad, y opcionalmente fecha de nacimiento y consentimiento de marketing. No solicita correo, no crea códigos OTP y no depende de SMTP. La tarjeta y el cliente se crean juntos en una única transacción.

La identidad se reserva por negocio mediante un HMAC estable del teléfono y la restricción compuesta de `customer_registration_identity_claims`. Esto permite el mismo teléfono en negocios distintos, pero evita duplicados y carreras dentro de un tenant. `REGISTRATION_IDENTITY_HMAC_KEY` debe ser estable, exclusiva y distinta de las claves de sesión y JWT.

La aceptación legal conserva fecha UTC, versión y SHA-256 del contenido presentado. La atribución de promotor usa un contexto firmado, vuelve a validar negocio y estado del promotor dentro de la transacción y bloquea la autorreferencia por teléfono.

## Datos históricos

Las columnas de correo, desafíos y trabajos del outbox se conservan para no destruir historial. Los clientes nuevos guardan `email=NULL` y `email_verified_at=NULL`; ningún endpoint público crea nuevos desafíos o correos de registro. Una eliminación o anonimización futura de correos históricos requiere política de retención, revisión legal, respaldo probado y una migración separada.

## Recuperación

La recuperación pública está deshabilitada: nombre, fecha de nacimiento, teléfono y el QR operativo de caja no prueban legítima posesión. Los clientes históricos no pierden saldos, movimientos ni identificadores Wallet. Quien necesite recuperar una tarjeta debe acudir al negocio; una fase futura diseñará un flujo asistido con permisos explícitos, motivo obligatorio, auditoría persistente y revocación de sesiones.

La experiencia Wallet debe validarse manualmente en dispositivos físicos. No se ejecutaron migraciones ni se modificaron Railway o Supabase reales.
