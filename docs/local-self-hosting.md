# J2M Interactive en infraestructura local

Esta fase prepara la salida de Railway sin eliminar ni modificar sus servicios. La aplicación permanece conectada inicialmente al PostgreSQL alojado en Supabase, pero **ningún ensayo local debe apuntar a esa base** hasta que exista una ventana de cambio autorizada.

## Estado y bloqueos del host

La inspección del 2026-11-16 confirmó Windows, WSL 3.0.5 con Ubuntu en WSL2, 32 GB de RAM, conectividad HTTPS hacia GitHub/Supabase y salida TCP 7844 hacia Cloudflare. Docker no estaba instalado y la unidad C: conservaba solo 5.06 GB libres. No instalar Docker ni descargar imágenes hasta disponer de al menos 20 GB libres; para operación y respaldos se recomiendan 40 GB o más y un destino externo cifrado.

Windows Firewall y Microsoft Defender estaban habilitados, incluida la protección en tiempo real. No se pudo confirmar BitLocker para C:, por lo que la protección del disco queda como bloqueo de seguridad antes de almacenar copias operativas. El equipo admite suspensión S0 con la red desconectada: debe configurarse para no suspenderse mientras esté conectado a corriente durante la operación, decisión que no se aplicó automáticamente.

`j2minteractive.com` no tenía registros DNS y el RDAP autoritativo de `.com` devolvió 404 durante la revisión. Esto indica que parecía no registrado en ese instante, pero **no reserva ni garantiza disponibilidad**: el propietario debe comprobarlo y registrarlo con un registrador antes de configurar Cloudflare. No se modificó DNS.

## Respaldo de configuración

Antes de preparar contenedores se generó un respaldo cifrado con Windows DPAPI CurrentUser de las variables recuperables de web, Google Wallet worker, migrador y `.env` local. La verificación descifró cada contenedor en memoria y validó sus SHA-256 sin mostrar valores. Railway no reportó variables selladas; si aparecieran en otra ejecución, solo su nombre es recuperable y deben recrearse desde la fuente legítima.

DPAPI protege contra lectura fuera del perfil, pero no sustituye un respaldo ante pérdida del equipo. Copiar el directorio cifrado a un volumen externo protegido y conservar también el perfil/clave de recuperación de Windows, o crear posteriormente un segundo archivo cifrado con una contraseña que el propietario guarde fuera del equipo. Nunca subirlo a GitHub.

## Archivos Compose

- `compose.selfhost.yml`: web, Google Wallet worker, migrador manual y perfil opcional `tunnel`.
- `compose.selfhost.test.yml`: PostgreSQL 16 temporal en `tmpfs` para ensayos aislados.
- `.env.selfhost.example`: inventario fail-closed de producción, sin secretos.
- `.env.selfhost.test.example`: valores públicos desechables para la base temporal; nunca usar con Supabase.

La imagen de `cloudflared` está fijada a `2026.11.0` y al digest multi-arquitectura observado. El token se entrega como `TUNNEL_TOKEN` desde el archivo privado y no aparece en argumentos del proceso. Solo web publica un puerto, ligado a `127.0.0.1`; Cloudflare usa la red Docker. Web y worker reinician `unless-stopped`; el migrador es un perfil one-shot con `restart: no`.

## Ensayo aislado (cuando Docker esté disponible)

```powershell
$env:J2M_ENV_FILE = ".env.selfhost.test.example"
docker compose --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml --profile migrate run --rm migrator
docker compose --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml up -d web google-wallet-worker
Invoke-WebRequest http://127.0.0.1:8000/api/health
Invoke-WebRequest http://127.0.0.1:8000/api/ready
docker compose --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml down
```

El PostgreSQL temporal no publica puertos y se pierde al detener el contenedor. `GOOGLE_WALLET_MODE` y `APPLE_WALLET_MODE` permanecen `disabled`, por lo que no hay llamadas ni escrituras Wallet.

## Preparación de producción sin corte

1. Liberar almacenamiento, instalar Docker Desktop con backend WSL2 y habilitar su inicio con la sesión de Windows. Verificar `docker info` y `docker compose version`.
2. Copiar `.env.selfhost.example` a `.env.selfhost`; rellenarlo desde el respaldo cifrado sin imprimir valores. Mantener `APPLE_WALLET_MODE=disabled` y comenzar con Google Wallet deshabilitado.
3. Registrar un dominio controlado, añadirlo a Cloudflare y crear un túnel remoto. Configurar el hostname público hacia `http://web:8000`; no abrir 80/443 en el router. Cloudflare Tunnel realiza conexiones salientes y ofrece `/ready` en su puerto de métricas.
4. Ejecutar `scripts/selfhost_preflight.ps1`. Corregir todos los errores antes de arrancar.
5. Ensayar Docker con la base temporal. No utilizar la URL Supabase en el ensayo.
6. Crear un respaldo consistente y restaurable de PostgreSQL con las herramientas de Supabase/`pg_dump` en una ubicación cifrada externa. Probar la restauración en otra base aislada.
7. Durante la ventana autorizada, detener cualquier worker que pueda escribir en paralelo, ejecutar el migrador una sola vez solo si Alembic lo requiere y validar `/api/ready` además de la revisión Alembic. Esta fase no ejecuta ese paso.
8. Iniciar la versión exacta aprobada de `origin/main` con `selfhost_deploy_approved.ps1`; el script rechaza un SHA de PR o un árbol sucio y no ejecuta migraciones.
9. Mantener Railway activo hasta validar HTTPS, cookies, CSRF, sesiones, registro, escáner y outboxes. El rollback consiste en retirar el hostname del túnel y volver a dirigir tráfico a Railway; no hacer downgrade ni eliminar tablas.

## Operación y seguridad

- Docker Desktop debe iniciar automáticamente; Compose recupera web, worker y túnel con `unless-stopped`. El migrador nunca arranca automáticamente.
- Vigilar `/api/health`, `/api/ready`, el estado del contenedor, `http://127.0.0.1:2000/ready`, espacio libre y cola del worker. `/api/ready` no demuestra que Alembic esté actualizado.
- Los logs rotan en Docker (`10 MB`, cinco archivos). No registrar cuerpos, cookies, tokens ni credenciales.
- GitHub Actions continúa validando PostgreSQL 16 y la imagen Linux. Solo se construye localmente un SHA exacto que sea el `origin/main` aprobado; nunca ejecutar imágenes provenientes de PRs no confiables.
- Para rollback, conservar al menos la imagen del SHA anterior aprobado y no aplicar downgrades destructivos. Restaurar base únicamente desde un respaldo probado y con autorización.

## Límites pendientes

- Falta espacio en disco e instalación de Docker, por lo que no fue posible ejecutar Compose ni una construcción local.
- Falta confirmar cifrado de disco, desactivar suspensión durante operación y probar recuperación automática tras un reinicio real.
- Falta adquirir/controlar el dominio y crear el túnel; no existe token Cloudflare respaldado todavía.
- No se probó conectividad desde contenedores hacia Supabase ni una restauración de respaldo PostgreSQL.
- Un único equipo y una única conexión eléctrica/ISP no proporcionan alta disponibilidad. Se recomienda UPS, respaldo externo, monitoreo desde otra red y un segundo host antes de retirar Railway.

## Referencias operativas

- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/
- Token de túnel y variable `TUNNEL_TOKEN`: https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/
- Publicación de hostnames: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/
- Observabilidad y endpoint `/ready`: https://developers.cloudflare.com/tunnel/observability/
