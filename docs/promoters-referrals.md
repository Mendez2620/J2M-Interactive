# Promotores y referidos

La atribución se crea únicamente cuando el alta directa del cliente concluye correctamente. Se crea en estado `pending`; el alta, una lectura QR o un movimiento `earn` nunca la verifican automáticamente. El enlace público contiene un código aleatorio estable, sin identificadores secuenciales ni datos personales.

El contexto se firma en el servidor y vincula negocio y promotor. Al confirmar el alta, el promotor vuelve a validarse dentro de la misma transacción; los códigos inválidos, suspendidos, ajenos al negocio o de autorreferencia por teléfono no generan atribución. Las claves foráneas compuestas mantienen el aislamiento entre tenants.

El promotor usa una sesión web revocable independiente de gerente y cajero. Su portal solo presenta conteos agregados de elegibilidades `verified` y nunca nombres, correos históricos, teléfonos, saldos, QR de clientes ni movimientos. La suspensión o el restablecimiento de contraseña revocan todas sus sesiones y quedan auditados.

Un gerente activo con el permiso explícito `review_referrals` puede realizar una revisión manual. La primera compra elegible es la primera transacción `earn`, ordenada por fecha e ID, que ocurra después tanto del alta del cliente como de la atribución. En puntos debe contener puntos e importe positivo. En sellos debe contener al menos un sello; si no se registró importe, exige `manual_receipt`, referencia de comprobante y atestación humana. Un importe registrado usa `recorded_amount`. Ni el movimiento ni la referencia prueban por sí solos un pago externo.

Las operaciones anteriores a la atribución nunca permiten `verified`. Si existen datos históricos incompletos o una excepción que impida demostrar la primera compra, el caso puede mantenerse `in_review` o rechazarse, pero no se sustituye silenciosamente por una compra posterior. La referencia se conserva únicamente como HMAC, no en claro. Los estados admitidos son `pending`, `in_review`, `verified`, `rejected` y `revoked`; las transiciones se controlan mediante actualización condicional, clave idempotente e historial append-only.

La misma transacción y la misma referencia económica no pueden verificar más de una atribución. `REFERRAL_EVIDENCE_HMAC_KEYS` es independiente de las claves de sesión; la primera clave firma evidencia nueva y las anteriores deben conservarse durante una rotación para detectar duplicados históricos. La revocación permite reflejar evidencia posterior de fraude o reverso sin borrar el historial. Las claves foráneas `RESTRICT`, los triggers de base y los eventos ORM impiden eliminar o editar silenciosamente decisiones. Ninguna decisión modifica puntos, sellos, ledger, Wallet ni crea incentivos.

La API JWT web continúa disponible para integraciones internas. El portal por cookie dispone de un endpoint POST protegido por sesión revocable, permiso y CSRF, pero 8C.1 no incluye listado ni interfaz visual; la navegación y gestión completa quedan reservadas para 8C.3. No se exponen JWT en HTML.

Limitaciones: el teléfono y los datos del navegador reducen abuso, pero no identifican inequívocamente a una persona. Una persona que use teléfonos distintos puede obtener más de una tarjeta; no se usan nombre, IP, dispositivo ni QR como prueba universal de identidad. La Fase 8C.2 podrá consumir únicamente estados `verified` para reglas configurables de incentivos, pero deberá mantener una concesión económica separada e idempotente; esa concesión no forma parte de 8C.1.

La migración incremental `1b2c3d4e5f6a` conserva atribuciones históricas como `pending` mediante `INSERT ... SELECT`, sin cargar todas las filas en memoria. La revisión posterior `2c3d4e5f6a7b` añade reglas y contratos de incentivos exclusivamente en modo DRY-RUN; consulte `docs/referral-incentives.md`. En PostgreSQL, las tablas internas nuevas habilitan RLS sin políticas públicas; no deben exponerse al rol anónimo/autenticado de una Data API. No se implementan comisiones, pagos, recompensas automáticas ni exportaciones de datos personales.

## Participantes tipados (8C.2B)

La revisión `3d4e5f6a7b8c` introduce `referral_participants` como identidad tenant-scoped para `external_promoter`, `staff` y `customer_ambassador`. Las atribuciones históricas se enlazan a un participante de promotor sin cambiar su promotor, cliente, estado ni evidencia. Los nuevos empleados y embajadores nunca se guardan en `promoter_id`.

Un gerente necesita `manage_referral_participants` para habilitar o revocar a otro empleado; el empleado necesita además `refer_customers`, cuenta activa y participación habilitada para que su código produzca una atribución. La suspensión, retirada del permiso o revocación invalida inmediatamente nuevas invitaciones. Cada cambio exige motivo, clave idempotente y auditoría append-only. Super Admin dispone del mismo contrato administrativo global, sin habilitación automática de empleados nuevos.

Los clientes embajadores requieren consentimiento específico versionado y evidencia hash. El servicio interno está preparado, pero **no existe endpoint público para emitir su enlace**: Customer no dispone actualmente de una sesión persistente que pruebe titularidad. La autorización temporal de entrega Wallet, el teléfono, el ID, el QR de caja y el enlace Wallet no son prueba suficiente. Una fase posterior deberá incorporar autorización fuerte o recuperación asistida antes de exponer esta función.

Contrato backend para 8C.3:

- `POST /manager/business/<business_id>/staff/<staff_id>/referrals`, con `manage_referral_participants`, CSRF, `Idempotency-Key`, `enabled` booleano y `reason`.
- `GET /manager/portal/referral-invitation`, con sesión revocable y `refer_customers`, devuelve código y ruta relativa; no devuelve datos de invitados.
- El registro público sigue usando `GET/POST /join/<slug>` y crea la atribución únicamente después de completar el alta.
- La futura pestaña Referidos podrá agrupar métricas, equipo/promotores, embajadores, reglas y auditoría, sin modificar los contratos Wallet.
