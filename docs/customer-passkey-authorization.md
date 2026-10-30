# Autorización asistida de clientes con passkeys (8C.3B1)

Esta fase no cambia el alta ordinaria de la tarjeta: continúa sin correo, SMS, OTP, contraseña ni passkey obligatoria. Tampoco habilita todavía “Invitar amigos”.

## Garantía y límite de identidad

La vinculación aporta prueba criptográfica de que una passkey previamente enrolada participa en una ceremonia WebAuthn válida para el origen y RP ID configurados, con verificación de usuario requerida. No acredita identidad civil. La decisión inicial continúa dependiendo de una comprobación humana realizada por personal autorizado conforme a la política del negocio; esa comprobación se documenta y audita, pero no se presenta como infalible.

Conocer teléfono, nombre, Customer ID, QR de lealtad, URL Wallet o una sesión temporal de entrega nunca permite iniciar sesión ni vincular una credencial.

## Vinculación inicial

1. Staff activo del mismo negocio con `authorize_customer_identity` selecciona al Customer y documenta la comprobación humana.
2. El servidor crea una autorización de cinco minutos y entrega una sola vez un secreto aleatorio. Solo se conserva su SHA-256.
3. El dispositivo del cliente reclama el secreto por POST con CSRF. El servidor lo liga a ese navegador y muestra un código de emparejamiento de seis dígitos.
4. Staff confirma en su sesión revocable que observa el mismo código. El código se protege con HMAC en la base.
5. Solo ese navegador puede solicitar las opciones WebAuthn. El servidor exige verificación de usuario y valida challenge, origen, RP ID y respuesta criptográfica con `webauthn`.
6. El desafío se consume una vez y se crea una sesión de cliente independiente, revocable y tenant-scoped.

El secreto no aparece en rutas ni logs. Interceptar solo el secreto no basta sin la confirmación presencial del emparejamiento; interceptar secreto y código durante la ventana sigue siendo un riesgo operativo, por lo que el personal debe confirmar el dispositivo frente al cliente.

## Sesiones, revocación y recuperación

Las sesiones se validan en servidor contra Customer, Business, credencial activa, expiración absoluta e inactividad. La cookie Flask contiene identificadores y verificadores firmados, nunca claves privadas. Cerrar sesión, revocar una credencial o suspender el negocio bloquea el acceso.

La recuperación no utiliza correo ni OTP: personal autorizado crea una nueva vinculación en modo recuperación. Al finalizar correctamente la nueva passkey se revocan las credenciales y sesiones anteriores en la misma transacción. Esto reduce el impacto de un dispositivo perdido, pero mantiene el riesgo humano de una autorización indebida; los negocios deben limitar el permiso, revisar auditorías y aplicar separación de funciones cuando sea posible.

Varias credenciales legítimas se admiten mediante vinculaciones adicionales normales. Dos personas que comparten un dispositivo deben utilizar credenciales de plataforma separadas; autenticar una sustituye la identidad de cliente activa en ese navegador.

## Configuración

- `WEBAUTHN_ORIGIN`: origen exacto HTTPS. En producción puede omitirse para usar `PUBLIC_BASE_URL`.
- `WEBAUTHN_RP_ID`: dominio del RP, igual al host o sufijo registrable válido del origen.
- `WEBAUTHN_RP_NAME`: nombre mostrado por el autenticador.
- `CUSTOMER_LINKING_MAX_AGE`, `CUSTOMER_AUTH_CHALLENGE_MAX_AGE`.
- `CUSTOMER_SESSION_ABSOLUTE_SECONDS`, `CUSTOMER_SESSION_IDLE_SECONDS`.

La validación de producción falla cerrada si origen y RP ID no son compatibles.

## 8C.3B2 pendiente

La emisión pública de códigos/QR de cliente embajador permanece inexistente. La siguiente fase deberá exigir simultáneamente: sesión de cliente válida, credencial activa, consentimiento específico vigente, `ReferralParticipant` habilitado, negocio activo y programa de referidos activo. No se publicarán tokens de sesión en Wallet ni se modificarán pases existentes desde esta fase.

## Migración operativa futura

`5f6a7b8c9d0e` es incremental sobre `4e5f6a7b8c9d`. Crea cinco tablas internas con FKs tenant-scoped, índices, RLS en PostgreSQL, sin grants públicos y auditoría append-only. El downgrade solo se permite antes de crear cualquier historial. Para staging: confirmar respaldo y head anterior, fijar el migrador al SHA aprobado, ejecutar una sola vez la revisión, verificar tablas/FKs/RLS/triggers y después desplegar web. No relajar `EXPECTED_ALEMBIC_HEAD` ni ejecutar la migración desde web o workers.
