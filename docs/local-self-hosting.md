# J2M Interactive en infraestructura local

Esta fase prepara la salida de Railway sin eliminar ni modificar sus servicios. La aplicación permanece conectada inicialmente al PostgreSQL alojado en Supabase, pero **ningún ensayo local debe apuntar a esa base** hasta que exista una ventana de cambio autorizada.

## Estado y bloqueos del host

La reinspección del 2026-11-17 confirmó Windows, WSL 3.0.5 con Ubuntu como distribución WSL2, 32 GB de RAM, hipervisor activo y más de 116 GB libres en C:. Docker Desktop 4.99.0, Engine/CLI 29.9.3 y Compose v5.7.0 funcionan con el contexto `desktop-linux`. El named pipe está fuera del sandbox de Codex, por lo que los comandos automatizados requieren la autorización normal para ejecutarse en el contexto del usuario; no se reinició Codex ni Windows.

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
- `compose.selfhost.production-test.yml`: PostgreSQL temporal con TLS y aplicación en modo producción, siempre con credenciales sintéticas.
- `compose.selfhost.loopback-test.yml`: red adicional exclusiva de web para comprobar el binding de Windows en loopback; no sustituye la prueba principal con red interna.
- `.env.selfhost.example`: inventario fail-closed de producción, sin secretos.
- `.env.selfhost.test.example`: valores públicos desechables para la base temporal; nunca usar con Supabase.
- `.env.selfhost.production-test.example`: valores sintéticos reproducibles para `FLASK_ENV=production`; nunca reutilizar como secretos reales.
- `.env.cloudflared.example`: único inventario del túnel; no contiene secretos de aplicación.

La imagen de `cloudflared` está fijada a `2026.11.0` y al digest multi-arquitectura observado. El token se entrega como `TUNNEL_TOKEN` desde `.env.cloudflared`; cloudflared ya no recibe `DATABASE_URL`, claves Flask/JWT ni credenciales Wallet. Solo web publica un puerto, ligado a `127.0.0.1`; Cloudflare usa la red Docker. Web y worker reinician `unless-stopped`; el migrador es un perfil one-shot con `restart: no`.

## Ensayo aislado reproducible

```powershell
$env:J2M_IMAGE = "j2m-interactive:pr70-test"
docker compose -p j2m-pr70-test --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml config --quiet
docker compose -p j2m-pr70-test --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml build web
docker compose -p j2m-pr70-test --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml up -d --wait postgres-test
docker compose -p j2m-pr70-test --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml --profile migrate run --rm migrator
docker compose -p j2m-pr70-test --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml up -d --no-build --wait web google-wallet-worker
docker exec j2m-pr70-test-web-1 python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/ready').status)"
docker compose -p j2m-pr70-test --env-file .env.selfhost.test.example -f compose.selfhost.yml -f compose.selfhost.test.yml down --remove-orphans
```

El PostgreSQL temporal no publica puertos y se pierde al detener el contenedor. `GOOGLE_WALLET_MODE` y `APPLE_WALLET_MODE` permanecen `disabled`, por lo que no hay llamadas ni escrituras Wallet.
La red del overlay de ensayo usa `internal: true`: web, worker, migrador y PostgreSQL no tienen salida a Supabase, Railway ni proveedores Wallet. Esta defensa impide que una URL externa accidental funcione durante el ensayo.

En Docker Desktop para Windows, una red marcada `internal` conserva en la configuración el binding, pero no lo materializa en el host. Por ello la prueba principal mantiene esa red y ejecuta HTTP desde web. Una segunda prueba explícita puede añadir `compose.selfhost.loopback-test.yml`: solo web recibe una red bridge adicional y PostgreSQL continúa exclusivamente en la red interna sin publicar puerto. Esta topología permite validar desde Windows sin abrir la aplicación a la LAN.

## Ensayo sintético de producción y loopback

El perfil no utiliza `.env.selfhost`, DPAPI, Supabase ni credenciales de proveedores. PostgreSQL genera al arrancar un certificado TLS autofirmado efímero mediante la imagen de prueba `Dockerfile.postgres-test`; la clave queda en el `tmpfs` del contenedor y desaparece al eliminarlo.

```powershell
docker compose -p j2m-pr70-production --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml config --quiet
docker compose -p j2m-pr70-production --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml build
docker compose -p j2m-pr70-production --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml up -d --wait postgres-test
docker compose -p j2m-pr70-production --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml --profile migrate run --rm migrator
docker compose -p j2m-pr70-production --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml up -d --wait web google-wallet-worker
```

Para comprobar el acceso de Windows se repite con un nombre de proyecto distinto y el cuarto overlay:

```powershell
docker compose -p j2m-pr70-loopback --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml `
  -f compose.selfhost.loopback-test.yml up -d --wait postgres-test
docker compose -p j2m-pr70-loopback --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml `
  -f compose.selfhost.loopback-test.yml --profile migrate run --rm migrator
docker compose -p j2m-pr70-loopback --env-file .env.selfhost.production-test.example `
  -f compose.selfhost.yml -f compose.selfhost.test.yml -f compose.selfhost.production-test.yml `
  -f compose.selfhost.loopback-test.yml up -d --wait web google-wallet-worker
Invoke-WebRequest http://127.0.0.1:18080/api/health
Invoke-WebRequest http://127.0.0.1:18080/api/ready
```

El ensayo real confirmó `FLASK_ENV=production`, debug deshabilitado, origen canónico HTTPS, CORS restringido a `https://j2m.local.test`, cookie `Secure`, WebAuthn con origen/RP ID compatibles, claves Flask/JWT diferentes, uploads y ambos Wallet deshabilitados. PostgreSQL aceptó exclusivamente la conexión TLS configurada y Alembic llegó a `9d0e1f2a3b4c`. La red interna bloqueó resolución hacia Supabase, Railway, Google Wallet y APNs. Desde Windows, `health` y `ready` respondieron 200 en `127.0.0.1:18080`; la inspección de Docker mostró únicamente `127.0.0.1:18080->8000`, nunca `0.0.0.0` ni `[::]`.

Las regresiones existentes de configuración comprueban además que producción falla al usar HTTP, CORS abierto, cookies inseguras, claves ausentes/iguales, WebAuthn incompatible, SQLite o TLS de PostgreSQL inseguro. No se relajan esos controles para el ensayo.

## Resultado real del ensayo PR #70

- La imagen Linux `j2m-interactive:pr70-test` se construyó completamente con usuario final `j2m`.
- PostgreSQL `16.16` arrancó healthy en `tmpfs`, sin puerto publicado.
- El migrador actualizó una base vacía hasta el único head `9d0e1f2a3b4c`; quedaron 42 tablas públicas.
- Web, Google Wallet worker y PostgreSQL alcanzaron estado healthy. Web y worker usaron root filesystem de solo lectura, `no-new-privileges` y `cap_drop: ALL`.
- `/api/health` y `/api/ready` respondieron 200. Login de Super Admin, gerente y acceso unificado respondieron 200; `/admin`, `/admin/dashboard` y `/manager/portal` redirigieron al login; un POST de login sin CSRF respondió 400; rutas inexistentes respondieron 404.
- Una tabla temporal y un registro ficticio se crearon, leyeron y revirtieron mediante la conexión Flask/PostgreSQL; no persistieron negocios, clientes ni movimientos.
- Reiniciar y detener/iniciar web y worker recuperó sus healthchecks, mientras PostgreSQL permaneció healthy y conservó la revisión Alembic.
- El apagado final eliminó exclusivamente los tres contenedores, la red interna, el PostgreSQL en `tmpfs` y la imagen etiquetada `j2m-interactive:pr70-test`; no quedaron recursos del proyecto Compose de ensayo.
- La resolución/conexión hacia Supabase, Railway, Google Wallet y APNs falló desde web como exige la red interna. Cloudflared no se creó.
- La imagen no contiene archivos DPAPI, respaldos portables, certificados privados ni archivos de entorno privados. `.env.example` sí está presente y contiene únicamente valores de ejemplo versionados.
- `pip check` dentro de la imagen no encontró dependencias rotas. `npm audit` reportó dos vulnerabilidades altas transitivas de build (`@parcel/watcher`/`picomatch`); no existe versión corregida publicada de `picomatch` y `node_modules` no forma parte de la imagen final. Mantener seguimiento y actualizar el toolchain cuando exista corrección.
- El preflight ahora comprueba únicamente `J2M_LOCAL_PORT` y, cuando se solicita el perfil de túnel, `CLOUDFLARED_METRICS_PORT`. Con `18080` ocupado bloqueó el arranque; tras detener el stack aprobó. También bloquea si Apple Wallet o Google Wallet no están exactamente en `disabled` durante el primer corte. El switch `-SkipExternalConnectivity` permite que el ensayo sintético no contacte ni siquiera los endpoints de disponibilidad; no debe usarse en el preflight real del corte.

## Preparación de producción sin corte

1. Mantener Docker Desktop AMD64 con backend WSL2 e inicio con la sesión de Windows. Verificar `docker info` y `docker compose version` después de cada actualización.
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

### Respaldo cifrado y recuperación

El respaldo operativo usa PostgreSQL 17 (`postgres:17-alpine`) para ser compatible con Supabase PostgreSQL 17. El flujo de `pg_dump --format=custom` se cifra en marcos AES-256-GCM antes de escribirlo; solo se persisten `.j2mbackup` autenticados y su suma SHA-256. La clave de trabajo queda protegida con DPAPI CurrentUser y debe existir una segunda envoltura portable cifrada con una contraseña custodiada fuera del equipo. Ninguna de las dos se crea automáticamente.

Preparación, todavía sin activar tareas ni transferencias:

```powershell
python -m scripts.selfhost_backup init-key `
  --key-file "$env:LOCALAPPDATA\J2M\backup-key.dpapi" `
  --recovery-file "R:\J2M\backup-key-recovery.j2mbackup"
powershell -File scripts/install_selfhost_backup_tasks.ps1
```

`R:` representa una carpeta sincronizada del proveedor que el propietario autorice; no debe sustituirse por una carpeta local en C:. La contraseña se solicita interactivamente, nunca por argumento. Antes de usar `-Enable`, restringir ACL sobre la clave, autorizar el proveedor, configurar `rclone` sin secretos en Git y verificar la recuperación portable desde otro perfil/equipo.

Después de crear la clave, ejecutar `scripts/protect_selfhost_secrets.ps1` sobre el archivo DPAPI, la recuperación portable y el directorio de respaldos. La recuperación portable debe salir del equipo antes de considerar protegido el host; DPAPI por sí solo no cubre pérdida del perfil o del disco.

Al activarse explícitamente, las tareas independientes de Codex ejecutan:

- `run_selfhost_backup.ps1` cada hora: un solo `pg_dump`, tres intentos, bloqueo contra solapamientos, 48 copias locales cifradas y respaldo de `.env.selfhost` únicamente cuando cambia.
- `run_selfhost_cloud_copy.ps1` cada cuatro horas: usa `rclone copy`, nunca `sync`; conserva el prefijo de cuatro horas siete días y una copia diaria 30 días. Los borrados locales no se propagan. La eliminación por retención queda limitada a esos prefijos exclusivos.
- `run_selfhost_restore_test.ps1` semanalmente: restaura el último dump en PostgreSQL 17 con `--network none`, datos en `tmpfs`, ningún puerto y eliminación final del contenedor. Comprueba `alembic_version`; jamás apunta a producción.

Los resultados se registran sin URLs, contraseñas ni salida de `pg_dump`. Un fallo devuelve código distinto de cero para el historial del Programador de tareas; el monitoreo debe alertar si falta una ejecución horaria o una copia externa de cuatro horas. La activación requiere comprobar que la tarea S4U puede acceder a Docker Desktop y al almacén del proveedor aun sin sesión interactiva.

El 17 de noviembre de 2026 se comprobó desde la imagen aprobada, sin arrancar J2M, una conexión TLS de solo lectura a Supabase: `SELECT 1` respondió y `alembic_version` fue `9d0e1f2a3b4c`. No se ejecutó migración ni escritura. Un respaldo real y su restauración continúan bloqueados hasta autorizar la clave portable y el proveedor; las pruebas automatizadas y Docker utilizan exclusivamente datos sintéticos.

## Dominios, subdominios y prefijos

La aplicación ya genera URLs con `url_for` en la mayoría de los flujos y permite configurar el origen canónico mediante `PUBLIC_BASE_URL`, CORS y WebAuthn. Producción exige un origen HTTPS sin ruta; cookies seguras, RP ID y redirecciones dependen de ese origen. Por ello, dominios o subdominios distintos son configurables, pero montar toda la aplicación debajo de un prefijo inverso como `/lealtad` todavía **no está aprobado**: existen redirecciones absolutas `/admin/...`, rutas estáticas y contratos Wallet/WebAuthn que requieren una revisión coordinada. Las rutas actuales `/admin`, `/manager` y `/api` permanecen intactas. No configurar `StripPrefix`, `SCRIPT_NAME` ni reescrituras en Cloudflare hasta aprobar ese contrato.

La configuración preparada para el dominio aún no adquirido es: `PUBLIC_BASE_URL=https://lealtad.j2minteractive.com`, `CORS_ORIGINS` y `WEBAUTHN_ORIGIN` con ese mismo origen, `WEBAUTHN_RP_ID=lealtad.j2minteractive.com`, cookies seguras y `TRUSTED_PROXY_COUNT=1`. No debe copiarse al entorno activo, crear un túnel ni registrarse como origen Wallet/WebAuthn hasta que el propietario controle el dominio y el HTTPS público sea estable.

Si Railway suspende los servicios antes del corte, Supabase conserva los datos pero web, registro, escáner y worker dejan de estar disponibles. Railway no puede considerarse rollback mientras el servicio esté suspendido. El corte mínimo entonces exige dominio controlado, túnel estable, respaldo real restaurado en ensayo, host protegido y una ventana sin workers en paralelo. Ambos Wallet permanecen `disabled` localmente; no se promete emisión ni sincronización hasta una autorización posterior.

## Límites pendientes

- C: no usa BitLocker. La suspensión en corriente quedó deshabilitada; falta probar recuperación automática tras un reinicio real.
- No hay disco externo. La alternativa preparada es una copia cifrada en almacenamiento personal, pero faltan proveedor y custodia de la contraseña; no se realizó ninguna transferencia.
- Falta adquirir/controlar el dominio y crear el túnel; no existe token Cloudflare respaldado todavía.
- La conectividad TLS de solo lectura desde Docker hacia Supabase sí fue comprobada. Falta crear el primer respaldo real cifrado y restaurarlo aisladamente después de autorizar la custodia; no se probaron escrituras ni migraciones remotas.
- Un único equipo y una única conexión eléctrica/ISP no proporcionan alta disponibilidad. Se recomienda UPS, respaldo externo, monitoreo desde otra red y un segundo host antes de retirar Railway.

## Referencias operativas

- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/
- Token de túnel y variable `TUNNEL_TOKEN`: https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/
- Publicación de hostnames: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/
- Observabilidad y endpoint `/ready`: https://developers.cloudflare.com/tunnel/observability/
