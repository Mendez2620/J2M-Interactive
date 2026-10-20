# Google Wallet real en staging

Esta guía habilita Google Wallet de forma controlada después de desplegar y migrar staging. El modo real no debe activarse antes de completar todos los pasos.

## Modos y garantías

- `disabled`: no firma pases, no llama a Google y oculta las acciones de Google Wallet. Es el valor predeterminado de producción.
- `mock`: firma artefactos de prueba con una clave temporal generada en memoria. Solo está permitido en desarrollo y pruebas; nunca en producción.
- `real`: usa OAuth 2.0 mediante una cuenta de servicio, crea o actualiza `LoyaltyClass` y `LoyaltyObject`, y genera un enlace Save to Google Wallet que referencia el objeto persistente.

Los identificadores de clase y objeto son estables y contienen el ID interno del negocio para separar tenants. El objeto no envía correo, teléfono ni nombre completo del cliente. Los logs de errores solo incluyen estado HTTP y si el fallo admite reintento.

## Preparación manual en Google

1. Use una cuenta de emisor de Google Wallet existente y habilitada para pruebas.
2. Habilite Google Wallet API en el proyecto de Google Cloud correspondiente.
3. Cree o reutilice una cuenta de servicio dedicada y autorice su correo en la cuenta de emisor.
4. Descargue una clave JSON únicamente para cargarla como variable privada. No la copie al repositorio, al Dockerfile, a logs ni a documentación.
5. Mantenga la cuenta en modo de prueba durante staging y utilice solamente cuentas de prueba autorizadas.

El Issuer es la cuenta propietaria de las clases y objetos. Demo Mode permite desarrollar y validar pases marcados `[TEST ONLY]`; no equivale a acceso de publicación. En Google Pay & Wallet Console, copie el Issuer ID desde la configuración de la cuenta y agregue en **Test accounts** las cuentas de Google que usarán los dispositivos de prueba. No solicite acceso de publicación durante esta fase.

Referencias oficiales: [clases y objetos](https://developers.google.com/wallet/retail/loyalty-cards/overview/how-classes-objects-work), [creación](https://developers.google.com/wallet/retail/loyalty-cards/use-cases/create), [JWT Save to Google Wallet](https://developers.google.com/wallet/retail/loyalty-cards/use-cases/jwt) y [API LoyaltyClass](https://developers.google.com/wallet/reference/rest/v1/loyaltyclass).

## Variables privadas de Railway

Configure los valores reales manualmente en Railway; no los incluya en comandos compartidos:

```text
GOOGLE_WALLET_MODE=real
GOOGLE_ISSUER_ID=<identificador numérico del emisor>
GOOGLE_SERVICE_ACCOUNT_JSON=<JSON completo de la cuenta de servicio>
PUBLIC_BASE_URL=https://<dominio-publico-de-staging>
```

`PUBLIC_BASE_URL` y el logotipo de cada negocio deben usar HTTPS. Una credencial incompleta, un emisor inválido o `mock` en producción impiden el arranque. La aplicación no imprime la URL de base de datos, el JSON, claves, tokens ni datos privados.

## Migración y activación

Antes de cambiar el modo a `real`, haga un respaldo restaurable y ejecute una única migración controlada con las mismas variables de staging:

```sh
flask --app run.py db current
flask --app run.py db upgrade
flask --app run.py db current
```

El head esperado es `f3a4b5c6d7e8`. La migración agrega una cola persistente de sincronización y no modifica saldos ni transacciones existentes. No ejecute migraciones desde el comando de arranque, health check o petición web.

Después de migrar, cambie `GOOGLE_WALLET_MODE` a `real`, reinicie el servicio y confirme `/api/health` y `/api/ready`. Un `200` en `/api/ready` solo valida una consulta mínima; no confirma el estado de las migraciones.

El downgrade de esta revisión está bloqueado deliberadamente: eliminar la tabla de outbox podría perder sincronizaciones pendientes. Para volver atrás, primero deshabilite Google Wallet, procese o documente los trabajos pendientes y prepare una migración de conservación específica.

## Publicación del código en staging

Después de aprobar y fusionar el PR, actualice la rama `staging` desde `main` mediante el flujo Git revisado por el equipo. Confirme que el commit desplegado es el aprobado y lance un despliegue manual de `j2m-web`; los despliegues automáticos deben permanecer deshabilitados. No cambie variables, ejecute migraciones ni despliegue desde una sesión compartida de diagnóstico.

Orden recomendado:

1. mantener `GOOGLE_WALLET_MODE=disabled`;
2. desplegar el commit aprobado;
3. ejecutar y verificar la migración controlada;
4. configurar las variables privadas;
5. cambiar a `real` y reiniciar manualmente;
6. validar salud, preparación y logs sin secretos;
7. configurar la ejecución periódica del worker de sincronización.

Para desactivar la integración sin afectar el ledger, establezca `GOOGLE_WALLET_MODE=disabled` y reinicie. Las transacciones de J2M continúan; no se emitirán enlaces ni nuevos trabajos de Google hasta reactivar el modo real.

## Sincronización y reintentos

Cada transacción real registra, en la misma transacción de base de datos, un trabajo de sincronización. Un fallo de Google no revierte el ledger ni el saldo. Configure una ejecución periódica y de una sola instancia del siguiente comando:

```sh
flask --app run.py process-google-wallet-sync
```

La cola usa reclamo condicional, reintentos con espera creciente y conserva únicamente códigos de estado seguros. Una respuesta no reintentable queda marcada como fallida para revisión operativa. Si el objeto fue eliminado en Google, el proceso intenta reconstruir sus recursos antes de confirmar la sincronización.

## Validación pendiente del propietario

Las pruebas automatizadas usan credenciales, firmas y respuestas HTTP falsas; no contactan Google. La construcción Docker tampoco demuestra una emisión real. Antes de producción, el propietario debe validar en Railway staging, con una cuenta de prueba y un dispositivo Android real:

1. alta de un cliente y apertura del enlace Save to Google Wallet;
2. creación o actualización efectiva de la clase y el objeto;
3. actualización de puntos y sellos después de una transacción;
4. recuperación de una tarjeta existente;
5. reintento después de un fallo controlado;
6. ausencia de correo, teléfono y nombre completo en el pase y en logs.

Para la prueba inicial, cree desde Super Admin un negocio ficticio sin datos de clientes reales, configure un programa de ocho sellos, colores válidos y un logotipo legítimo disponible públicamente por HTTPS. Registre un cliente de prueba, abra el botón en Android con una cuenta autorizada y confirme que Google muestra `[TEST ONLY]`, el QR y `0 / 8`. Procese una acreditación autorizada y verifique primero el ledger en J2M y después `1 / 8` en Google Wallet. Repita con un canje válido para confirmar el saldo resultante.

Para diagnosticar fallos, correlacione la hora y el código HTTP seguro del log con el estado del trabajo en `google_wallet_sync_jobs`. `400` suele indicar un payload o recurso inválido; `401` y `403`, autenticación o autorización; `404`, un recurso ausente que el sincronizador intentará reconstruir; `409`, creación concurrente ya resuelta mediante actualización; y `429` o fallos de red, reintento. Nunca copie JWT, enlaces completos, encabezados Authorization ni la credencial JSON en incidencias.

Si fuera necesario rotar la credencial, cree la nueva clave desde el procedimiento autorizado de Google Cloud, sustituya atómicamente `GOOGLE_SERVICE_ACCOUNT_JSON` en Railway, reinicie y valide una operación antes de revocar la clave anterior. La aplicación no crea ni rota claves. Evite mantener dos claves activas más tiempo del necesario.

La recuperación reutiliza el mismo ID estable del objeto tras verificar el código enviado por correo. Si SMTP no está configurado o validado en staging, esa prueba queda bloqueada externamente y no debe darse por operativa.

No solicite publicación general del emisor hasta completar esa validación. Apple Wallet, Flutter, Supabase, Railway y SMTP no son modificados por esta integración; una configuración externa incorrecta de esos servicios debe resolverse por separado.
