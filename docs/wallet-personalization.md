# Personalización Wallet

Super Admin configura el diseño desde el detalle de cada negocio. Las preferencias son opcionales y los negocios históricos mantienen valores seguros derivados del nombre y tipo de programa.

## Compatibilidad

- Apple PassKit utiliza nombre del programa, texto descriptivo, colores de fondo, etiqueta y texto, instrucciones, términos, contacto y los recursos de logo/portada ya autorizados.
- Google Wallet utiliza los campos admitidos de `LoyaltyClass` y `LoyaltyObject`: nombre del programa, color de fondo, logo HTTPS, `heroImage` HTTPS, etiquetas y módulos de texto.
- Los IDs, seriales, QR y balances no cambian al editar el diseño. La sincronización contable continúa bajo el worker existente.
- Las vistas previas son aproximaciones HTML con un titular y QR ficticios. No generan JWT, pases firmados ni llamadas externas.

## Operación

Aplicar la migración incremental `e8f9a0b1c2d3` antes de usar el editor. No ejecutarla automáticamente sobre staging o producción. Railway mantiene deshabilitadas las cargas locales mientras no exista almacenamiento persistente; puede usarse un logo HTTPS público.

Los modos Google Wallet `disabled`, `mock` y `real` se conservan. Un `.pkpass` generado localmente con la firma de desarrollo no demuestra validez en un dispositivo Apple. La apariencia final debe validarse con credenciales autorizadas y dispositivos físicos antes de producción.
