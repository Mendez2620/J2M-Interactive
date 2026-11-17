# J2M Interactive en infraestructura local

Esta fase prepara la salida de Railway sin eliminar ni modificar sus servicios. La aplicación permanece conectada inicialmente al PostgreSQL alojado en Supabase, pero **ningún ensayo local debe apuntar a esa base** hasta que exista una ventana de cambio autorizada.

## Estado y bloqueos del host

La reinspección del 2026-11-17 confirmó Windows, WSL 3.0.5 con Ubuntu como distribución WSL2, 32 GB de RAM, hipervisor activo y 117.38 GB libres en C:. El bloqueo de almacenamiento quedó resuelto. Docker Desktop todavía no estaba instalado ni disponible en `PATH`, por lo que la ejecución de contenedores continúa bloqueada hasta completar su instalación AMD64 con backend WSL2.

Windows Firewall y Microsoft Defender estaban habilitados, incluida la protección en tiempo real. La comprobación elevada indicó que C: no tiene un volumen BitLocker asociado; el disco no debe considerarse cifrado. El plan `Turbo` se ajustó para no suspender el equipo conectado a corriente; la suspensión en batería permanece en 240 segundos. El cambio debe confirmarse después de actualizaciones del fabricante y todavía falta probar un reinicio completo.

`j2minteractive.com` no tenía registros DNS y el RDAP autoritativo de `.com` devolvió 404 durante la revisión. Esto indica que parecía no registrado en ese instante, pero **no reserva ni garantiza disponibilidad**: el propietario debe comprobarlo y registrarlo con un registrador antes de configurar Cloudflare. No se modificó DNS.

## Respaldo de configuración

Antes de preparar contenedores se generó un respaldo cifrado con Windows DPAPI CurrentUser de las variables recuperables de web, Google Wallet worker, migrador y `.env` local. La verificación descifró cada contenedor en memoria y validó sus SHA-256 sin mostrar valores. Railway no reportó variables selladas; si aparecieran en otra ejecución, solo su nombre es recuperable y deben recrearse desde la fuente legítima.

DPAPI protege contra lectura fuera del perfil, pero no sustituye un respaldo ante pérdida del equipo. `scripts/portable_backup.py` permite descifrar los blobs DPAPI únicamente en memoria y volver a cifrarlos con AES-256-GCM y una clave derivada mediante scrypt. La contraseña se solicita de forma interactiva, no se acepta por argumentos ni se registra. Ejemplo:

```powershell
python scripts/portable_backup.py create `
  C:\Users\mende\Documents\J2M-Secure-Backup-20261116-170835 `
  E:\J2M\j2m-staging-20261117.j2mbackup `
  --decrypt-dpapi
python scripts/portable_backup.py verify E:\J2M\j2m-staging-20261117.j2mbackup
```

El destino debe ser un medio externo cifrado o retirado físicamente. Conservar la contraseña fuera del equipo. Nunca subir el archivo a GitHub.

## Archivos Compose

- `compose.selfhost.yml`: web, Google Wallet worker, migrador manual y perfil opcional `tunnel`.
- `compose.selfhost.test.yml`: PostgreSQL 16 temporal en `tmpfs` para ensayos aislados.
- `.env.selfhost.example`: inventario fail-closed de producción, sin secretos.
- `.env.selfhost.test.example`: valores públicos desechables para la base temporal; nunca usar con Supabase.
- `.env.cloudflared.example`: único inventario del túnel; no contiene secretos de aplicación.

La imagen de `cloudflared` está fijada a `2026.11.0` y al digest multi-arquitectura observado. El token se entrega como `TUNNEL_TOKEN` desde `.env.cloudflared`; cloudflared ya no recibe `DATABASE_URL`, claves Flask/JWT ni credenciales Wallet. Solo web publica un puerto, ligado a `127.0.0.1`; Cloudflare usa la red Docker. Web y worker reinician `unless-stopped`; el migrador es un perfil one-shot con `restart: no`.

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
La red del overlay de ensayo usa `internal: true`: web, worker, migrador y PostgreSQL no tienen salida a Supabase, Railway ni proveedores Wallet. Esta defensa impide que una URL externa accidental funcione durante el ensayo.

## Preparación de producción sin corte

1. Instalar Docker Desktop AMD64 con backend WSL2 y habilitar su inicio con la sesión de Windows. Verificar `docker info` y `docker compose version`.
2. Copiar `.env.selfhost.example` a `.env.selfhost`; rellenarlo desde el respaldo cifrado sin imprimir valores. Mantener `APPLE_WALLET_MODE=disabled` y comenzar con Google Wallet deshabilitado.
3. Cuando el propietario decida dominio y organización, crear `.env.cloudflared` desde su plantilla y un túnel remoto. Configurar el hostname público hacia `http://web:8000`; no abrir 80/443 en el router. Cloudflare Tunnel realiza conexiones salientes y ofrece `/ready` en su puerto de métricas.
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

## Dominios, subdominios y prefijos

La aplicación ya genera URLs con `url_for` en la mayoría de los flujos y permite configurar el origen canónico mediante `PUBLIC_BASE_URL`, CORS y WebAuthn. Producción exige un origen HTTPS sin ruta; cookies seguras, RP ID y redirecciones dependen de ese origen. Por ello, dominios o subdominios distintos son configurables, pero montar toda la aplicación debajo de un prefijo inverso como `/lealtad` todavía **no está aprobado**: existen redirecciones absolutas `/admin/...`, rutas estáticas y contratos Wallet/WebAuthn que requieren una revisión coordinada. Las rutas actuales `/admin`, `/manager` y `/api` permanecen intactas. No configurar `StripPrefix`, `SCRIPT_NAME` ni reescrituras en Cloudflare hasta aprobar ese contrato.

## Límites pendientes

- Docker Desktop aún no está instalado, por lo que no fue posible ejecutar Compose ni una construcción local. El espacio ya no bloquea.
- C: no usa BitLocker. La suspensión en corriente quedó deshabilitada; falta probar recuperación automática tras un reinicio real.
- Falta adquirir/controlar el dominio y crear el túnel; no existe token Cloudflare respaldado todavía.
- No se probó conectividad desde contenedores hacia Supabase ni una restauración de respaldo PostgreSQL.
- Un único equipo y una única conexión eléctrica/ISP no proporcionan alta disponibilidad. Se recomienda UPS, respaldo externo, monitoreo desde otra red y un segundo host antes de retirar Railway.

## Referencias operativas

- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/
- Token de túnel y variable `TUNNEL_TOKEN`: https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/
- Publicación de hostnames: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/
- Observabilidad y endpoint `/ready`: https://developers.cloudflare.com/tunnel/observability/
