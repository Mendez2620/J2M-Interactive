# Certificación operativa 8E.2D.3

Este procedimiento continúa el preflight histórico. La preparación no
habilita el despliegue desde la imagen legada. El overlay de Compose se
incluye solo si se selecciona expresamente en una futura ventana aprobada.

## Candidatos de conexión

| Identidad | Alcance propuesto | Privilegios fuera del alcance |
| --- | --- | --- |
| j2m_owner | Propiedad de los objetos J2M enumerados, sin LOGIN | Objetos administrados por Supabase |
| j2m_migrator | DDL aprobado por sesión dedicada con SET ROLE al propietario | Login de servicios, credenciales compartidas |
| j2m_web | Flujos HTTP con grants por tabla y columna del fixture reconciliado | Propiedad, BYPASSRLS y escritura por funciones ajenas |
| j2m_worker | Colas, leases, reservas y lecturas necesarias | Saldos, ledger, credenciales |
| j2m_backup | SELECT y políticas RLS de lectura de cobertura completa. Enumerar secuencias | DML, DDL, TEMP, avance de secuencias |
| j2m_recovery | Identidad independiente de recuperación, custodiada fuera del runtime | Uso por web, worker y tareas rutinarias |

Los roles de servicio son internos y atienden varios tenants. Los filtros
y permisos de Flask autorizan cada operación. FKs compuestas y RLS
protegen la integridad y limitan operaciones por rol. El rol web en SQL
directo no constituye una frontera de tenant.

## Configuración candidata

Mantener cuatro archivos privados distintos, fuera del árbol de Git y del
contexto de construcción. Protegerlos con el mecanismo ACL del host y
examinar herencia y accesos efectivos antes de escribir secretos. Pasar
rutas explícitas en J2M_WEB_ENV_FILE, J2M_WORKER_ENV_FILE y
J2M_MIGRATOR_ENV_FILE. El overlay reemplaza env_file por servicio y monta
la CA verificada en solo lectura. Requiere una versión de Compose que
soporte !override y format: raw. Los valores raw no llevan comillas de
envoltura. La variable J2M_DATABASE_CA_FILE apunta a una CA del host,
fuera del build, y cada URI de servicio usa verify-full y el mount
/run/j2m/supabase-ca.pem.

El respaldo candidato usa DATABASE_URL y, opcionalmente,
DATABASE_SSL_ROOT_CERT. El runner existente de Python acepta el archivo
por --env-file y fuerza verify-full en pg_dump. En ausencia de CA
explícita emplea la confianza del contenedor PostgreSQL. Las tareas
instaladas siguen usando su configuración anterior. Rotar una credencial
candidata y volver a ensayarla antes de revocar la anterior. No incluir
claves Wallet en los archivos de transición.

Ejecutar python -m scripts.check_selfhost_identity_files con las cuatro
rutas y el destino verificado de forma independiente. Solo valida forma,
destino, TLS declarado, roles y separación de valores. Las ACL, la CA
y el funcionamiento de los roles se verifican por separado. Validar
Compose con config --quiet para evitar imprimir el entorno resuelto.

## Secuencia para una futura ventana

1. Inventariar el origen y el esquema objetivo desde las dos revisiones
   autorizadas. Comparar propietarios, ACL efectivas, PUBLIC, INSERT por
   columna, secuencias, funciones, triggers, RLS, roles transitivos y
   privilegios predeterminados. Confirmar los esquemas realmente expuestos
   en Data API. Usar el catálogo en modo de solo lectura.
2. Enumerar los objetos J2M y sus owners en un manifiesto de producción.
   Para cada objeto incluir identidad cualificada, ACL antes/después y
   política esperada. Excluir objetos de extensiones y esquemas gestionados.
   La evidencia sintética no autoriza aplicar un GRANT de todos los
   objetos de public. Rechazar cualquier objeto sin clasificar.
3. En una operación autorizada separada, crear primero los roles candidatos
   sin LOGIN. Definir sus grants por operación y políticas RLS antes de
   habilitar credenciales independientes. Validar las cuatro conexiones
   por el endpoint y CA esperados. Comprobar el rol efectivo, aislamiento
   HTTP y cobertura del pg_dump desde el rol de respaldo. Retener la
   credencial histórica mientras no se actúe sobre sus dependencias.
4. Antes del corte, cerrar la admisión, suspender temporalmente las tres
   tareas y los escritores confirmados. Adquirir la exclusión del runner,
   releer procesos y drenar transacciones, incluidas conexiones ya
   establecidas. Una tarea Ready o una captura de pg_stat_activity no
   demuestra ausencia de escritores alternativos. Exigir denegación
   efectiva por pooler y rutas directas, además de conservar respaldo y
   migrador. No usar NOLOGIN de la cuenta administrada como sustituto
   del plan auditado.
5. Bajo exclusión generar y autenticar el respaldo integral exacto.
   Restaurarlo sin salida, verificar el hash de cada componente, el head
   y la contabilidad estricta. Registrar el ID del conjunto, digest,
   imágenes y estado observable. Ante un fallo mantener el ingreso cerrado.
6. Migrar únicamente si la revisión exacta sigue siendo 9d0e1f2a3b4c y el
   head aprobado es c3d4e5f6a7b8. Mantener la exclusión del migrador a
   través de los commits. Si se pierde la respuesta establecer el estado
   mediante el esquema real y el diario. Nunca repetir una migración
   ya confirmada.
7. Validar permisos, esquema, saldos, movimientos, ledger y backend
   HTTPS. Un HTTP 200 sin identidad del backend y un diario parcial
   no autorizan reapertura. Si es preciso recuperar el backend anterior,
   hacerlo sobre la misma base migrada únicamente si la compatibilidad
   comprobada y la revisión real coinciden. Conservar mantenimiento si
   ninguna versión puede atestarse. No bajar el esquema ni restaurar
   copias anteriores sobre movimientos más nuevos.
8. Aprobación independiente de reapertura. Revalidar exclusión, inventario,
   contabilidad y permisos efectivos. Habilitar el ingreso al final,
   comprobar un único worker y mantener ambos Wallet deshabilitados.
   Restaurar la programación anterior y comprobar cada tarea en orden.
   La revocación de la credencial histórica exige validar previamente
   la recuperación y demostrar que ningún proceso sigue usándola.

## Puertas de aborto

Identidad inesperada, ACL no reconciliada, otro escritor, pérdida de
afinidad o limpieza incierta, respaldo sin autenticación o discrepancia
contable, presión de disco, fallo TLS o backend equivocado mantienen
NO-GO. La sonda aprobada con la identidad histórica no certifica por sí
misma los roles futuros ni una barrera operativa en Supavisor.


## Controles adicionales de archivos y sonda

El validador de candidatos rechaza discrepancias entre la interpretación
normalizada y los valores de Compose raw en todas las asignaciones. Los
archivos de transición no contienen campos de Google o Apple distintos
de los modos deshabilitados y la reconciliación deshabilitada, incluso
si el proveedor no los usaría. DATABASE_SSLMODE, si aparece, debe ser
verify-full. La validación sigue siendo estructural: no acredita ACL,
arranque de Flask, compatibilidad de claves de aplicación ni conexión
real. Conservar las claves HMAC y de cifrado estables que necesite cada
proceso; no rotarlas por accidente al separar contraseñas de PostgreSQL.

La sonda admite ahora un rol candidato explícito (web, worker, migrator
o backup). Exige la identidad SQL current_user y session_user en ambas
sesiones antes de adquirir el lock y después de COMMIT. Para candidatos
se exige CA verificada del host explícita mediante --ca-file, destino
Session pooler 5432 y TLS verify-full. No se utiliza la ruta de CA del
contenedor como ruta de Windows. El diagnóstico sigue requiriendo
autorización específica y no habilita la transición.

    python -m scripts.probe_authorized_pooler --env-file <archivo-privado-del-rol> --expected-host <host-verificado> --expected-project-ref <proyecto-verificado> --expected-role j2m_web --ca-file <CA-verificada-del-host> --authorize-session-lock-probe

Repetir para worker, migrator y backup solo cuando existan las
identidades autorizadas. La identidad de migración se prueba primero
sin SET ROLE; su capacidad limitada para asumir al propietario se
certifica por separado. La documentación de Supabase confirma el
formato ROLE.PROJECT_REF para el pooler compartido y advierte que cada
rol puede abrir su propio pool de conexiones: dimensionar el presupuesto
total antes de habilitar todos los servicios.

El inventario reportado indica escritura de service_role sobre las 42
tablas existentes. La barrera debe considerar también Data API,
funciones RPC, vistas, grants predeterminados y consumidores externos.
No confundir ausencia de grants para anon/authenticated con cierre
de service_role. No revocar masivamente privilegios de roles gestionados
ni cambiar las claves del proyecto durante la preparación.

Referencias:
- https://supabase.com/docs/guides/database/connecting-to-postgres
- https://supabase.com/docs/guides/database/connection-management
- https://supabase.com/docs/guides/api/securing-your-api

## Inventario de relaciones y accesos alternativos

El manifiesto scripts/j2m_transition_relation_manifest.json enumera las
relaciones J2M de public que no pertenecen a extensiones. El catálogo
legado autorizado contiene 42 tablas y 30 secuencias, sin vistas en ese
conjunto. Las tres revisiones incrementales añaden cuatro tablas y una
secuencia, además de claim_token en google_wallet_sync_jobs. El fixture
compara los dos heads y rechaza relaciones sobrantes, faltantes y
revisiones desconocidas. Esta comprobación se complementa con la matriz
efectiva de roles del fixture existente. La lista de relaciones aún no
constituye una autorización para transferir propiedad o aplicar grants.

La vía pendiente por Data API es service_role, que tiene escritura
efectiva sobre las tablas J2M. El catálogo observado contiene seis
funciones de trigger en public y ninguna función pública de otro tipo.
Los privilegios predeterminados y las membresías gestionadas requieren
revisión por objeto; la ausencia de funciones SECURITY DEFINER en public
no demuestra ausencia de caminos en otros esquemas. Para REST, GraphQL
y RPC, contrastar esquemas realmente expuestos, roles efectivos y uso
autorizado de service_role en Edge Functions, programadores y consumidores.
El estado REMOVED de Railway es evidencia de despliegue, no de revocación
de credenciales.

El plan de barrera futura debe limitarse a relaciones, columnas, secuencias,
funciones y defaults J2M explícitamente inventariados. Registrar ACL
directas y efectivas antes del corte, probar negativamente cada camino
por catálogo y desde consumidores autorizados, y preparar una reversión
exclusiva de esos grants. Una vía de escritura sin atribución mantiene
NO-GO. Conservar claves, políticas de otros proyectos y roles reservados.
No aplicar el SQL de los fixtures al servicio administrado.

## Presupuesto candidato de conexiones

Primer techo de aplicación para medir, no una cuota contratada:
web con 2 procesos y pool de 2 más 1 overflow por proceso (6), worker con
pool de 2 más 1 overflow (3), migrador con 2 conexiones como máximo
(incluido el lock dedicado), respaldo con 1 conexión sin paralelismo y
2 conexiones reservadas para diagnóstico y recuperación. Total candidato:
14 conexiones de cliente. Revalidar lotes y cualquier conexión directa,
límites por combinación usuario/base del pooler, conexiones gestionadas,
sesiones históricas y reserva server-side. Los límites del servidor no
equivalen a capacidad disponible del pooler. El overlay candidato fija los
procesos web y los tamaños de pool para web, worker y migrador. El check
HTTP de disponibilidad comparte el pool del proceso Gunicorn que lo
atiende. Una segunda réplica web o worker requiere volver a calcular el
presupuesto. Las tres conexiones de respaldo y diagnóstico siguen siendo
una reserva hasta acreditar la ventana completa. Si falta margen, detener el
plan y reducir concurrencia/configuración antes de habilitar identidades.

## Autorización consolidada propuesta

- **A. Aprovisionamiento.** Autorizar solo los seis candidatos, credenciales
  fuera de Git, membresía del migrador limitada al propietario y ACL
  enumerada para objetos J2M. Antes de transferir propiedad comprobar
  CREATEROLE, capacidad de SET ROLE y reglas del rol administrado en el
  ensayo. Probar conexión TLS, permisos efectivos, recuperación separada
  y cobertura de respaldo. Abortar si un objeto o consumidor no está
  clasificado o si el proveedor impide un paso.
- **B. Mantenimiento y migración.** Autorizar una ventana para cerrar
  admisión, suspender los tres runners, drenar escritores y adquirir el
  lock compartido. Aplicar la barrera J2M aprobada y autenticar/restaurar
  el respaldo exacto. Punto de control independiente antes de ejecutar
  Alembic. Si falta respaldo reproducible o el estado es ambiguo,
  mantener la barrera. No usar downgrade.
- **C. Verificación y reapertura.** Autorizar por separado la reapertura
  después de validar la revisión, contabilidad, roles, HTTPS, worker único
  y recuperación. Restaurar exclusivamente los accesos aprobados y
  reanudar tareas con los intervalos existentes.

La duración de indisponibilidad sigue sin estimarse con evidencia hasta
cronometrar la secuencia completa con los archivos independientes, la
coordinación de tareas y el respaldo completo. No presentar los tiempos
del fixture de relaciones como estimación de una ventana real.

### Migrador dedicado del ensayo

El overlay de identidades ejecuta `python -m scripts.run_certified_migration` para
el migrador. El comando está limitado al PostgreSQL sintético `db/full_schema`
con TLS verify-full. Comprueba el login `j2m_migrator` antes de asumir
`j2m_owner` y conserva un bloqueo de sesión en la misma conexión durante
Alembic. Si la revisión de origen difiere o el resultado es incierto,
la ejecución se detiene. Inspeccionar el head efectivo antes de plantear
otra ejecución. La ruta legada de Compose permanece intacta.

### Evidencia adicional de catálogo y respaldo

El fixture PostgreSQL compara un inventario de propietarios, ACL, columnas,
secuencias, funciones, triggers, políticas, defaults y membresías directas y
efectivas. El ensayo de gateway usa roles descartables para modelar privilegios
heredados, permisos de PUBLIC y ejecución de una función SECURITY DEFINER. La
revocación sintética queda limitada a los objetos nombrados por el ensayo.
La conciliación contra una matriz aprobada del proveedor sigue pendiente.

El runner incluye una modalidad sintética para j2m_backup. Exige TLS verify-full,
el destino db/full_schema, el identificador de un snapshot exportado y una
comprobación independiente del contenido restaurado. La prueba mantiene viva la
transacción exportadora y compara relaciones, filas y estado de las secuencias
en una fuente sintética sin escritores. Las secuencias no usan MVCC. Un ensayo
operativo tendrá que impedir cambios de secuencias desde todos los escritores
hasta cerrar el respaldo. El índice integral solo se publica después de la
restauración, la verificación contable y la comparación de cobertura.
Los runners operativos y su programación no cambian.

### Puntos de aprobación

- **A.** Antes de aprovisionar, contrastar el inventario inicial con la lista
  de esquemas expuestos por Data API y GraphQL, los endpoints REST y RPC y los
  consumidores externos de la credencial service_role e histórica. Clasificar
  las funciones privilegiadas y los objetos gestionados por el proveedor.
  Acotar cualquier transferencia de propiedad a objetos J2M y comprobar
  capacidad de SET ROLE y transferencia antes de ejecutar DDL. Abortar ante
  discrepancias de ACL, membresías o consumidores sin atribución.
- **B.** Autorizar separadamente el mantenimiento y la barrera de escritores
  enumerados. Comprobar drenaje directo y por pooler, adquirir el lock de
  respaldos y verificar la restauración del conjunto exacto y la contabilidad
  antes de migrar. Ante resultado incierto conservar la barrera y comprobar
  la revisión y el estado efectivos. Evitar reejecutar una migración incierta.
- **C.** Autorizar la reapertura solo después de acreditar la revisión, el
  upstream HTTPS, el pin y la ausencia de escritores adicionales. Reponer
  los permisos exactos aprobados, comprobar los accesos efectivos y reanudar
  las tareas con sus intervalos vigentes. Ante ambigüedad cerrar el ingreso
  y restablecer la barrera.

Los tiempos de los subensayos son evidencia sintética. La ventana completa
todavía requiere integrar la coordinación de tareas, el supervisor y la
reapertura en una misma ejecución.

### Respaldo previo a la migración y capacidad sintética

La prueba restringida se ejecuta en 9d0e1f2a3b4c, antes del migrador.
El fixture adquiere la exclusión de operaciones antes de exportar el
snapshot, comprueba ausencia de otras sesiones en la base y cierra
CONNECT a las identidades escritoras del ensayo durante la copia.
j2m_backup conserva lectura y se prueba la denegación de nextval.
La revisión se declara en la invocación, se comprueba en la restauración
y se autentica en el índice del conjunto sintético. Una revisión errónea
o una fila oculta por RLS debe impedir publicar un índice nuevo.
El comando restringido exige la red interna del fixture, no admite
destinos operativos y no altera el valor esperado por los runners legados.

La matriz sintética comprueba propietarios, ACL de relaciones, ausencia
de grants de columna antes de aprovisionar, RLS, identidad y modo de
ejecución de las rutinas y membresías en ambos heads. La pertenencia
implícita a pg_database_owner se modela por separado. Todavía falta
declarar por objeto el contenido esperado de columnas, defaults,
políticas, triggers y rutinas, y reconciliar los consumidores externos.
La barrera usada sobre la base descartable no prescribe revocar
CONNECT a PUBLIC ni a cuentas administradas en Supabase real.

La sonda de capacidad usa dos procesos Gunicorn con tres conexiones
cada uno y pools auxiliares con los tamaños candidatos de worker,
migrador, respaldo y diagnóstico. Verifica los PID PostgreSQL desde
una conexión ya contabilizada y prueba la denegación al superar cada
pool. Los pools auxiliares modelan concurrencia, sin ejecutar en ese
instante el runner de respaldo ni el migrador real. La sonda y la
restauración previa a migrar son subetapas del fixture. La suspensión
de tareas, la atestación HTTPS y la recuperación supervisada aún deben
quedar unidas a ellas en una sola ventana de mantenimiento. Ninguna
medición del fixture implica una capacidad de Supavisor certificada.

### Identidades y cierre de la ventana descartable

La restauración restringida usa la reconstrucción de dae140e creada desde
Git en el job histórico. El fixture verifica la etiqueta de revisión,
resuelve el ID inmutable y calcula el SHA-256 del índice autenticado.
El restore recibe ambos valores y vuelve a comprobarlos. La reconstrucción
acredita compatibilidad del código histórico. Verificar una imagen instalada
requiere comparar su ID por separado.

En el clúster descartable, se retiene CONNECT para el lector y luego
para el migrador durante el cierre de admisión. Los escritores se
mantienen cerrados hasta que el ensayo de roles termina correctamente.
Ante una excepción, el cierre permanece activo hasta la limpieza del
clúster. El supervisor histórico y la atestación HTTPS siguen ejecutándose
como ensayos separados.

Para la aprobación A, clasificar service_role, REST, Data API y GraphQL
según esquemas expuestos y rol efectivo. Inventariar las funciones RPC por
firma y privilegio EXECUTE, así como las funciones SECURITY DEFINER y su
propietario. Confirmar también quién conserva la credencial legada, dónde
puede usarse, y si una conexión directa o un consumidor externo puede
escribir tras drenar el pooler. Verificar credenciales persistentes
independientemente del estado de despliegue de Railway. Un consumidor sin
atribuir impide B.

Para B, enumerar el delta por objeto J2M, incluidas ACL por columna,
secuencias y rutinas. Guardar el grantor y la opción de grant para preparar
su inversa exacta. Tras cerrar ingreso y drenar escritores por ambas
rutas, autenticar y restaurar el índice exacto antes de autorizar el
migrador. Si falta una verificación o la revisión es incierta, mantener
la barrera e inspeccionar los recursos vivos.

Para C, acreditar el backend por HTTPS, un único worker, la contabilidad
y los permisos efectivos antes de reponer el delta aprobado. Reabrir
ingreso y reanudar tareas solo después de comprobar cada vía de escritura
prevista. El ensayo descartable no acredita los permisos ni el
comportamiento efectivo de Supavisor.

La búsqueda de referencias a service_role y a rutas REST, GraphQL y RPC
en app, scripts, workflows y la configuración Railway versionada solo
encontró las referencias del runbook. La inspección de consumidores
externos y esquemas publicados sigue siendo una condición operativa.

### Contrato declarado para la migración y HTTPS sobre el origen

La matriz PostgreSQL comprueba los tipos, nulabilidad, identidad y defaults
de las cuatro tablas añadidas y de claim_token. En el head legado esos
campos deben estar ausentes. La lista se deriva del DDL de las tres
migraciones. Los cuerpos de las seis rutinas se contrastan por separado.

El fixture inicia un proxy interno y un web y worker de diagnóstico con
la identidad lectora sintética. Las sondas HTTPS deben correlacionar cada
nonce con el upstream esperado, confirmar la revisión de la imagen y
conservar el mismo clúster y head. La recuperación de backend se ensaya
sobre el origen ya migrado, sin restaurar datos sobre él. La barrera de
ingreso a escritores permanece activa durante las sondas. Tras la salida
normal se comprueba CONNECT de los escritores. Los ensayos de terminación
del supervisor se conservan por separado. La restauración de CONNECT del
fixture no habilita un corte operativo.

Una tarea simulada compite por el mismo archivo de exclusión antes
del respaldo y vuelve a intentarlo al finalizar. Se registra el tiempo
desde la adquisición de la exclusión hasta su liberación. Esta medición
incluye las negativas del fixture y no mide una ventana operativa.

### Secuencias y reapertura del origen sintético

La declaración versionada del inventario identifica las 30 secuencias de
origen y la nueva secuencia de destino. Para cada SERIAL de las claves
primarias integer, la comprobación exige los parámetros PostgreSQL, el
vínculo OWNED BY y el default nextval en la columna correcta. La nueva
regresión SQL intenta otorgar el rol propietario a un login ajeno a J2M
dentro de una transacción revertida y exige que el catálogo lo rechace.

El alias full_schema de PgBouncer apunta a la base que se migró y sirvió
los diagnósticos HTTPS. El fixture de grants y de interrupción de
coordinadores usa ese alias. La reconexión por pool se verifica con
current_database y current_user. En la reapertura, Nginx vuelve a
atestarse con el par de identidades web y worker; los contenedores
comprueban sus identidades SQL efectivas sobre full_schema. Las tres
tareas simuladas comprueban la misma exclusión antes y después de la
ventana. El volumen restaurado sigue disponible como destino separado
para las negativas de contabilidad que alteran deliberadamente valores.

Las pruebas de terminación del supervisor histórico tienen su propio
ciclo de creación de recursos. Para completar la certificación todavía
deben compartir una variante de la ventana desde el respaldo restringido,
con las mismas identidades inmutables y el diario de ese origen. La
declaración de columnas históricas fuera de las claves SERIAL también
sigue pendiente. Las pruebas de Supavisor, la atribución externa y la
validación de credenciales e imagen instaladas pertenecen a las puertas
operativas A/B/C.

### Columnas históricas y suspensión persistente

El manifiesto histórico fija 493 columnas de 42 tablas. Se contrastaron
las declaraciones SQL de una reconstrucción independiente de dae140e en
PostgreSQL 17.12, migrada desde una base vacía hasta 9d0e1f2a3b4c, con el
volcado DDL de ese esquema. El fixture compara tipos, nulabilidad y
defaults en ambos heads y conserva el delta en un contrato separado.

Antes del respaldo se comprueban escritores reales del fixture. Sus
identidades deben autenticarse directamente y mantener sesiones en
PgBouncer. Al aplicar la barrera se verifica CONNECT denegado, alias
deshabilitado y drenaje de las sesiones persistentes. El manifiesto
transitorio conserva un marcador de suspensión para cada prueba: los tres
runners simulados deben rechazar el arranque aunque esté libre el lock.
La prueba de proceso confirma que terminar al dueño del lock libera el
archivo de exclusión y conserva el marcador de suspensión.

CI ejecuta una ruta nominal y una ruta de regresión en clústeres
distintos. La nominal excluye las interrupciones inyectadas y el
reemplazo deliberado del backend. Ambas reutilizan los componentes de
respaldo, migración, atestación y reapertura. La variante integrada del supervisor termina el coordinador completo
tras confirmar la migración y antes de atestar HTTPS. Conserva el
origen para volver a comprobar su identidad, contabilidad, digest,
revisión, respaldo y barreras. Mantiene cerrado el ingreso en esa
variante porque falta la atestación. Las demás fronteras continúan
en el ensayo histórico separado. Los runners operativos siguen intactos.
