# Cobertura operativa de identidades antes de A3

## Sesión del worker

El CLI de Google Wallet conserva un contexto Flask durante el bucle. Las consultas de métricas abrían una transacción de lectura y podían dejarla abierta mientras el worker esperaba el ciclo siguiente. La limpieza ahora ocurre en finally mediante db.session.remove(), antes de dormir o evaluar el fin del bucle. Se conserva el rollback y el registro de fallos. El cambio no introduce commits y mantiene los límites de las transacciones de cada trabajo.

La consulta de publicaciones ahora agrega count(id) directamente. Así el worker solo necesita SELECT de id, status y locked_at en google_wallet_publication_jobs. El conteo se ejecuta también con Wallet deshabilitado.

El ensayo en PostgreSQL usa un login restringido y una conexión observadora independiente. Al omitir la limpieza reproduce idle in transaction y AccessShareLock en ambas colas. Con la limpieza, la conexión observadora obtiene ACCESS EXCLUSIVE durante la espera, el siguiente ciclo termina y las escrituras sintéticas ya confirmadas permanecen. Se comprueban también errores de procesamiento y métricas, stop_event, max_cycles y ausencia de efectos contables.

## Incremento propuesto

Los cuatro SQL y sus SHA-256 están en [scripts/a3_permissions](../scripts/a3_permissions). Los archivos no son migraciones. No se referencian desde arranque, despliegue ni tareas. El ensayo solo instala las líneas base A2 reproducidas en un clúster descartable.

| Base | Directas y políticas iniciales | Incremento | Después |
| --- | --- | --- | --- |
| Legada 9d0e1f2a3b4c | 148 y 36 | 46 y 8 | 194 y 44 |
| Objetivo c3d4e5f6a7b8 | 188 y 50, con A2 objetivo instalado | 46 y 8 | 234 y 58 |

Las filas de los GRANT en los archivos son la matriz exacta, una operación y una columna por línea si corresponde. Los dos heads comparten los objetos nuevos de este incremento. El obligatorio entorno objetivo y su línea base están separados en los guardas. Ningún archivo otorga permisos adicionales a owner, migrator, backup, recovery o roles de API. En el objetivo, el grantor debe ser j2m_owner tras una transferencia autorizada por separado.

| Receptor y objetos | Justificación |
| --- | --- |
| Web · businesses, business_categories y sus secuencias | Crear negocio y categoría por Super Admin. |
| Web · staff, secuencia, name/email/phone; access_audits INSERT y SELECT del id con su secuencia | Alta de cajero, edición de datos de contacto y auditoría append-only. No se añade escritura de hashes, rol, permisos o revocación. |
| Web · promoters, promoter_sessions | Login, portal y revocación de sesión del promotor. |
| Web · referral_attributions, referral_eligibilities y sus secuencias | Alta con referido y lectura/creación de elegibilidad pending. SELECT del id de atribución para RETURNING. |
| Web · referral_participants.updated_at | PostgreSQL requiere UPDATE sobre una columna para SELECT FOR UPDATE. La política USING (true) WITH CHECK (false) permite tomar el lock de alta y rechaza la modificación. |
| Web · referral_incentive_rules, referral_incentive_rule_audits y secuencias | Crear/listar/editar una regla en draft o disabled con auditoría append-only. Las columnas de edición se enumeran. No se añade acceso a la concesión efectiva de incentivos. |
| Worker · google_wallet_publication_jobs(id,status,locked_at) | Dos consultas de métricas. No puede leer el resto de columnas ni actualizar la cola de publicaciones mediante este incremento. |

Las transacciones cotejan head, identidades, ACL previas y posteriores y definiciones RLS, y preservan las ACL de los demás roles. Las comprobaciones de RLS comparan valores normalizados por PostgreSQL contra políticas de referencia en tablas temporales. La reversión solo revoca el incremento y elimina sus ocho políticas. Es obligatorio confirmar el destino del proyecto por un canal independiente, verificar NOLOGIN, OID, contraseñas nulas y ausencia de sesiones en una consulta administrativa antes de cualquier uso real. Abortará por revisión, estado o privilegios inesperados. Si no se sabe si hubo COMMIT, reconciliar el catálogo antes de decidir.

## Evidencia y alcance

- Los ensayos sintéticos ejercitan por HTTP alta atribuida y repetición, acceso de promotor, alta y edición básica de cajero, alta de negocio y categoría, panel de Super Admin, exportaciones Excel y PDF, reglas DRY-RUN y rechazo de un tenant ajeno. Estos se complementan con los ensayos PostgreSQL existentes de login, recuperación y revocación, puntos, sellos y contabilidad.
- El paquete se aplica, rechaza el replay y la divergencia de políticas y se revierte en los dos esquemas. Los tests comprueban que el worker no puede modificar saldos ni leer referencias de clientes de la cola de publicación y que web no puede escribir credenciales de staff.
- A3 necesita decidir el alcance de suspensión, reset y eliminación de personal, actualización completa de negocios y branding, administración de promotores y consentimientos, decisión de primera compra y variantes de informes que no se ejercitaron. Si deben exponerse, validar sus privilegios de columna y pruebas de denegación antes de habilitar el login. Wallet, emisión, escrituras reales del proveedor y recompensas quedan fuera.
- También quedan pendientes para A3 la aprobación independiente de cualquier incremento real, custodia de credenciales, TLS verificado por identidad, atribución de consumidores de la API, capacidad y afinidad reales de Supavisor, y pruebas negativas. Ninguno de estos SQL habilita LOGIN. No se ha intervenido la infraestructura.

El subconjunto de rutas sintéticas acredita permisos adicionales concretos. No autoriza sustituir el runtime por roles candidatos.
