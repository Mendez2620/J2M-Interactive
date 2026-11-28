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
