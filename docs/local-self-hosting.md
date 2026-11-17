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
- `.env.selfhost.example`: inventario fail-closed de producción, sin secretos.
- `.env.selfhost.test.example`: valores públicos desechables para la base temporal; nunca usar con Supabase.
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

En Docker Desktop para Windows, una red marcada `internal` conserva en la configuración el binding `127.0.0.1:8000`, pero no lo materializó en el host durante este ensayo. Para no añadir una segunda red con salida, las solicitudes HTTP se ejecutaron dentro de web contra `127.0.0.1:8000`. El Compose base, sin el overlay aislado, mantiene el binding exclusivo a loopback; debe probarse nuevamente antes del corte.

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

## Dominios, subdominios y prefijos

La aplicación ya genera URLs con `url_for` en la mayoría de los flujos y permite configurar el origen canónico mediante `PUBLIC_BASE_URL`, CORS y WebAuthn. Producción exige un origen HTTPS sin ruta; cookies seguras, RP ID y redirecciones dependen de ese origen. Por ello, dominios o subdominios distintos son configurables, pero montar toda la aplicación debajo de un prefijo inverso como `/lealtad` todavía **no está aprobado**: existen redirecciones absolutas `/admin/...`, rutas estáticas y contratos Wallet/WebAuthn que requieren una revisión coordinada. Las rutas actuales `/admin`, `/manager` y `/api` permanecen intactas. No configurar `StripPrefix`, `SCRIPT_NAME` ni reescrituras en Cloudflare hasta aprobar ese contrato.

## Límites pendientes

- C: no usa BitLocker. La suspensión en corriente quedó deshabilitada; falta probar recuperación automática tras un reinicio real.
- No había un disco o memoria externa conectado durante el ensayo; la copia portable independiente de DPAPI continúa pendiente y no debe guardarse como sustituto en C:.
- Falta adquirir/controlar el dominio y crear el túnel; no existe token Cloudflare respaldado todavía.
- No se probó conectividad real desde contenedores hacia Supabase porque el ensayo la bloquea deliberadamente. Tampoco se probó una restauración de respaldo PostgreSQL.
- Un único equipo y una única conexión eléctrica/ISP no proporcionan alta disponibilidad. Se recomienda UPS, respaldo externo, monitoreo desde otra red y un segundo host antes de retirar Railway.

## Referencias operativas

- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/
- Token de túnel y variable `TUNNEL_TOKEN`: https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/
- Publicación de hostnames: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/
- Observabilidad y endpoint `/ready`: https://developers.cloudflare.com/tunnel/observability/
