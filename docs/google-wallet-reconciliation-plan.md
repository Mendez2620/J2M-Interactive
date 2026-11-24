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
