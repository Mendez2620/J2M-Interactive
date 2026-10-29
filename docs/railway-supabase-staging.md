# Railway + Supabase: preproducción

Esta guía prepara `j2m-web` para Railway y PostgreSQL de Supabase. No despliega, no crea recursos y no incluye secretos. La conexión se realiza con SQLAlchemy/psycopg2; no se usa Supabase Auth ni Data API.

## Arquitectura preparada

- Railway construye el `Dockerfile`: Node 22 ejecuta `npm ci` y `npm run build:css`; la imagen final usa Python 3.12, `requirements.lock` y Gunicorn.
- Gunicorn escucha en `0.0.0.0:$PORT`. `/api/health` solo confirma que el proceso responde; `/api/ready` hace un `SELECT 1` y confirma conectividad mínima con la base.
- Producción no arranca sin una `DATABASE_URL` PostgreSQL explícita. La URL debe apuntar al Session pooler de Supabase en el puerto 5432; SQLite queda limitado a desarrollo y pruebas.
- SQLAlchemy usa un pool conservador (3 conexiones, 2 de desborde), `pool_pre_ping` y un modo TLS obligatorio: `require`, `verify-ca` o `verify-full`. `require` cifra la conexión, pero no verifica por sí solo la identidad del servidor.
- Las subidas se deshabilitan por defecto en producción. El sistema no debe prometer persistencia en el disco efímero de Railway.
- Las migraciones son un paso manual controlado; ninguna petición ni arranque de Gunicorn ejecuta Alembic.

## 1. Conectar Railway sin desplegar código no aprobado

1. En el proyecto Railway `j2m-interactive-staging`, abra el servicio `j2m-web` y conecte el repositorio `Mendez2620/J2M-Interactive`.
2. Seleccione una rama de staging protegida y revisada. No habilite despliegues de entornos efímeros para cada PR y no apunte temporalmente a una rama sin aprobar.
3. Confirme que Railway detecta el `Dockerfile` y `railway.toml`.
4. Mantenga el servicio detenido hasta cargar y revisar todas las variables. Este documento no autoriza pulsar Deploy.
5. Cuando se autorice el primer despliegue, confirme que el dominio asignado es HTTPS y use su origen exacto en `PUBLIC_BASE_URL` y `CORS_ORIGINS`.

Railway inyecta `PORT`; no configure un puerto fijo. El health check usa `/api/ready` y solo activa una nueva versión cuando responde `2xx`. Railway no convierte este health check en monitor continuo, por lo que después deberá añadirse observabilidad externa si staging permanece activo.

## 2. Obtener la conexión de Supabase

1. En Supabase abra **Connect** y copie la cadena de **Session pooler**; no construya manualmente el host ni el usuario.
2. Verifique que el host sea el pooler compartido, que el usuario incluya el identificador del proyecto y que el puerto sea `5432`.
3. Sustituya la contraseña solo dentro de la variable privada `DATABASE_URL` de Railway. Codifique caracteres reservados de la contraseña para una URL.
4. Añada `sslmode=require` a la URL o mantenga `DATABASE_SSLMODE=require`. Los modos `disable`, `allow` y `prefer` hacen fallar el arranque de producción. En Supabase active **Enforce SSL**. Para verificar CA y hostname se requiere instalar el certificado real de Supabase antes de usar `verify-full`; esta fase no incorpora certificados ni afirma haber validado esa modalidad en Railway.
5. No pegue la URL en GitHub, logs, tickets, capturas ni comandos que queden en historial.

La documentación oficial de Supabase confirma que el Session pooler compartido utiliza puerto 5432 y es compatible con IPv4. El pool pequeño evita consumir innecesariamente las conexiones del plan gratuito.

## 3. Variables privadas de Railway

Obligatorias antes de arrancar:

- `FLASK_ENV`
- `SECRET_KEY`
- `JWT_SECRET_KEY`
- `DATABASE_URL`
- `CORS_ORIGINS`
- `PUBLIC_BASE_URL`
- `SESSION_COOKIE_SECURE`
- `RECOVERY_MAIL_BACKEND`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USERNAME`
- `SMTP_PASSWORD`
- `SMTP_FROM`
- `SMTP_USE_TLS`
- `TRUSTED_PROXY_COUNT`
- `UPLOAD_STORAGE_BACKEND`

Recomendadas/ajustables:

- `DATABASE_SSLMODE`
- `DATABASE_POOL_SIZE`
- `DATABASE_MAX_OVERFLOW`
- `DATABASE_POOL_TIMEOUT`
- `DATABASE_POOL_RECYCLE`
- `DATABASE_CONNECT_TIMEOUT`
- `WEB_CONCURRENCY`
- `GUNICORN_THREADS`
- `GUNICORN_TIMEOUT`
- `LOG_REQUESTS`
- `JWT_ISSUER`
- `JWT_AUDIENCE`

Use `FLASK_ENV=production`, `SESSION_COOKIE_SECURE=true`, `DATABASE_SSLMODE=require`, `UPLOAD_STORAGE_BACKEND=disabled` y secretos independientes generados con suficiente entropía. El arranque rechaza una base ausente/no PostgreSQL, TLS opcional y almacenamiento local. `CORS_ORIGINS` debe contener únicamente los orígenes HTTPS necesarios, nunca `*`.

Railway termina TLS delante de la aplicación. Configure `TRUSTED_PROXY_COUNT=1` únicamente después de verificar que el edge de Railway es el único proxy y que reemplaza los encabezados reenviados; si la topología cambia, ajuste el número exacto. No aumente el valor para corregir una URL incorrecta.

## 4. Migraciones y primer administrador

Antes de migrar, compruebe que la base está vacía o que existe un respaldo restaurable. Desde una ejecución puntual de Railway con las mismas variables privadas:

```sh
flask --app run.py db current
flask --app run.py db upgrade
flask --app run.py db current
```

El resultado final esperado es `1b2c3d4e5f6a (head)`. No ejecute `seed-db`, `db.drop_all()`, downgrades ni scripts de semillas. No añada `db upgrade` al `CMD`, al health check ni a una petición web: dos réplicas podrían intentar migrar simultáneamente.

Un `/api/ready` con estado `200` solo demuestra que `SELECT 1` funciona. No demuestra que las migraciones estén aplicadas. Confirme `flask --app run.py db current`, ejecute el upgrade controlado y vuelva a confirmar el head antes de abrir los paneles.

Después, cree un administrador ficticio de staging mediante una ejecución interactiva y no registre su contraseña:

```sh
flask --app run.py create-platform-admin
```

## 5. Verificación controlada

1. Consulte `/api/health`: debe responder `200` sin probar la base.
2. Consulte `/api/ready`: debe responder `200`; una base inaccesible debe producir `503`. Después verifique Alembic por separado, ya que readiness no valida el esquema.
3. Revise que las cookies tengan `Secure`, `HttpOnly` y `SameSite=Lax`, que HSTS/CSP sigan presentes y que CORS rechace orígenes no configurados.
4. Autentique únicamente cuentas ficticias y compruebe aislamiento entre negocios, CSRF, revocación y rate limiting.
5. Consulte logs por `X-Request-ID`. No imprima variables, cuerpos, cabeceras `Authorization`, cookies, códigos de recuperación, JWT ni cadenas de conexión.

## 6. Logos y portadas

El filesystem del contenedor Railway no es almacenamiento permanente: un rebuild, redeploy o cambio de réplica puede perder archivos. Por ello producción exige `UPLOAD_STORAGE_BACKEND=disabled`, rechaza `local` al arrancar y devuelve un error explícito si se intenta adjuntar una imagen. Desarrollo y pruebas conservan almacenamiento local.

Antes de habilitarlas debe implementarse y probarse un adaptador de almacenamiento persistente, por ejemplo Supabase Storage con un bucket dedicado, políticas de acceso mínimas, nombres aleatorios, validación Pillow antes de subir, eliminación compensatoria ante rollback y URLs compatibles con Apple/Google Wallet. También se necesita migrar los archivos existentes, validar restauración y probar fallos parciales. No cambie `UPLOAD_STORAGE_BACKEND` a `local` en Railway.

## 7. Coste, pausa y operación

- Mantenga una sola réplica y los valores conservadores de Gunicorn/pool mientras se mide memoria y conexiones.
- Revise consumo y límites en Railway y Supabase antes y después de cada sesión de pruebas; configure alertas disponibles en los planes usados.
- Detenga el servicio Railway cuando no se utilice. Pausar el proceso no sustituye respaldar la base ni los objetos.
- Los proyectos gratuitos pueden pausarse o imponer límites; confirme las políticas actuales en los paneles antes de una demostración.
- Para rollback, retire tráfico y restaure un respaldo compatible. No use una migración descendente destructiva.

## Pendientes antes de tráfico real

- Probar esta versión contra el PostgreSQL real de Supabase; la suite local no lo sustituye.
- Ejecutar una imagen correctamente construida en Railway y verificarla allí; el build Docker de CI no demuestra que el servicio ya se haya probado o desplegado en Railway.
- Verificar la cadena, TLS, topología real de proxy, DNS y dominio público dentro de Railway.
- Implementar almacenamiento persistente antes de admitir logos o portadas.
- Configurar SMTP real, respaldos/restauración, monitoreo y credenciales Wallet; los modos mock no equivalen a emisión real.

Referencias: [Railway health checks](https://docs.railway.com/deployments/healthchecks), [Supabase: conexión a Postgres](https://supabase.com/docs/guides/database/connecting-to-postgres), [Supabase: SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement).
