# Inventario operativo de transición (preparación parcial)

Este comando solo observa el checkout local, Docker y archivos de respaldo.
No contacta Supabase ni Google, no ejecuta Docker Compose, no cambia el pin
y no autoriza la transición. Ejecutarlo desde un checkout limpio y actualizado
después de fusionar el código, con la configuración privada autorizada:

    python -m scripts.selfhost_transition_preflight --approved-sha <SHA-main> --env-file .env.selfhost --backup-dir selfhost-backups/hourly --key-file <ruta-DPAPI-privada>

El SHA debe coincidir con HEAD y origin/main **local**. Confirmar el remoto y
su CI por separado. El inventario compara el tag legado y su ID inmutable,
las imágenes activas, la red, los modos Wallet, los puertos loopback y la
exclusividad del worker local por etiquetas. No demuestra ausencia de
escritores externos, ni prueba identidad HTTPS o permisos efectivos.

El conjunto integral candidato se selecciona por fecha de archivo; si no
autentica, se bloquea sin sustituirlo silenciosamente por uno antiguo.
La antigüedad (máximo 90 minutos) se comprueba contra el índice cifrado,
no contra mtime. Se verifica el índice y los dos componentes mediante
verify_integral_set. Se informa el hash del índice exacto. No es una prueba
de la revisión Alembic del dump ni de una restauración integral reciente.
El inventario no adquiere el bloqueo de respaldo: repetir la verificación
bajo exclusión durante la ventana futura.

Salida 1: alguna observación falló. Salida 2: observaciones completadas,
**NO-GO operativo**. El campo transition_authorized siempre es false.
No se imprime el contenido de variables privadas, respuestas Docker ni
rutas de archivos privados.

## Acciones aún necesarias

- Autorizar por separado restaurar el conjunto exacto en Docker aislado
  con restore-integral, bajo el bloqueo de respaldo existente, comprobar
  su hash antes y después, revisión y medios, sin restaurar sobre Supabase.
- Confirmar afinidad efectiva del pooler mediante sonda autorizada.
- Demostrar exclusión de escritores locales y externos y migradores con
  una barrera de acceso efectiva; la inspección local no basta. Coordinar
  tareas programadas y mantener mantenimiento tras una interrupción.
- Integrar atestación HTTPS, respaldo/restauración, barrera y diario en
  el flujo operativo antes de habilitar cualquier comando de transición.
  Mantener el bloqueo general de la imagen legada. No se autoriza una
  excepción mediante un JSON o un argumento genérico.


## Sonda de afinidad independiente (no ejecutada)

El propietario debe confirmar primero que la URL privada corresponde al
Session pooler (puerto 5432), el hostname exacto y la referencia del
proyecto, y autorizar expresamente dos conexiones TLS y un lock de
diagnóstico de sesión. No ejecutar como parte del preflight.

    python -m scripts.probe_authorized_pooler --env-file .env.selfhost --expected-host <hostname-session-pooler> --expected-project-ref <project-ref> --authorize-session-lock-probe

Se rechazan otros destinos, puertos, parámetros de conexión inesperados
y variables PG heredadas. Se exige CA local y verify-full. Se usa una
clave bigint negativa aleatoria, no los namespaces de dos enteros de
la aplicación. No se escriben tablas. Se comprueban los dos PID tras
COMMIT, exclusión y liberación. En caso de pérdida de afinidad, error
de transporte o liberación no confirmada, se cierran las conexiones y
se informa NO-GO; un lock de diagnóstico podría permanecer en una sesión
del pooler hasta que esta termine. No se matan sesiones ni se libera
ninguna otra clave. El resultado positivo no autoriza el despliegue.


## Restauración exacta independiente (no ejecutada)

Después de obtener el hash del índice y el ID inmutable del legado en
el inventario, solicitar autorización explícita para restaurar esa copia
real en una red y volumen Docker aislados. Reutiliza restore_integral_isolated,
con el bloqueo de respaldo existente y el índice exacto; no usa una
conexión a Supabase. La restauración ejecuta la imagen por ID inmutable,
verifica el índice y componentes bajo el bloqueo y exige la revisión
inicial. Revalida el conjunto y el tag al finalizar.

    python -m scripts.verify_transition_restore --index <archivo-integral-exacto> --key-file <ruta-DPAPI-privada> --expected-index-sha256 <hash> --expected-legacy-image-id <sha256:id> --expected-postgres-image-id <sha256:id> --authorize-isolated-restore

No ejecutar sin aprobación expresa ni utilizar el resultado como permiso
de transición. La evidencia no se conserva automáticamente; no acredita
una barrera de escritores ni el estado de la base después de la copia.

## Atestación HTTPS de solo lectura

Añada `--attest-legacy-https` al preflight para comprobar health y ready
en el origen local por TLS verificado. La comprobación requiere que Nginx
registre en stdout el nonce de `X-J2M-Probe`, `$uri`,
`$upstream_addr` y `$upstream_status` en ese orden. Verifica las identidades
de web, worker y proxy y sus IP antes y después de cada petición, y
rechaza una respuesta sin un registro único correlacionado. Si los logs
operativos aún no exponen el formato, la comprobación queda bloqueada.
Este comando no recarga ni reconfigura Nginx.

## Barrera operativa requerida

La futura ventana exige un control mantenido en PostgreSQL que impida
reconectar o escribir a los roles usados por web, worker y otros clientes,
incluidos los procesos que compartan su credencial. Use una identidad de
migración separada y pruebe que una sesión nueva con un rol escritor queda
denegada durante toda la ventana. Si la identidad operativa compartida
también es la única que puede migrar, separe primero esas identidades bajo
una autorización específica. Identifique los consumidores externos y el
comportamiento de las conexiones ya abiertas antes de aplicar el control.

Tras autorizar la ventana, bloquee el ingreso HTTP y directo, desactive el
reinicio de escritores, suspenda tareas y drene transacciones. Mantenga
la barrera hasta comprobar revisión, esquema, datos, identidad HTTPS y una
aprobación separada de reapertura. Use el bloqueo compartido de respaldo
para que el conjunto no compita con copia, restauración ni retención.
Si la exclusión no se puede demostrar, no hay migración. Una inspección
puntual de Docker, del Programador o de pg_stat_activity no acredita
esa barrera. El procedimiento desplegable aún debe integrar la barrera,
el diario y la recuperación con comprobaciones en vivo.

## Hallazgos de la comprobación autorizada

El 26 de noviembre de 2026, se cotejaron en memoria las variables locales y
los respaldos DPAPI de Railway. El web, el worker y el migrador históricos
usaban el mismo usuario y contraseña de base que la configuración local.
El preflight acredita que web y worker locales usan esa misma conexión.
Esto no prueba el estado actual de servicios externos. Debe verificarse
por separado qué rol usa la tarea efectiva de respaldo y cada consumidor
externo. No se puede bloquear la identidad compartida y seguir migrando
con ella. Tampoco se debe revocar un rol administrado sin un plan aprobado.

Para diseñar la barrera, solicite una inspección de solo lectura de
`pg_roles` (propiedades y capacidad de login), `pg_auth_members` (membresías),
privilegios efectivos sobre esquema/tablas y agregados por rol en
`pg_stat_activity` (sin texto SQL, IP ni datos personales). Coteje localmente
la identidad de conexión usada por cada tarea y confirme por separado si
Railway conserva escritores. Separe primero escritores y migrador con
roles y permisos mínimos. La pausa debe impedir conexiones nuevas de
escritores y drenar las existentes, incluidas las persistentes del pooler.
Hasta demostrar ambas condiciones, no existe una barrera efectiva.

### Nginx

`selfhost-legacy-attestation.nginx.conf` contiene el formato candidato.
Instale el mapa y formato en `http` y añada el access_log condicional
al servidor HTTPS. Mantenga el registro existente. Valide el archivo con
`nginx -t`, conserve la configuración anterior para recuperación,
recargue de forma graciosa solo con autorización y ejecute el preflight de
atestación. Si no se vincula el nonce al upstream en ambas rutas, restaure
la configuración original y valide antes de recargar. El log adicional usa
la ruta sin query y carece de cookies, cuerpos y tokens.

## Inventario operativo del 26 de noviembre

La configuración candidata se aplicó con recarga graciosa. La atestación
por nonce acreditó health y ready. La copia exacta anterior queda protegida
por ACL para recuperación. Web y worker no se reiniciaron.

La inspección de solo lectura observó que el login compartido es propietario
de las 42 tablas y 30 secuencias de `public`, puede escribir en las 42 tablas,
puede omitir RLS y tiene nueve membresías directas que puede asumir. Railway
mostraba los tres servicios fuera de línea, sin trabajos pendientes. Eso no
impide nuevos despliegues o reconexiones. La instantánea de actividad no
acredita una barrera y no se modificó ningún rol.

Separe un rol propietario sin login y una identidad de migración autorizada
para asumirlo. Web y worker deben tener logins distintos sin propiedad,
BYPASSRLS ni membresías administrativas, limitados a tablas, funciones y
secuencias que sus flujos usan. El respaldo debe usar una identidad
separada de lectura con cobertura comprobada de las tablas con RLS y sus
secuencias. La administración y recuperación se mantienen separadas. Antes
de autorizar ese cambio, ensaye las políticas y permisos con los cuatro roles
en una copia sintética. El acceso completo de respaldo requiere un diseño
explícito compatible con RLS, sin conceder escritura.

La barrera futura debe bloquear nuevos logins de escritores, impedir
suplantación por membresías y coordinar el drenaje de sesiones ya abiertas
a través del pooler. Debe conservar al migrador y registrar el estado de
roles, permisos y servicios para una reversión aprobada. Compruebe intentos
directos y por pooler con cada escritor mientras la barrera está activa.
No dependa de un filtro HTTP, del estado offline de Railway, ni de una
captura única de sesiones. No ejecute cambios de privilegios en el rol
administrado compartido sin una revisión específica.

### Restauración

Las tres tareas apuntan al checkout operativo `dae140e`. Este espera
Alembic `9d0e1f2a3b4c`, usa PostgreSQL 17 y valida el snapshot antes de
publicar un conjunto. El checkout actualizado espera `c3d4e5f6a7b8` y el
verificador de transición exige explícitamente el head legado. Eso explica
la diferencia entre contratos de revisión, pero no el fallo previo dentro
de `pg_restore`. No hay diagnóstico histórico suficiente para atribuirlo a
extensiones, roles, RLS, dependencias o recursos.

El nuevo proceso drena errores concurrentemente y conserva solo categorías.
Mantiene `--exit-on-error` y limita el filtro de TOC al proveedor
`supabase_vault` y su comentario exacto. Se conserva la exclusión
establecida del esquema `vault`. Cualquier dependencia nueva debe
reproducirse y resolverse en aislamiento. No excluya más objetos por
coincidencia de nombre ni acepte una restauración parcial.

## Separación de roles y respaldo

`python tests/postgres_role_separation_ci.py` ensaya cinco roles sobre
PostgreSQL sin red. Comprueba políticas por tenant, permisos de secuencia,
respaldo mediante `pg_dump --enable-row-security` y comparación del contenido
restaurado con la fuente. La política SELECT de respaldo se concede
exclusivamente a esa identidad. Con RLS, `--enable-row-security` por sí solo
puede omitir filas, por lo que se requiere una comparación exhaustiva y una
política explícita por objeto. El propietario no tiene login, y el migrador
puede asumirlo sin entregar ese privilegio a los escritores.

El ensayo combina NOLOGIN, revocación de DML y secuencia, y una barrera de
bloqueo exclusivo para drenar transacciones. Prueba el rechazo a nuevas
conexiones y el rechazo de DML desde una conexión que ya existía. La barrera
queda aplicada al confirmar la transacción. El ensayo no reabre escritores.
Su modelo de una tabla no sustituye el inventario de grants, funciones,
políticas, trabajos y caminos efectivos de la aplicación completa. La
transición no puede depender de trasladar este SQL ficticio a Supabase.
La reapertura y la implementación de la barrera real requieren autorización.

Referencias: [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html),
[RLS de PostgreSQL](https://www.postgresql.org/docs/current/ddl-rowsecurity.html) y
[roles de Supabase](https://supabase.com/docs/guides/database/postgres/roles).

## Matriz propuesta para la separación de identidades

El ensayo de esquema completo crea ambos heads desde las migraciones de J2M.
Incluye dos negocios, movimientos sintéticos, secuencias y restauraciones
estrictas. Las concesiones del fixture se aplican exclusivamente a la base
desechable. No representan una lista aprobada para Supabase.

| Identidad | Alcance propuesto | Restricciones |
| --- | --- | --- |
| Propietario J2M | Propiedad de las tablas, secuencias, funciones y triggers enumerados en el manifiesto de migraciones J2M. Puede cambiar RLS. | NOLOGIN. Excluir esquemas administrados y objetos de extensiones. |
| Migrador | CONNECT y membresía con SET ROLE hacia el propietario. DDL y cambios de esquema solo durante la ventana. | NOINHERIT. Sin credencial compartida con servicios. |
| Web | SELECT y DML por flujo sobre negocios, clientes, staff, sesiones, passkeys, recuperación, movimientos, ledger, referidos y auditoría. USAGE en las secuencias de tablas que inserta. | Sin propiedad, BYPASSRLS, CREATEROLE, TRUNCATE ni SET ROLE al propietario. Revisar por separado el acceso administrativo, los outboxes que encola y cada función ejecutable. |
| Worker | Leer el contexto necesario de negocios y clientes. Operaciones por estado en outboxes, reservas, trabajos de publicación y cola de referidos. | Sin DML en saldos, ledger, credenciales o sesiones. Sin propiedad ni membresía privilegiada. |
| Respaldo | CONNECT, USAGE de esquema, SELECT de cada tabla y secuencia enumeradas. Política SELECT explícita donde RLS filtra filas. | Sin permisos efectivos de escritura, secuencias de escritura o roles privilegiados. Comparar filas, estructura y posición de secuencias. |
| Administración | Gestión aprobada de roles, mantenimiento y recuperación. | Credencial separada fuera de web, worker, migrador y tareas. |

Los privilegios predeterminados del propietario deben retirar EXECUTE de
PUBLIC para funciones nuevas que lo hereden. No conceder DML futuro por
defecto. Cada migración deberá declarar y probar grants, RLS y permisos de
secuencia correspondientes. `pg_dump --no-acl --no-owner` compara contenido
y estructura, pero la recuperación de la configuración de roles exige un
manifiesto de permisos independiente.

Los logins de web y worker sirven a todos los negocios. Un filtro fijo
por tenant o un GUC que el mismo login pueda cambiar no acredita aislamiento
en PostgreSQL. El contexto debe derivarse de la sesión autorizada o del
trabajo reclamado y aplicarse por transacción, con rollback y limpieza en el
pool. El modelo actual requiere demostrar en cada flujo si esa autorización
queda en la aplicación o se vincula a una identidad verificable por la base.
Las tablas protegidas de reservas y cola necesitan políticas explícitas para
el rol que ejecuta cada operación. Esta parte del diseño no está certificada.

### Paso previo a cualquier cambio de credenciales

1. Revisar el manifiesto exacto de objetos y grants por flujo en una copia
   sintética. Detener si un flujo autorizado falla o un rol alcanza otro
   tenant, la propiedad o una función de escritura ajena.
2. Solicitar solo el aprovisionamiento de nuevas identidades y grants
   aprobados. Mantener una copia protegida de la configuración anterior.
   Cambiar web, worker y respaldo de forma separada y verificar reautenticación,
   la cobertura del dump y el acceso al pooler de cada rol.
3. Aprobar por separado la invalidación de la credencial histórica compartida.
   Identificar y desconectar sus consumidores, incluido Railway, sin
   modificar roles administrados por anticipado. Mantener un camino
   administrativo de recuperación.
4. Solicitar aparte la ventana de migración, con respaldo reciente probado,
   escritores bloqueados y drenados, migrador independiente, diario y
   verificación previo a reabrir. Si falla la barrera o la prueba de integridad,
   conservar mantenimiento, sin downgrade ni pérdida de movimientos.

El comando de restauración de transición requiere
`--expected-postgres-image-id sha256:<id>`. Verifica la imagen inmutable y la
versión 17 del contenedor real. La restauración programada conserva su
interfaz anterior. El verificador estricto de transición exige baseline,
cadena de balances, correspondencia con movimientos y secuencias sanas.

## Resultado del ensayo de permisos acotados

El manifiesto ejecutado está en `tests/postgres_scoped_grants_fixture.py`.
En el esquema objetivo, el candidato web puede leer negocios y clientes
y tiene los permisos probados para las tablas de movimientos y ledger.
El candidato worker carece de permisos para cambiar saldos. Ninguno
puede asumir al propietario. El GRANT de lectura sobre la cola de
referidos no basta: la consulta del worker devuelve cero filas bajo RLS.
Hasta definir y probar las políticas y los flujos de autorización que faltan,
esos roles no deben sustituir al login operativo.

La exclusión sintética utiliza una clave transaccional de migrador y verifica
que otra sesión no puede adquirirla. El bloqueo por privilegios del
esquema sintético revoca grants directos y bloquea reconexiones. El ensayo
independiente de sesión persistente verifica los verbos de escritura sobre
una tabla representativa. Aún deben revisarse sus equivalentes en el
inventario real de funciones, membresías, Data API, propietarios y pooler.

El procedimiento operativo no se habilita por un informe de este ensayo.
El bloqueo del legado se mantiene. La disponibilidad de un respaldo
certificado y una atestación anterior no sustituye validar la barrera en
vivo antes de migrar. Mantenga separadas las futuras aprobaciones de
aprovisionamiento de roles, cambio de servicios y migración.

## Permisos operativos de la cola de referidos

`scripts/j2m_wallet_queue_grants.sql` es un manifiesto de aprovisionamiento
para el propietario del esquema después de migrar y crear los roles internos.
El ensayo usa `j2m_schema_web` y `j2m_schema_worker` sin propiedad, membresía
del propietario ni BYPASSRLS. La instalación productiva sigue pendiente de
aprobar el manifiesto completo y comprobar el pooler.

| Recurso | Web | Worker |
| --- | --- | --- |
| Cola de referidos | SELECT, INSERT inicial y uso de secuencia | SELECT, INSERT inicial, uso de secuencia y UPDATE de campos de procesamiento |
| Participantes | Sin cambios en este manifiesto | SELECT para expansión |
| Reservas | SELECT, INSERT inicial y UPDATE de campos de transición | Igual que web |

Los servicios atienden varios negocios. RLS limita el acceso por identidad y
operación. Los filtros de Flask, las claves foráneas compuestas y las
comprobaciones del servicio preservan el tenant de cada trabajo. Estas
políticas no aíslan por tenant a un operador con acceso SQL directo al rol
interno. La matriz cubre la cola y las reservas, y aún falta la matriz completa
para autenticación, registro, lealtad y demás flujos.

La barrera sintética prueba revocación de permisos de columna y de tabla,
secuencias y SET ROLE desde una conexión ya abierta. La prueba de bloqueo de
migradores conserva la sesión a través de varios commits. Sigue pendiente
integrar la barrera completa, incluir privilegios heredados y funciones
operativas, y verificar drenaje y recuperación en el pooler.

El verificador contable conserva el rechazo estricto de movimientos sin
evidencia. Para datos anteriores al ledger incorporado por c0d1e2f3a4b5,
la transición requiere un baseline autenticado que distinga el historial sin
backfill de movimientos posteriores. No se ha validado ese límite sobre el
respaldo operativo.

La reaplicación del manifiesto acepta políticas idénticas y rechaza
definiciones distintas en una sola transacción. No reemplaza políticas
existentes. Antes de conceder permisos rechaza identidades internas con
membresías, propiedad de relaciones en public o atributos de elevación.
La validación cubre las políticas nombradas por este manifiesto. La
revisión de ACL heredadas, funciones, privilegios por defecto y políticas
adicionales sigue pendiente antes de aplicarlo en operación.

### Autenticación web de gerentes

El manifiesto complementario `scripts/j2m_staff_login_grants.sql` habilita
la consulta de personal y negocio, la creación y revocación de sesiones
por columna y las operaciones del rate limiter de autenticación. El ensayo
HTTP usa dos negocios y verifica que cada gerente pueda acceder solo a su
perfil, que las credenciales del otro negocio no abran sesión y que el
cierre produzca una revocación persistida. Las operaciones de
Super Admin, caja, registro, recuperación, passkeys y auditoría requieren
sus propias validaciones y permisos antes de autorizar el aprovisionamiento.

El ensayo incluye el acceso histórico de cajeros sin correo, el permiso
`scanner_operate_web`, el rechazo de un negocio incorrecto, el bloqueo del
portal de gerentes y la revocación persistida. La política SELECT de
participantes deja a Flask la autorización de negocio y personal para el
escáner. El rol web es interno y multi-tenant.

### Evidencia del baseline del ledger

El PR #6 (merge `7280c32`) creó el ledger en la revisión
`c0d1e2f3a4b5`. Su migración actualiza el estado de cooldown a partir
del historial cuando agrega la columna y crea la tabla de ledger con sus
restricciones. No inserta una entrada por movimiento histórico ni registra
el conjunto de movimientos presentes al migrar.

Una cadena de balances internamente coherente y la correspondencia de
movimientos nuevos con sus entradas son comprobaciones distintas. Ninguna
identifica por sí sola qué movimientos existían antes del ledger. Para
clasificarlos se necesita una evidencia autenticada y vinculada a esa
ejecución de Alembic: un inventario contemporáneo de IDs, tenant y digest
de cada movimiento, o una copia consistente previa al upgrade cuyo vínculo
con la ventana de migración pueda demostrarse con un registro de despliegue
y sin escrituras en el intervalo. Un respaldo actual no sustituye esa
evidencia. El verificador estricto de transición permanece sin cambios.

### Inventario sintético y coordinación por etapas

El fixture usa `scripts/j2m_transition_privilege_inventory.sql` dos veces para
comprobar que el inventario sea reproducible. Registra roles, membresías,
conexión, uso del esquema, propietarios y ACL, RLS, políticas, triggers,
secuencias, configuración de funciones y privilegios predeterminados.
El inventario no constituye todavía una lista de autorización integral.
Queda pendiente evaluar todos los privilegios efectivos, Data API y las
rutas heredadas o alternativas antes de aprobar un manifiesto operativo.

`scripts/isolated_transition.py` coordina las etapas contra un adaptador
sintético. Comprueba las precondiciones y el resultado de cada etapa, usa
un lock exclusivo y reemplaza el diario después de escribirlo y sincronizarlo.
Una interrupción deja una etapa pendiente y una excepción registrada la
marca como bloqueada. Cualquier diario previo obliga a inspección
independiente. No se reejecutan mutaciones ni se reabren escritores a
partir del diario. Falta conectar los adaptadores Docker y PostgreSQL, y
ensayar recuperación y reapertura con comprobaciones vivas. El ejecutor no
está enlazado al despliegue operativo.

La prueba de laptop `tests/legacy_wallet_transition_docker.py` conecta el
coordinador con Docker, PostgreSQL 17, Alembic y Nginx sintéticos. Conserva
el diario de evidencia anterior y escribe el historial de etapas junto a
él. Exige un directorio privado del operador en Windows, donde el
reemplazo usa un archivo sincronizado, sin prometer sincronizar el
directorio tras una pérdida abrupta de energía. En POSIX también
sincroniza el directorio. El aislamiento de red y el drenaje de
contenedores en este ensayo no sustituyen una barrera de permisos a
través del pooler. La recuperación del backend legado es deliberada y
usa la misma base migrada.

Para investigar el baseline, el primer permiso necesario es lectura de
los registros del migrador de Railway que mencionen la revisión
`c0d1e2f3a4b5` y del catálogo de respaldos de esa ventana, limitado a
fechas, revisiones, identidad de ejecución y hashes. No es necesario
consultar filas ni restaurar respaldos para esa búsqueda inicial. Si se
localiza una copia contemporánea, se debe autorizar por separado su
validación aislada y la comprobación de ausencia de escrituras hasta el
upgrade. Si el historial ya no existe, se puede preparar una
reconciliación formal desde un corte futuro verificable, con revisión
independiente de diferencias. Ese corte no certifica la ventana
histórica y no cambia el rechazo estricto actual.


### Cierre de búsqueda histórica y criterios de revisión

La investigación de metadatos del 27 de noviembre de 2026 no encontró
una ejecución autenticada de `c0d1e2f3a4b5` ni una copia vinculable a ella.
El proyecto Railway visible se creó el 19 de octubre y su despliegue más
antiguo accesible es del 23 de octubre. Los primeros metadatos locales y
de Drive visibles corresponden al 18 de noviembre. Estos resultados no
prueban que no existieran movimientos ni que nunca hubiera otras copias.

No repetir esa búsqueda sin una fuente nueva. Verificar primero el respaldo
actual autenticado en aislamiento, con conteos agregados de movimientos
sin ledger, discrepancias de cadena, saldos sin baseline, referencias y
secuencias. Si la verificación estricta aprueba, documentar el alcance de
la integridad actual, sin atribuirle certeza histórica. Si falla, conservar
las diferencias como no resueltas y diseñar un corte prospectivo auditable,
sin fabricar entradas, modificar movimientos o saldos ni cambiar el
verificador estricto por una excepción genérica. El nuevo corte exige
evidencia del estado congelado, reconciliación independiente y pruebas
negativas; no certifica la migración histórica.

El cierre del código de preparación y la autorización de la transición
operativa son decisiones distintas: se puede revisar el código aislado
cuando cumpla los criterios de preparación, manteniendo bloqueada cualquier
operación real no certificada.

El inventario sintético incluye privilegios efectivos de conexión, esquema,
tabla, columna, secuencia, ejecución de funciones y membresías, incluyendo
las opciones de PostgreSQL 17. El fixture compara grants de tabla, columna y
secuencia de web y worker con sus concesiones explícitas y prueba desviaciones
directas, por PUBLIC y por membresía. Esto no acredita todavía funciones,
Data API, ni el manifiesto operativo completo. El workflow conserva únicamente
los diarios sintéticos del ensayo histórico como artefactos de CI.

### Barrera y reapertura mediante PgBouncer de sesión

El fixture de esquema completo añade un PgBouncer desechable en la misma red
interna que PostgreSQL 17. Usa DISABLE para bloquear clientes nuevos antes
de revocar permisos y NOLOGIN en PostgreSQL. Una sesión anterior conserva
el mismo PID entre commits, mantiene una transacción abierta hasta que el
bloqueo de drenaje se observa en pg_locks y luego comprueba denegaciones
por tabla, columna, secuencia, función y SET ROLE. Una conexión directa
nueva también debe fallar. El rol de respaldo conserva lectura bajo RLS.
La reapertura sintética reaplica los grants declarados, verifica el
inventario efectivo y habilita el ingreso antes de volver a probar
autenticación HTTP. La prueba usa una tabla de la cola para observar el
drenaje. Aún no acredita drenaje de todas las tablas, interrupciones de
reapertura, integración con el supervisor ni comportamiento de Supavisor.


### Cuentas sin baseline y autenticación de plataforma

La restauración real autorizada reportó correspondencia de movimientos y
ledger, pero la ausencia de un baseline en ciertos clientes no equivale por
sí misma a una discrepancia. La aplicación crea el baseline al registrar la
primera operación. Una cuenta sin ledger con ambos saldos cero puede ser
una cuenta sin actividad; una cuenta sin ledger con saldo no nulo debe
bloquear la certificación actual. El verificador estricto ahora comprueba
también ese caso y el fixture prueba ambas situaciones. No se altera ningún
saldo ni se fabrica historia. Verificar nuevamente el respaldo autenticado
y documentar por separado el estado de la ventana histórica.

El ensayo restringido amplía el fixture de Super Admin para login, dashboard,
revocación persistida y denegación a un gerente, antes y después de la
reapertura. Los grants de plataforma son exclusivamente de ensayo:
`SELECT FOR UPDATE` requiere UPDATE en alguna columna de platform_admins,
por lo que se limita a name y se prueban denegaciones de password_hash e
is_active. Las políticas de sesión para el rol interno son multi-tenant y
no sustituyen la autorización HTTP. Las operaciones administrativas de
escritura y el manifiesto operativo completo siguen pendientes.

### Terminación entre el commit de Alembic y el diario

El supervisor sintético arranca el coordinador con un identificador
aleatorio. La base usa un volumen Docker dedicado y etiquetado. El
supervisor termina el proceso después del commit y antes de confirmar
la etapa. Inspecciona los mismos recursos mediante Docker y PostgreSQL,
valida las identidades de ambas bases, la revisión y la contabilidad.
Comprueba que no haya otros clientes conectados. No vuelve a migrar ni
habilita escritores y elimina únicamente los recursos identificados del
ensayo. Esta prueba no sustituye otras fronteras de terminación ni
acredita recuperación con tráfico habilitado.

En la reapertura sintética con PgBouncer se eliminan las conexiones
PostgreSQL restantes de los dos escritores antes de restaurar grants.
El manifiesto se aplica con los roles aún en NOLOGIN y el ingreso del
pooler cerrado. Se verifica el inventario efectivo y se prueba la
denegación directa antes de volver a habilitar LOGIN e ingreso. Aún se
requieren pruebas de terminación durante esta acción separada.


### Ampliación del ensayo de interrupción

El supervisor sintético permite terminar el proceso realmente después de
las acciones FENCE, RESTORE, MIGRATE y VERIFY, antes de confirmar la etapa.
Verifica la revisión correspondiente, identidad, digest lógico independiente,
contabilidad y rechazo efectivo de nuevas conexiones de web y worker
(NOLOGIN), sin repetir migraciones. El fixture histórico mantiene ambos roles
NOLOGIN hasta su prueba de disponibilidad HTTPS. Esto no equivale a una
barrera integral de permisos en el pooler: el ensayo de PgBouncer sigue
siendo independiente. No se simula un fallo dentro de pg_restore ni dentro
de una transacción, ni se acredita todavía la recuperación durante HTTPS
o reapertura. CI conserva los informes de las cuatro fronteras.

El fixture de lealtad ejecuta el escáner web con sesiones de cajero
auténticas. Tiene dos negocios, uno de puntos y otro de sellos. El rol SQL
web puede actualizar únicamente current_points, current_stamps y
last_earn_at en customers en este fixture. Conserva SELECT y los inserts
necesarios en movimientos y ledger. Flask valida la pertenencia del QR
antes de leer o escribir. El ensayo compara los saldos y entradas
persistidos después de compra, acumulación, canje, replay concurrente y
rollback inducido. Esto no amplía los permisos de registro o recuperación.

La copia real previamente examinada pasó las comprobaciones actuales:
22 movimientos, 26 entradas de ledger, cuatro cadenas verificables y
dos cuentas sin actividad contable. Esta evidencia corresponde a esa
copia. Una transición posterior debe verificar el respaldo exacto que
utilice y no puede inferir de estas cifras el estado anterior a
c0d1e2f3a4b5.
