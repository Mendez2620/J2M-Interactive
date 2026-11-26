# Fase 8E.2B.3A — Preflight de reconciliación de saldos

La selección es interna y de solo lectura: `preview_google_wallet_reconciliation(business_id, limit=50, after_customer_id=0)`. No hay ruta pública, comando de aplicación, activación de worker ni acceso a Google.

Se inspecciona un negocio explícito y activo, mediante paginación de clientes por ID (máximo 100 por página) y un límite de 5.000 registros por cada historial. PostgreSQL utiliza una transacción dedicada de solo lectura y aislamiento repeatable read. SQLite abre una transacción de lectura explícita para pruebas locales. No ejecutar con una transacción abierta que comparta una conexión SQLite en memoria.

La selección requiere baseline, cobertura de todos los movimientos registrados, coincidencia de negocio y cliente, deltas coherentes con las transacciones, continuidad del ledger y saldo confirmado igual al actual. Los historiales incompletos, incluidos los anteriores al baseline, se excluyen conservadoramente; nunca se reconstruye el ledger. Los estados desconocidos se bloquean. Si cualquier trabajo del cliente sigue pendiente o procesándose, no se selecciona. Un trabajo completado de la última transacción tampoco se vuelve a seleccionar; la falta de trabajo o un último fallo pueden generar una candidatura local.

El resultado contiene agregados y snapshots internos no serializables públicamente: ID estable del cliente, última transacción, última entrada de ledger, trabajo fallido si existe, saldo y parámetros de representación. No registrar esos snapshots ni presentarlos a usuarios finales. La paginación no es una autorización ni una fotografía global entre páginas.

**Una candidatura local no prueba que exista un objeto remoto.** Antes de permitir ejecución se requiere un orquestador autorizado con:
- revalidación en una lectura fresca bajo el bloqueo compartido por cliente;
- comprobación de modo, negocio y objeto remoto existente, sin reemisión;
- tratamiento de movimientos concurrentes y evidencia durable de resultados sin marcar transacciones posteriores como sincronizadas;
- límites, auditoría, idempotencia, recuperación de fallos y autorización separada para llamadas reales.

Las pruebas usan SQLite aislado y, cuando se dispone del servicio PostgreSQL de CI dedicado, esquemas sintéticos únicos. No se accede a Supabase ni a Google.


## Ejecutor interno de una candidatura

`execute_google_wallet_reconciliation(candidate, business_id=..., service=...)` requiere el modo real configurado, un negocio explícito y un proveedor real inyectado. No es una interfaz autorizada por sí misma: no tiene endpoint, comando ni tarea programada.

Adquiere el mismo bloqueo por cliente que el worker, vuelve a construir una fotografía fresca y exige coincidencia exacta con la candidatura. Envía al proveedor únicamente la representación del saldo comprobado, sin QR ni información personal. La llamada remota no mantiene bloqueadas las filas del cliente durante I/O. Tras una confirmación de saldo, abre una transacción local breve, bloquea negocio/cliente en PostgreSQL (SQLite usa BEGIN IMMEDIATE), vuelve a comprobar ledger y trabajo, y registra la última transacción inspeccionada como completada. Si ya existe un trabajo fallido, actualiza exclusivamente ese trabajo conservando sus intentos previos. Si falta, inserta un único trabajo completado; nunca uno pendiente que podría crear otra tarjeta.

Un objeto ausente, error, respuesta sin confirmación o cambio concurrente no genera un nuevo trabajo ni se declara completado. Una colisión de unicidad no sobrescribe el trabajo ganador. Repetir después de una confirmación local no llama al proveedor. Si Google confirma pero el proceso falla antes del registro local, la siguiente recuperación debe verificar de nuevo el objeto existente; el proveedor ya implementa no-op cuando el saldo coincide.

Pendiente antes de operación: autorización administrativa, selección y revisión del lote, registro durable de intentos fallidos/omitidos (solo éxitos quedan registrados en el outbox actual), límites operativos, política de reintentos, verificación de recuperación real y prueba en dispositivo. No activar el proveedor por este cambio.

El lote interno `execute_google_wallet_reconciliation_batch` admite entre 1 y 25 candidaturas de un solo negocio, rechaza toda la selección si hay identidades repetidas o ajenas, ordena por cliente y se detiene ante el primer error o reintento. Solo devuelve agregados. No constituye un comando operativo autorizado; los resultados inciertos no se marcan completados.


## Control de administración e historial durable

El módulo interno `google_wallet_reconciliation_control.py` añade `run_reviewed_reconciliation`. Requiere una identidad de administrador de plataforma previamente autenticada por el futuro punto de entrada, administrador activo en la base, negocio activo, confirmación booleana explícita, UUID de solicitud y selección acotada. Un ID de administrador por sí solo no autentica a nadie.

Utiliza la tabla existente `access_audits`, como el flujo de publicaciones. Registra por inserción el inicio de la solicitud, cada inicio/resultado individual y el resumen final. No se agregan migraciones ni se modifica la retención existente. Los registros guardan referencias locales de auditoría, huella de selección y resultados controlados, sin saldos claros, nombres ni credenciales. Un bloqueo por negocio serializa el control de solicitudes. La misma solicitud y selección terminada reproduce el resumen sin llamar al proveedor. Una solicitud iniciada sin resultado final permanece incierta y no se repite automáticamente; requiere investigación y nueva selección autorizada.

El resultado remoto, la cobertura local y la bitácora no forman una transacción distribuida. Si se pierde el registro final, no se declara éxito de la solicitud aunque la cobertura local ya pudiera estar confirmada. No se garantiza inmutabilidad física de la tabla por este cambio; se utiliza su contrato de historial por inserción existente.

La bandera `GOOGLE_WALLET_RECONCILIATION_ENABLED` permanece ausente/falsa y no se expone a configuración de entorno, interfaz web o CLI. Pendientes: conectar un punto de entrada autenticado con CSRF, revisión explícita de la selección, autorización operativa y validación física. No se debe activar esta operación todavía.
# Persistent write reservations

Real Google Wallet writes require a committed reservation for the exact class
or object. A request starts durably before HTTP. An interrupted write retains
ownership indefinitely. A later GET, a new request UUID and a worker restart
cannot clear it. Class and object reservations are acquired in that order.

Before a rollout or restore, disable Google Wallet and drain every writer
running older code. Keep Google Wallet disabled after restoring an older
database until the remote state and any potentially lost reservations have been
reviewed. There is no administrative override for uncertain writes.
