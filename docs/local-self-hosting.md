# J2M Interactive en infraestructura local

La migración operativa desde Railway al host Windows quedó completada el 2026-11-19, sin eliminar los recursos remotos. Web y el worker local usan el PostgreSQL alojado en Supabase; el worker remoto fue retirado de ejecución para evitar procesamiento paralelo. El acceso público permanece pendiente de un dominio, pero la aplicación está disponible exclusivamente desde la laptop mediante HTTPS local. Los ensayos y las pruebas automatizadas continúan usando bases aisladas y nunca deben apuntar a producción.

## Estado operativo verificado

- Web, Google Wallet worker y proxy HTTPS local están saludables bajo Docker Desktop. Web y worker conservan `restart: unless-stopped`; el migrador no arranca automáticamente.
- El acceso local es `https://localhost:8443/staff-access`. El puerto HTTPS y el puerto directo de web están ligados únicamente a `127.0.0.1`, no a la LAN.
- El certificado individual de `localhost` está en `CurrentUser\TrustedPeople` y encadena a `J2M-Local-CA` en `CurrentUser\Root`. La clave privada de la CA permanece fuera del repositorio y de Docker, bajo `%LOCALAPPDATA%\J2M\local-https`, con herencia deshabilitada y acceso limitado al propietario, Administradores y SYSTEM. El volumen TLS del proxy contiene solamente el certificado y la clave individual del servidor. No modificar este material sin una rotación autorizada.
- `APPLE_WALLET_MODE=disabled` y `GOOGLE_WALLET_MODE=disabled`. La operación local no emite ni sincroniza pases.
- Railway web se conserva intacto mientras termina el periodo de prueba. El servicio del worker se conserva como configuración, pero no tiene un despliegue activo; no volver a iniciarlo mientras el worker local esté operativo.
- Las tareas de respaldo horario, copia cifrada a Google Drive cada cuatro horas y restauración aislada semanal están habilitadas y sus ejecuciones reales verificadas terminaron con código `0`. Healthchecks.io observa las tareas, pero no decide su resultado.
- Docker Desktop está configurado para iniciar con la sesión. La recuperación automática de Docker, web, worker y Nginx fue validada tras reinicios completos del host; las tareas esperan de forma acotada a que Docker esté disponible.
- La imagen que mantiene el stack local en ejecución está fijada como `j2m-interactive:e18f073bf479458fb03065810d1421d5cffe8373`. El pin se conserva en `.env.selfhost`; no depende de una variable temporal de PowerShell. Un despliegue posterior debe reemplazarlo únicamente por el SHA exacto de `origin/main` que se construya y apruebe.

## Estado y bloqueos del host

La reinspección del 2026-11-17 confirmó Windows, WSL 3.0.5 con Ubuntu como distribución WSL2, 32 GB de RAM, hipervisor activo y más de 116 GB libres en C:. Docker Desktop 4.99.0, Engine/CLI 29.9.3 y Compose v5.7.0 funcionan con el contexto `desktop-linux`. El named pipe está fuera del sandbox de Codex, por lo que los comandos automatizados requieren la autorización normal para ejecutarse en el contexto del usuario; no se reinició Codex ni Windows.

Windows Firewall y Microsoft Defender están habilitados, incluida la protección en tiempo real. C: quedó cifrada al 100 % con protección BitLocker activa y la recuperación posterior al reinicio fue validada el 2026-11-21; el propietario confirmó la custodia externa de la clave de recuperación. El plan `Turbo` evita suspender el equipo conectado a corriente; la suspensión en batería permanece en 240 segundos. Estos estados deben volver a comprobarse después de actualizaciones de Windows, firmware o fabricante.

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
8. Iniciar la versión exacta aprobada de `origin/main` con `selfhost_deploy_approved.ps1`; el script rechaza un SHA de PR o un árbol sucio, construye `j2m-interactive:<SHA>`, fija atómicamente esa etiqueta en `.env.selfhost` preservando su ACL y no ejecuta migraciones. Compose rechaza un entorno sin `J2M_IMAGE`, por lo que nunca vuelve silenciosamente a una etiqueta genérica.
9. Mantener Railway activo hasta validar HTTPS, cookies, CSRF, sesiones, registro, escáner y outboxes. El rollback consiste en retirar el hostname del túnel y volver a dirigir tráfico a Railway; no hacer downgrade ni eliminar tablas.

## Operación y seguridad

- Docker Desktop debe iniciar automáticamente; Compose recupera web, worker y túnel con `unless-stopped`. El migrador nunca arranca automáticamente.
- Vigilar `/api/health`, `/api/ready`, el estado del contenedor, `http://127.0.0.1:2000/ready`, espacio libre y cola del worker. `/api/ready` no demuestra que Alembic esté actualizado.
- Los logs rotan en Docker (`10 MB`, cinco archivos). No registrar cuerpos, cookies, tokens ni credenciales.
- GitHub Actions continúa validando PostgreSQL 16 y la imagen Linux. Solo se construye localmente un SHA exacto que sea el `origin/main` aprobado; nunca ejecutar imágenes provenientes de PRs no confiables.
- Para rollback, conservar al menos la imagen del SHA anterior aprobado y no aplicar downgrades destructivos. Restaurar base únicamente desde un respaldo probado y con autorización.

### Reconstrucción sin variables temporales

La recuperación normal después de un apagón solo requiere iniciar sesión, esperar a Docker Desktop y dejar que `restart: unless-stopped` recupere web, worker y HTTPS. Si web o worker fueron eliminados pero la imagen sigue presente, ejecutar desde el repositorio aprobado:

```powershell
powershell -File scripts/invoke_selfhost_compose.ps1 -EnvFile .env.selfhost config --quiet
powershell -File scripts/invoke_selfhost_compose.ps1 -EnvFile .env.selfhost up -d --no-build web google-wallet-worker
```

No establecer `$env:J2M_IMAGE` ni `$env:J2M_ENV_FILE`: el wrapper reemplaza valores heredados durante el subproceso, usa siempre el proyecto `j2m-cutover-ready` y restaura el entorno del operador al terminar. También bloquea el arranque si detecta un Google Wallet worker Compose activo bajo otro proyecto. `selfhost_preflight.ps1` exige el formato `j2m-interactive:<SHA completo>` y comprueba que la imagen exista localmente. Si la imagen se perdió, reconstruir primero ese SHA desde un checkout limpio y aprobado; después ejecutar `set_selfhost_image_pin.ps1 -ApprovedSha <SHA>` y finalmente el wrapper. No ejecutar el migrador durante una recuperación ordinaria.

`selfhost_deploy_approved.ps1` requiere que el pin anterior sea único y que su imagen todavía exista. Rechaza un SHA solicitado igual al pin activo para no reconstruir y sobrescribir la única imagen que serviría para rollback. Construye una versión distinta antes de cambiar el pin; la actualización del pin está dentro del bloque recuperable. Después espera los healthchecks de Compose durante un máximo de 120 segundos y verifica que el contenedor web use la imagen aprobada, que exista un único worker del proyecto operativo y que el proxy HTTPS esté en ejecución. Primero prueba `https://localhost:8443/api/health` y `/api/ready` sin tocar Nginx. Si la IP anterior quedó almacenada, valida `nginx -t`, realiza una recarga graciosa y reintenta de forma acotada; nunca reinicia el proxy si ya alcanza el backend nuevo. Una segunda comprobación TLS independiente exige 200/200 antes de declarar éxito. Si el pin, Compose, la recarga o HTTPS fallan, restaura el pin anterior, vuelve a aplicar web y worker con la imagen previa, recupera del mismo modo la resolución de Nginx y exige nuevamente 200/200. Si tampoco puede comprobar el rollback, informa un fallo crítico; nunca crea deliberadamente un segundo worker. El archivo temporal del pin recibe su ACL restrictiva cuando todavía está vacío y solo después se escriben las variables privadas.

La regresión Docker de 8E.1D usa nombres, red, certificados y contenedores sintéticos aislados. Fuerza una dirección IP distinta para el web reemplazado, comprueba que la recarga alcanza realmente el marcador del backend nuevo, simula un backend no saludable y confirma que el rollback vuelve a servir el marcador anterior por HTTPS. Antes y después compara los IDs de cualquier contenedor `j2m-cutover-ready`; no inicia, detiene ni recrea servicios operativos.

El proxy HTTPS se reconstruye con la imagen fijada por digest que documenta `LOCAL-RUNBOOK.txt`, la red `j2m-cutover-ready_j2m-private`, el usuario no-root `nginx`, `restart=unless-stopped`, `read_only`, `cap_drop=ALL`, `no-new-privileges`, el `nginx.conf` persistente y el volumen TLS de solo lectura. El volumen debe contener exclusivamente `server.crt` y `server.key`, propiedad de UID/GID 101; nunca montar `ca.key` dentro del contenedor. Antes de recrearlo, validar `nginx -t` en un contenedor efímero sin publicar puertos.

El ensayo de recuperación del 2026-11-20 creó web y worker como contenedores detenidos bajo un proyecto Compose temporal y los eliminó sin arrancarlos ni conectarlos a Supabase. También validó `nginx -t` con la imagen, usuario, red, configuración y volumen TLS reales. El ensayo confirmó que los tres contenedores pueden reconstruirse sin detener el stack activo; además detectó y corrigió la dependencia anterior de una variable temporal `J2M_IMAGE`.

### Respaldo cifrado y recuperación

El respaldo operativo usa PostgreSQL 17 (`postgres:17-alpine`) para ser compatible con Supabase PostgreSQL 17. El flujo de `pg_dump --format=custom` se cifra en marcos AES-256-GCM antes de escribirlo. Un terminador también autenticado conserva el número de marcos y la longitud total, por lo que la verificación rechaza truncamiento, eliminación, reordenamiento y tamaños inválidos aunque alguien recalcule el SHA-256 externo. Un manifiesto `.complete.json` se publica al final como marcador atómico; procesos de retención, copia y restauración ignoran cualquier archivo sin ese marcador. La clave binaria de trabajo queda protegida con DPAPI CurrentUser y debe existir una segunda envoltura portable cifrada con una contraseña custodiada fuera del equipo. Ninguna de las dos se crea automáticamente.

Preparación, todavía sin activar tareas ni transferencias:

```powershell
python -m scripts.selfhost_backup init-key `
  --key-file "$env:LOCALAPPDATA\J2M\backup-key.dpapi" `
  --recovery-file "R:\J2M\backup-key-recovery.j2mbackup"
powershell -File scripts/install_selfhost_backup_tasks.ps1
```

En otro perfil Windows o equipo, recuperar sin sustituir una clave existente:

```powershell
python -m scripts.selfhost_backup recover-key `
  --recovery-file "R:\J2M\backup-key-recovery.j2mbackup" `
  --key-file "$env:LOCALAPPDATA\J2M\backup-key.dpapi"

python -m scripts.selfhost_backup restore-configuration `
  "R:\J2M\configuration\j2m-configuration-AAAA.zip.j2mbackup" `
  --destination "$env:LOCALAPPDATA\J2M\recovered-configuration" `
  --key-file "$env:LOCALAPPDATA\J2M\backup-key.dpapi"

powershell -File scripts/protect_selfhost_secrets.ps1 `
  -Path "$env:LOCALAPPDATA\J2M\backup-key.dpapi","$env:LOCALAPPDATA\J2M\recovered-configuration"
```

La contraseña se solicita de forma interactiva. La operación crea una nueva envoltura DPAPI para el perfil actual, verifica que descifra los mismos 32 bytes binarios y falla si el destino ya existe. La restauración de configuración exige archivo cifrado, SHA-256 y manifiesto válidos, rechaza rutas o enlaces inseguros dentro del ZIP y escribe únicamente en un directorio nuevo. Después se protegen las ACL y se comparan los nombres requeridos con `.env.selfhost.example`; no se imprimen sus valores.

`R:` representa una carpeta sincronizada del proveedor que el propietario autorice; no debe sustituirse por una carpeta local en C:. La contraseña se solicita interactivamente, nunca por argumento. Antes de usar `-Enable`, restringir ACL sobre la clave, autorizar el proveedor, configurar `rclone` sin secretos en Git y verificar la recuperación portable desde otro perfil/equipo.

Después de crear la clave, ejecutar `scripts/protect_selfhost_secrets.ps1` sobre el archivo DPAPI, la recuperación portable y el directorio de respaldos. La recuperación portable debe salir del equipo antes de considerar protegido el host; DPAPI por sí solo no cubre pérdida del perfil o del disco.

Al activarse explícitamente, las tareas independientes de Codex ejecutan:

- `run_selfhost_backup.ps1` cada hora: un solo `pg_dump`, tres intentos, límite temporal, bloqueo de sistema operativo contra solapamientos, 48 copias locales cifradas y respaldo de configuración únicamente cuando cambia. Siempre conserva al menos el último respaldo completo de base y configuración.
- `run_selfhost_cloud_copy.ps1` cada cuatro horas: prepara exactamente el respaldo completo más reciente, lo verifica con SHA-256, manifiesto y AES-GCM, y usa `rclone copy`, nunca `sync`. Después vuelve a descargar el archivo cifrado y repite la verificación completa, incluso cuando el backend no ofrece hashes compatibles. La retención genera una lista exacta por cada categoría y ejecuta `rclone deletefile` solamente sobre esos objetos; no combina filtros `--include`/`--exclude`. Cuatro horas, diarios y configuración protegen de forma independiente su conjunto completo más reciente, aunque sea más antiguo que la ventana.
- `run_selfhost_restore_test.ps1` semanalmente: restaura el último dump en PostgreSQL 17 con `--network none`, datos en `tmpfs`, ningún puerto y eliminación final del contenedor. La lista TOC se genera solo dentro del contenedor; omite la extensión administrada `supabase_vault` y su esquema `vault`, que no existen en PostgreSQL oficial, sin persistir el dump descifrado. Exige el head Alembic esperado, tablas representativas, recuentos legibles, ausencia de clientes huérfanos y saldos no negativos; jamás apunta a producción.

### Alertas externas de respaldos

Los tres runners pueden notificar a Healthchecks.io sin convertir el servicio de monitoreo en una dependencia del respaldo. El archivo privado `%LOCALAPPDATA%\J2M\healthchecks.json` contiene exclusivamente las URL de ping de `database_hourly`, `cloud_four_hour` y `restore_weekly`; debe protegerse con `protect_selfhost_secrets.ps1` y nunca guardarse en el repositorio. Cada runner envía `start`, luego `success` solamente tras completar sus verificaciones, o `fail` desde su manejo de errores. Los timeouts, indisponibilidad o errores de Healthchecks.io se ignoran de forma segura y no cambian el resultado de la operación principal.

El plan Hobbyist es suficiente para estas tres comprobaciones. Configuración recomendada: una hora más 20 minutos de gracia para el respaldo local, cuatro horas más 45 minutos para la copia externa y siete días más cuatro horas para la restauración. Las URL de ping son secretos: no deben aparecer en argumentos de tareas, logs, capturas ni documentación.

La integración operativa se validó con un cuarto check sintético independiente: se recibió el aviso de caída y el de recuperación sin interrumpir ninguna tarea real. Los identificadores comprometidos durante la configuración se revocaron y el archivo activo quedó protegido por ACL. Las tareas reales se ejecutaron secuencialmente con resultado `0`.

Los tres runners adquieren el mismo mutex nombrado antes de iniciar cualquier trabajo y lo mantienen hasta terminar. Si una tarea coincide con otra, espera de forma acotada en vez de acceder simultáneamente a respaldos, retención o restauración. El bloqueo se libera también ante excepciones y puede recuperarse si el proceso propietario terminó abruptamente. `MultipleInstances IgnoreNew` sigue evitando duplicados de la misma tarea, mientras el mutex coordina tareas diferentes. El envío a Healthchecks se ejecuta mediante un wrapper de mejor esfuerzo que descarta su salida, captura fallos de proceso o red y restaura el código de salida anterior; una indisponibilidad del monitor no convierte un respaldo correcto en fallo ni un respaldo fallido en éxito.

La Fase 8E.1B conserva las frecuencias instaladas y no reinstala tareas. El interruptor privado
`J2M_MEDIA_BACKUP_ENABLED` permanece ausente o `false` hasta una activación posterior. Cuando
sea aprobado, el respaldo horario cambia de un componente PostgreSQL legado a un conjunto
integral DB+medios. Copia y restauración seleccionan estrictamente el modo configurado y exigen
un conjunto autenticado reciente; nunca hacen fallback silencioso a componentes o dumps
legados. La retención protege por separado los últimos conjuntos integrales, dumps legados y
configuraciones, de modo que cambiar el interruptor no destruye respaldos anteriores.

El respaldo horario y la restauración semanal comprueban el Docker Engine después de adquirir el mutex y de notificar `start` a Healthchecks. Si Docker Desktop todavía está arrancando después del inicio de sesión, inician una comprobación cada 30 segundos durante un máximo global de cinco minutos. Cada `docker info` se ejecuta en un proceso CLI desechable con un presupuesto total de 10 segundos, que reserva hasta dos segundos para terminar y recoger el propio proceso de comprobación mediante una espera acotada. Si se bloquea, se termina únicamente ese proceso, nunca Docker Desktop, su backend, el engine ni los contenedores; ni la consulta ni su limpieza pueden extender el presupuesto global. La espera conserva el mismo bloqueo global, por lo que otra tarea no puede iniciar un respaldo, copia o restauración concurrente; `MultipleInstances IgnoreNew` evita además duplicar la misma tarea. Los eventos `docker_unavailable`, `docker_probe_timeout`, `docker_recovered` y `docker_timeout` se escriben sin secretos en `selfhost-backups/logs/task-runtime.jsonl`. Una recuperación continúa con la operación normal y solo entonces puede emitir `success`; un timeout global pasa por el manejo de errores existente, conserva el marcador de fallo y emite `fail`. La copia de Google Drive no requiere Docker y no realiza esta espera.

Los resultados se registran sin URLs, contraseñas ni salida de `pg_dump`. El contenedor de `pg_dump` recibe solamente `DATABASE_URL` como secreto. Para Supabase, `DATABASE_SSL_ROOT_CERT` apunta en el host al certificado público descargado desde Database Settings; el script valida que sea un archivo regular, lo monta de solo lectura y configura dentro del contenedor `sslmode=verify-full` con esa CA. También comprueba que el almacén CA del contenedor exista antes de conectar. Así valida TLS, cadena de certificado y hostname sin relajar seguridad ni copiar la CA al repositorio o a la imagen. Un fallo devuelve código distinto de cero para el historial del Programador de tareas; el monitoreo debe alertar si falta una ejecución horaria o una copia externa de cuatro horas.

No se usa S4U. Microsoft documenta que S4U no almacena contraseña, pero tampoco ofrece acceso a red ni archivos cifrados. Docker Desktop, DPAPI CurrentUser y `rclone` requieren el token de la sesión del propietario, así que las tareas se preparan con `InteractiveToken`: no guardan la contraseña de Windows, son independientes de Codex y solo ejecutan mientras el propietario tenga una sesión iniciada. No se considera probado el arranque sin login. Para operación totalmente desatendida haría falta migrar Docker a un servicio compatible y rediseñar la custodia de claves; queda fuera de este corte urgente.

El 17 de noviembre de 2026 se comprobó desde la imagen aprobada una conexión TLS de solo lectura a Supabase: `SELECT 1` respondió y `alembic_version` fue `9d0e1f2a3b4c`. Posteriormente se generó el primer respaldo real cifrado, se restauró en PostgreSQL 17 aislado y se verificó nuevamente tras descargarlo desde Google Drive. La clave portable y su recuperación quedaron verificadas sin exponer material secreto. Las tareas automáticas usan la misma cadena autenticada y los ensayos de código siguen utilizando exclusivamente datos sintéticos.

## Dominios, subdominios y prefijos

La aplicación ya genera URLs con `url_for` en la mayoría de los flujos y permite configurar el origen canónico mediante `PUBLIC_BASE_URL`, CORS y WebAuthn. Producción exige un origen HTTPS sin ruta; cookies seguras, RP ID y redirecciones dependen de ese origen. Por ello, dominios o subdominios distintos son configurables, pero montar toda la aplicación debajo de un prefijo inverso como `/lealtad` todavía **no está aprobado**: existen redirecciones absolutas `/admin/...`, rutas estáticas y contratos Wallet/WebAuthn que requieren una revisión coordinada. Las rutas actuales `/admin`, `/manager` y `/api` permanecen intactas. No configurar `StripPrefix`, `SCRIPT_NAME` ni reescrituras en Cloudflare hasta aprobar ese contrato.

La configuración preparada para el dominio aún no adquirido es: `PUBLIC_BASE_URL=https://lealtad.j2minteractive.com`, `CORS_ORIGINS` y `WEBAUTHN_ORIGIN` con ese mismo origen, `WEBAUTHN_RP_ID=lealtad.j2minteractive.com`, cookies seguras y `TRUSTED_PROXY_COUNT=1`. No debe copiarse al entorno activo, crear un túnel ni registrarse como origen Wallet/WebAuthn hasta que el propietario controle el dominio y el HTTPS público sea estable.

Si Railway suspende sus servicios, la operación local y Supabase continúan funcionando, pero el acceso público de Railway deja de estar disponible. Railway no puede considerarse rollback mientras esté suspendido. El worker local es el único procesador activo y ambos Wallet permanecen `disabled`; no se promete emisión ni sincronización hasta una autorización posterior. El futuro acceso público requiere dominio controlado, HTTPS estable y una validación específica antes de cambiar tráfico.

## Límites pendientes

- BitLocker y la recuperación automática tras reinicio están verificados. Sigue pendiente probar el comportamiento ante un apagón real y añadir continuidad eléctrica mediante UPS antes de considerar el host tolerante a cortes físicos.
- No hay disco externo. Google Drive personal conserva copias cifradas y verificadas con una clave recuperable de forma portable, pero sigue siendo recomendable una copia física independiente.
- Falta adquirir/controlar el dominio y crear el túnel; no existe token Cloudflare respaldado todavía.
- La conectividad TLS, el respaldo real cifrado, la descarga externa y la restauración aislada fueron comprobados. No se ejecutaron migraciones remotas durante esas verificaciones.
- Un único equipo y una única conexión eléctrica/ISP no proporcionan alta disponibilidad. Se recomienda UPS, respaldo externo, monitoreo desde otra red y un segundo host antes de retirar Railway.

## Referencias operativas

- Cloudflare Tunnel: https://developers.cloudflare.com/tunnel/
- Token de túnel y variable `TUNNEL_TOKEN`: https://developers.cloudflare.com/tunnel/reference/tunnel-tokens/
- Publicación de hostnames: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/
- Observabilidad y endpoint `/ready`: https://developers.cloudflare.com/tunnel/observability/
- Microsoft Task Scheduler, tipos de inicio de sesión y limitaciones S4U: https://learn.microsoft.com/windows/win32/taskschd/taskschedulerschema-logontype-principaltype-element
- `New-ScheduledTaskPrincipal`: https://learn.microsoft.com/powershell/module/scheduledtasks/new-scheduledtaskprincipal
