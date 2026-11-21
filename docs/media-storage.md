# Almacenamiento persistente de imágenes de negocio

## Decisión de 8E.1A

J2M usa un adaptador de almacenamiento y no escribe directamente desde las rutas administrativas. Para el host Docker único se seleccionó filesystem persistente mediante el volumen nombrado `j2m-media`. Es la alternativa de menor coste, no requiere un proveedor y permite respaldo y restauración portables. El backend de producción continúa en `disabled` hasta que el propietario apruebe su activación y el hostname HTTPS público.

| Criterio | Volumen local persistente | Almacenamiento de objetos |
| --- | --- | --- |
| Coste inicial | Sin coste adicional | Depende del proveedor, transferencia y operaciones |
| Portabilidad | Exportación/restauración explícita del volumen | API y herramientas del proveedor |
| Exposición pública | La aplicación controla `/media/...` | Puede usar CDN o URL firmada/pública |
| Recuperación | Debe incluirse en respaldos cifrados | Versionado y replicación según proveedor |
| Escalado multi-host | Requiere volumen compartido | Adecuado para varios hosts |

Un futuro adapter de objetos puede implementar el mismo contrato sin cambiar los endpoints administrativos ni las columnas históricas.

## Claves y aislamiento

Los recursos nuevos usan claves generadas por el servidor:

```text
businesses/<business_id>/<logo|cover>/<uuid-aleatorio>.<png|jpg|webp>
```

La clave se valida con una gramática cerrada y el adapter comprueba el `business_id` esperado. No se aceptan rutas absolutas, `..`, nombres proporcionados por el usuario ni extensiones fuera de la lista. `/media/<clave>` solo sirve una clave que siga asociada como logo o portada al mismo negocio; no lista objetos ni revela rutas del host.

Las rutas históricas `uploads/<archivo>` continúan resolviéndose desde `/static/uploads/...`. Las URL HTTPS configuradas en `logo_url` no cambian.

## Validación y publicación

- Límite por archivo: 5 MB; límite multipart: 12 MB.
- Formatos binarios admitidos: PNG, JPEG y WEBP, verificados por Pillow.
- Máximo predeterminado: 4096 x 4096 y 16 millones de píxeles.
- No se admiten animaciones. La imagen se decodifica por completo y se vuelve a codificar; así se eliminan metadatos, nombres originales y payloads anexos.
- La escritura usa un temporal en el mismo directorio, `fsync` y reemplazo atómico.
- Un lote se valida por completo antes de escribir. Si falla una escritura o la transacción, se retiran solo los objetos nuevos administrados.
- Al sustituir una imagen, la anterior se conserva hasta confirmar la transacción y después se elimina. Nunca se borran rutas históricas ni URL externas.

Google Wallet recibe una URL absoluta `https://<origen>/media/<clave>`. Apple Wallet lee el mismo objeto desde el adapter al generar el pase. Ambos conservan sus identificadores, barcode, saldos y módulos existentes. El origen público debe ser HTTPS estable antes de activar este backend; el HTTPS local no es consumible por Wallet.

## Docker, respaldo y recuperación

`compose.selfhost.yml` monta `j2m-selfhost-media` en `/app/instance/media`. El contenedor sigue siendo read-only salvo por ese volumen. No usar `docker compose down -v` ni eliminar el volumen durante reconstrucciones.

Antes de habilitar subidas reales se debe ampliar, revisar y activar por separado el sistema de respaldos para:

1. crear un snapshot consistente del volumen;
2. cifrarlo y autenticarlo antes de conservarlo o transferirlo;
3. registrar el SHA-256 y el identificador de la versión de aplicación;
4. restaurarlo semanalmente en un volumen aislado y comprobar que las claves referenciadas por la base existen;
5. restaurar base de datos y medios desde el mismo punto temporal cuando sea necesario.

Esta fase **no modifica las tareas instaladas**. Hasta aprobar y probar ese procedimiento, producción debe mantener `UPLOAD_STORAGE_BACKEND=disabled`. En desarrollo o ensayos aislados se habilita con:

```ini
UPLOAD_STORAGE_BACKEND=local
MEDIA_STORAGE_ROOT=/app/instance/media
```

La activación futura debe comprobar permisos del volumen, respaldo cifrado reciente, restauración aislada y disponibilidad de `/media/...` mediante el origen HTTPS definitivo.
