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
