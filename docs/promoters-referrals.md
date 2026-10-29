# Promotores y referidos

La atribución se crea únicamente cuando el alta directa del cliente concluye correctamente. Se crea en estado `pending`; el alta, una lectura QR o un movimiento `earn` nunca la verifican automáticamente. El enlace público contiene un código aleatorio estable, sin identificadores secuenciales ni datos personales.

El contexto se firma en el servidor y vincula negocio y promotor. Al confirmar el alta, el promotor vuelve a validarse dentro de la misma transacción; los códigos inválidos, suspendidos, ajenos al negocio o de autorreferencia por teléfono no generan atribución. Las claves foráneas compuestas mantienen el aislamiento entre tenants.

El promotor usa una sesión web revocable independiente de gerente y cajero. Su portal solo presenta conteos agregados de elegibilidades `verified` y nunca nombres, correos históricos, teléfonos, saldos, QR de clientes ni movimientos. La suspensión o el restablecimiento de contraseña revocan todas sus sesiones y quedan auditados.

Un gerente activo con el permiso explícito `review_referrals` puede realizar una revisión manual. Para verificar debe señalar una transacción `earn` con importe de compra positivo del mismo cliente y negocio, una referencia de evidencia y un motivo. La referencia se conserva únicamente como HMAC, no en claro. Esta es una atestación manual: no valida automáticamente un pago externo ni garantiza que una persona física no use múltiples identidades. Los estados admitidos son `pending`, `in_review`, `verified`, `rejected` y `revoked`; las transiciones se controlan mediante actualización condicional, clave idempotente e historial append-only.

La misma transacción y la misma referencia económica no pueden verificar más de una atribución. La revocación permite reflejar evidencia posterior de fraude o reverso sin borrar el historial. Ninguna decisión modifica puntos, sellos, ledger, Wallet ni crea incentivos.

Limitaciones: el teléfono y los datos del navegador reducen abuso, pero no identifican inequívocamente a una persona. Una persona que use teléfonos distintos puede obtener más de una tarjeta; no se usan nombre, IP, dispositivo ni QR como prueba universal de identidad. La Fase 8C.2 podrá consumir únicamente estados `verified` para reglas configurables de incentivos, pero deberá mantener una concesión económica separada e idempotente; esa concesión no forma parte de 8C.1.

La migración incremental `1b2c3d4e5f6a` conserva atribuciones históricas como `pending`. En PostgreSQL, las tablas internas nuevas habilitan RLS sin políticas públicas; no deben exponerse al rol anónimo/autenticado de una Data API. No se implementan comisiones, pagos, recompensas automáticas ni exportaciones de datos personales.
