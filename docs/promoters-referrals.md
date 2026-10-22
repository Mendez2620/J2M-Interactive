# Promotores y referidos

La atribución se conserva únicamente cuando el cliente concluye la verificación por correo. El enlace público contiene un código aleatorio estable, sin identificadores secuenciales ni datos personales. Los códigos inválidos, suspendidos, ajenos al negocio o de autorreferencia no generan atribución.

El contexto inicial se firma en el servidor y queda fijado al primer desafío pendiente. Editar el formulario o reenviar el registro no permite sustituir al promotor. El promotor vuelve a validarse al confirmar el correo y las claves foráneas compuestas impiden asociar negocio, promotor y cliente de tenants diferentes. Un código público sirve para atribución técnica, pero por sí solo no demuestra que una captación sea legítima.

El promotor usa una sesión web revocable independiente de gerente y cajero. Su portal solo presenta conteos agregados y nunca nombres, correos, teléfonos, saldos, QR de clientes ni movimientos. La suspensión o el restablecimiento de contraseña revocan todas sus sesiones y quedan auditados.

La migración `f9a0b1c2d3e4` es incremental. Su downgrade está bloqueado porque eliminar las tablas destruiría el historial de atribución. Antes de aplicarla en staging deben realizarse respaldo, `flask db current`, `flask db heads` y una ejecución controlada de `flask db upgrade`. Este cambio no se aplicó a Railway ni Supabase.

En staging y producción debe configurarse `PUBLIC_BASE_URL` con el origen HTTPS canónico, sin credenciales, ruta, consulta ni fragmento. Los QR y enlaces administrativos nunca toman un dominio no local del encabezado `Host`. En desarrollo, `localhost`, `127.0.0.1` y `::1` siguen permitidos para validación local.

No se implementan comisiones, pagos, recompensas automáticas ni exportaciones de datos personales. Si el esquema se expusiera mediante Supabase Data API, las tablas nuevas deberán mantenerse fuera de esquemas expuestos o cubrirse con RLS antes de habilitar acceso directo; la aplicación Flask no depende de Data API.
