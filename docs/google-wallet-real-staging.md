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

El head vigente esperado para Fase 8C.2B es `3d4e5f6a7b8c`. La revisión `f3a4b5c6d7e8` agregó la cola persistente de sincronización; ninguna migración debe ejecutarse desde el comando de arranque, health check o petición web.

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

Procedimiento posterior al merge:

1. actualice `main` y después integre ese commit aprobado en `staging`;
2. despliegue manualmente con `GOOGLE_WALLET_MODE=disabled`;
3. ejecute `flask --app run.py db upgrade` como operación única;
4. confirme que `flask --app run.py db current` muestra `3d4e5f6a7b8c`;
5. cargue `GOOGLE_ISSUER_ID` y `GOOGLE_SERVICE_ACCOUNT_JSON` como variables privadas;
6. cambie el modo a `real`, reinicie y confirme que el proceso arranca;
7. revise únicamente logs sanitizados y los endpoints de salud;
8. cree un negocio ficticio con logotipo HTTPS legítimo;
9. guarde el pase desde un Android autorizado en Demo Mode;
10. acredite y canjee puntos o sellos desde los flujos normales;
11. ejecute la cola y confirme el saldo final tanto en J2M como en Wallet;
12. diagnostique por código HTTP y estado del outbox, nunca copiando JWT o credenciales;
13. ante fallos, vuelva a `disabled` y reinicie sin alterar el ledger.

Para desactivar la integración sin afectar el ledger, establezca `GOOGLE_WALLET_MODE=disabled` y reinicie. Las transacciones de J2M continúan; no se emitirán enlaces ni nuevos trabajos de Google hasta reactivar el modo real.

## Sincronización y reintentos

Cada transacción real registra, en la misma transacción de base de datos, un trabajo de sincronización. El endpoint del cajero responde `google_status=pending` sin esperar a Google. Un fallo externo no revierte el ledger ni el saldo. Ejecute manualmente en Railway, como comando puntual con las variables del servicio:

```sh
flask --app run.py process-google-wallet-sync
```

El comando termina por sí mismo e imprime únicamente cantidades `processed`, `pending`, `failed` y `completed`; se conserva para diagnóstico controlado. La operación normal requiere un servicio Railway independiente y permanente que ejecute `flask --app run.py run-google-wallet-sync-worker`. No lo agregue al proceso Gunicorn. Consulte [Operación del worker de Google Wallet](google-wallet-sync-worker.md) para configurar el servicio, métricas, alertas y recuperación segura del backlog.

En PostgreSQL, la cola toma un advisory lock transaccional por cliente. Dos workers no pueden enviar simultáneamente el saldo del mismo cliente; el lock se libera automáticamente al confirmar, revertir, perder la conexión o terminar el proceso. Una marca de agua limita qué trabajos puede completar cada ejecución. Los movimientos que lleguen durante la llamada permanecen pendientes y la siguiente ejecución lee el saldo confirmado más reciente. SQLite usa un lock equivalente dentro del proceso únicamente para desarrollo y pruebas; la garantía multiproceso de producción depende de PostgreSQL.

La emisión y la recuperación solo incluyen el balance al crear inicialmente un `LoyaltyObject`. Si el objeto ya existe, esos flujos actualizan únicamente metadatos seguros como QR, estado y textos; nunca envían `loyaltyPoints`. La cola protegida es la única ruta que actualiza puntos o sellos. Si una creación concurrente devuelve `409`, la emisión aplica solo metadatos y el worker vuelve a enviar el saldo confirmado antes de completar su trabajo.

Los errores `429`, `408`, `5xx`, timeouts y conectividad vuelven a `pending` con espera exponencial. Los errores permanentes quedan en `failed` con su código HTTP, sin borrar el movimiento. Si el objeto fue eliminado en Google, el proceso intenta reconstruir sus recursos. Si el worker se suspende, los trabajos permanecen en PostgreSQL; al reanudarlo se procesan en orden. Un proceso que termina abruptamente revierte su reclamo, y un estado heredado `processing` puede recuperarse después de cinco minutos sin solaparse con un propietario que aún conserve el advisory lock.

La concurrencia PostgreSQL se cubre por diseño y SQL específico, pero debe verificarse con dos ejecuciones reales en Railway staging antes de producción. Las pruebas locales SQLite validan de forma determinista la exclusión, la marca de agua, los reintentos y la recuperación; no sustituyen esa validación externa.

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
