# Personalización Wallet

Super Admin configura el diseño desde el detalle de cada negocio. Las preferencias son opcionales y los negocios históricos mantienen valores seguros derivados del nombre y tipo de programa.

## Compatibilidad

- Apple PassKit utiliza nombre del programa, colores de fondo, etiqueta y texto, instrucciones, términos, contacto y los recursos de logo autorizados. El mensaje breve se incluye como un `secondaryField` visible del `storeCard`; `description` conserva su función de metadato.
- Google Wallet utiliza los campos admitidos de `LoyaltyClass` y `LoyaltyObject`: nombre del programa, color de fondo, logo HTTPS, `heroImage` HTTPS, etiquetas y módulos de texto. Google calcula el contraste del texto; no existe un equivalente al `foregroundColor` de PassKit.
- Los presets Clásica, Intensa y Minimalista fueron retirados del editor porque las plataformas no permiten controlar esquinas, sombras, tipografía o distribución como HTML. La columna histórica `wallet_preset` se conserva para evitar una migración destructiva, pero no afecta los payloads.
- Los IDs, seriales, QR y balances no cambian al editar el diseño. La sincronización contable continúa bajo el worker existente.
- Las vistas previas son aproximaciones HTML con un titular y QR ficticios. No generan JWT, pases firmados ni llamadas externas.

## Operación

Aplicar la migración incremental `e8f9a0b1c2d3` antes de usar el editor. No ejecutarla automáticamente sobre staging o producción. Railway mantiene deshabilitadas las cargas locales mientras no exista almacenamiento persistente; puede usarse un logo HTTPS público.

Los modos Google Wallet `disabled`, `mock` y `real` se conservan. Un `.pkpass` generado localmente con la firma de desarrollo no demuestra validez en un dispositivo Apple. La apariencia final debe validarse con credenciales autorizadas y dispositivos físicos antes de producción.

## Tarjetas ya instaladas

- Guardar el editor solo persiste la configuración del negocio; no inicia una actualización masiva ni llama servicios externos.
- Las nuevas emisiones y la recuperación verificada usan la configuración vigente.
- Apple obtiene el diseño vigente cuando se descarga o vuelve a solicitar el `.pkpass`. Un pase ya instalado requiere que Wallet consulte una actualización después de una notificación PassKit/APNs; guardar el diseño por sí solo no envía esa notificación.
- Google actualiza la clase y los metadatos del objeto cuando el flujo real vuelve a ejecutar `ensure_resources`, por ejemplo durante una nueva emisión o recuperación. Guardar el diseño por sí solo no hace el `PATCH` remoto ni garantiza cuándo un pase instalado reflejará el cambio.
- La entrega efectiva en pases instalados depende de credenciales, servicios externos, conectividad y notificaciones reales. No se validó con dispositivos físicos en esta fase.

## Publicación controlada en Google Wallet

Super Admin dispone de una operación explícita «Publicar cambios en Google Wallet». Guardar incrementa la versión del diseño cuando cambia un campo compatible, pero nunca crea trabajos ni llama a Google. La confirmación muestra versión, modo y cantidad de objetos antes de encolar.

La revisión `8c9d0e1f2a3b` añade un outbox separado del outbox contable: conserva cada ejecución histórica de una versión y admite una sola ejecución activa por negocio, con un trabajo tenant-scoped por cliente. Cancelar detiene únicamente trabajos que todavía no se enviaron; una llamada ya aceptada por Google no puede deshacerse. Una ejecución cancelada o completada puede republicarse mediante confirmación explícita, creando otro intento auditado e incluyendo clientes añadidos después. El worker procesa en lotes con reclamaciones atómicas, leases recuperables, un máximo de seis intentos, backoff y fencing. En PostgreSQL, una conexión dedicada mantiene un advisory lock de sesión por cliente durante todo el HTTP y lo libera explícitamente antes de volver al pool; el worker de saldo usa la misma clave. Otro lock de sesión por negocio serializa las publicaciones de `LoyaltyClass` entre versiones: después de adquirirlo se revalida la versión y una versión nueva solo se envía cuando terminó la anterior. Ninguno de estos locks requiere mantener abierta una transacción ORM de negocio durante el HTTP. La entrega remota es al menos una vez: tras expirar un lease podría repetirse un PATCH idempotente, pero el token de fencing impide que un worker antiguo sobrescriba el estado vigente. Una versión anterior queda `superseded` y no puede sobrescribir una configuración posterior.

La clase conserva su ID. En cada objeto se leen primero los metadatos remotos y se combinan únicamente los módulos J2M (`program`, `reward`, `instructions`, `terms`, `contact` e `invite_friends`), manteniendo en orden textos, enlaces y atributos ajenos. El PATCH de publicación no contiene barcode ni `loyaltyPoints`. Un `GET 404` se registra como `skipped/not_issued` y nunca crea un objeto: la emisión permanece reservada a los flujos autorizados de entrega del pase. Solo se incluye el enlace público de referido cuando el participante ya está habilitado y consentido; nunca se publica una capacidad temporal de activación.

En modo `mock`, los trabajos se registran y terminan como simulación, pero la interfaz identifica el modo y no afirma que Google haya aplicado cambios. `disabled` rechaza el encolado. Una validación real en staging requiere migración controlada, credenciales autorizadas, una publicación de alcance conocido y comprobación del objeto con Google y del dispositivo; este PR no la ejecuta.

Apple Wallet/PassKit y APNs se implementan en la revisión `9d0e1f2a3b4c` mediante una cola separada y publicación explícita. La activación real sigue condicionada a certificados Apple, HTTPS público, worker dedicado y validación física; los modos de prueba no se presentan como sincronización real.

## Vista previa de las instrucciones

El editor diferencia «Mensaje de la tarjeta» de «Cómo utilizar la tarjeta (detalles)». Las instrucciones se previsualizan en tiempo real en secciones de detalles: el reverso de Apple y los módulos de información de Google, no en el frente. Se renderizan como texto con `textContent`, no como HTML. Las vistas previas son aproximaciones: guardar no hace `PATCH` de tarjetas instaladas ni envía una notificación de actualización.
