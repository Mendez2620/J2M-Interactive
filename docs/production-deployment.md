# Despliegue seguro de J2M Interactive

Esta guía prepara un despliegue; no sustituye una revisión de infraestructura ni activa servicios externos.

## Plataforma y proceso

- Use Python 3.12, Node.js 22 solo durante la compilación, PostgreSQL soportado por SQLAlchemy, HTTPS y un servidor WSGI multiproceso (por ejemplo Gunicorn en Linux o Waitress en Windows).
- Instale Python con `pip install -r requirements.txt`. Compile CSS de forma reproducible con `npm ci && npm run build:css`; el archivo generado `app/static/css/tailwind.min.css` debe coincidir con Git.
- Sirva `/static` desde la aplicación o el proxy sin permitir listados. No use el compilador Tailwind CDN.

## Variables y secretos

Parta de `.env.example`, pero entregue los secretos mediante el almacén seguro de la plataforma, nunca mediante Git. Genere `SECRET_KEY` y `JWT_SECRET_KEY` independientes con al menos 32 bytes aleatorios (`python -c "import secrets; print(secrets.token_urlsafe(48))"`). Configure un `DATABASE_URL` PostgreSQL, `CORS_ORIGINS` explícitos, `PUBLIC_BASE_URL=https://...`, cookies seguras y `RECOVERY_MAIL_BACKEND=smtp`.

Separe desarrollo, staging y producción. Proteja y respalde certificados Apple, la clave privada Google, SMTP y la contraseña de base de datos con acceso mínimo. Rotar `SECRET_KEY` invalida sesiones web y enlaces firmados; rotar `JWT_SECRET_KEY` invalida JWT del personal. Planifique una ventana, reautenticación y revocación. No se implementó validación simultánea con claves antiguas ni rotación sin interrupción.

## HTTPS y proxy

Termine TLS en un proxy controlado y configure `TRUSTED_PROXY_COUNT` con el número exacto de saltos conocidos; use `0` si Flask recibe directamente al cliente. Nunca incremente el valor para “arreglar” encabezados: confiar en saltos inexistentes permite falsificar IP y esquema. El proxy debe reemplazar, no anexar ciegamente, `X-Forwarded-For`, `X-Forwarded-Proto` y `X-Forwarded-Host`. Compruebe HSTS, `Secure`, `HttpOnly` y `SameSite=Lax`.

## Base, migraciones y primer acceso

1. Cree una base y usuario PostgreSQL con privilegios limitados.
2. Haga un respaldo consistente y pruebe su restauración en un entorno aislado.
3. Ejecute `flask --app run.py db upgrade` una vez por versión antes de abrir tráfico.
4. Cree el primer administrador con `flask --app run.py create-platform-admin`.
5. Configure permisos y habilite canales web/móvil explícitamente.

Nunca ejecute `seed.py` ni `flask seed-db` sobre una instalación existente: pueden eliminar tablas. Los downgrades destructivos están bloqueados; el rollback operativo consiste en detener tráfico, restaurar el respaldo verificado y desplegar la versión anterior compatible.

## Correo y Wallet

Configure `SMTP_HOST`, puerto, remitente, TLS y credenciales. Verifique entrega y rebotes con una cuenta de prueba; el backend en memoria está prohibido en producción. Apple Wallet requiere certificados Pass Type y WWDR vigentes, clave protegida y HTTPS. Google Wallet requiere issuer y cuenta de servicio. Los modos mock solo prueban estructura y nunca deben anunciarse como emisión real. Las fallas externas posteriores al commit no revierten movimientos; todavía no existe entrega exactamente una vez ni una cola de reintentos.

## Operación, salud y registros

- `GET /api/health` confirma que el proceso responde.
- `GET /api/ready` ejecuta una lectura mínima de la base y devuelve `503` si no está disponible. Ninguna ruta ejecuta migraciones.
- Envíe stdout/stderr a un recolector con rotación y retención. Cada respuesta contiene `X-Request-ID`; los logs JSON incluyen UTC, endpoint, estado y duración, sin cuerpos, Authorization, cookies, contraseñas, códigos, JWT o QR.
- Limite el acceso público a estas sondas en el proxy si su plataforma no las necesita desde Internet. No hay endpoint público de métricas privadas.

## Respaldo, restauración y actualizaciones

Automatice respaldos cifrados de PostgreSQL y de certificados/recursos subidos, con retención separada. Pruebe restauraciones periódicas y documente RPO/RTO. Para actualizar: respaldo, prueba en staging, compilación CSS, suite automatizada, migración, despliegue gradual, readiness y revisión de errores. Si falla, retire tráfico y restaure; no improvise una migración inversa destructiva.

Antes de atender clientes reales valide en la infraestructura elegida PostgreSQL concurrente, SMTP, DNS/TLS, proxy, CORS, Apple/Google Wallet, APNs, respaldos, restauración, alertas y capacidad.
