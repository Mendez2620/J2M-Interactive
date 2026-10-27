# Promotores y referidos

La atribución se crea únicamente cuando el alta directa del cliente concluye correctamente. El enlace público contiene un código aleatorio estable, sin identificadores secuenciales ni datos personales.

El contexto se firma en el servidor y vincula negocio y promotor. Al confirmar el alta, el promotor vuelve a validarse dentro de la misma transacción; los códigos inválidos, suspendidos, ajenos al negocio o de autorreferencia por teléfono no generan atribución. Las claves foráneas compuestas mantienen el aislamiento entre tenants.

El promotor usa una sesión web revocable independiente de gerente y cajero. Su portal solo presenta conteos agregados y nunca nombres, correos históricos, teléfonos, saldos, QR de clientes ni movimientos. La suspensión o el restablecimiento de contraseña revocan todas sus sesiones y quedan auditados.

La migración `f9a0b1c2d3e4` sigue siendo incremental y su downgrade continúa bloqueado para preservar el historial. No se implementan comisiones, pagos, recompensas automáticas ni exportaciones de datos personales. Si el esquema se expusiera mediante Supabase Data API, debe permanecer protegido por permisos y RLS apropiados.
