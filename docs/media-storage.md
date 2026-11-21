# Almacenamiento persistente de imágenes de negocio

## Decisión de 8E.1A

J2M usa un adaptador de almacenamiento y no escribe directamente desde las rutas administrativas. Para el host Docker único se seleccionó filesystem persistente mediante un volumen nombrado y aislado por proyecto Compose. Es la alternativa de menor coste, no requiere un proveedor y permite respaldo y restauración portables. Los backends de lectura y escritura de producción continúan en `disabled` hasta que el propietario apruebe su activación y el hostname HTTPS público.

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

La clave se valida con una gramática cerrada y el adapter comprueba el `business_id` esperado. No se aceptan rutas absolutas, `..`, nombres proporcionados por el usuario ni extensiones fuera de la lista. `/media/<clave>` solo sirve una clave asociada actualmente como logo o portada al mismo negocio, o una versión anterior con un marcador de retención válido para ese mismo tenant. No lista objetos, temporales ni archivos sin referencia, y nunca revela rutas del host.

Las rutas históricas `uploads/<archivo>` continúan resolviéndose desde `/static/uploads/...`. Las URL HTTPS configuradas en `logo_url` no cambian.

## Validación y publicación

- Límite por archivo: 5 MB; límite multipart: 12 MB.
- Formatos binarios admitidos: PNG, JPEG y WEBP, verificados por Pillow.
- Máximo predeterminado: 4096 x 4096 y 16 millones de píxeles.
- No se admiten animaciones. La imagen se decodifica por completo y se vuelve a codificar sin EXIF, ICC ni metadatos auxiliares; la transparencia real de PNG/WEBP se conserva.
- El resultado recodificado vuelve a comprobarse contra `MAX_NORMALIZED_IMAGE_BYTES` (5 MB de forma predeterminada), aunque el archivo fuente estuviera dentro del límite.
- La escritura usa un temporal en el mismo directorio, `fsync` y reemplazo atómico.
- Un lote se valida por completo antes de escribir. Si falla una escritura o la transacción, se retiran solo los objetos nuevos administrados.
- Al sustituir una imagen administrada, la anterior recibe dentro de la misma operación un marcador privado de retención ligado al negocio y a la versión de diseño que la sustituyó. Guardar branding no encola ni confirma una publicación Wallet y, por ello, no elimina la URL anterior.
- La actualización bloquea transaccionalmente la fila del negocio (`SELECT ... FOR UPDATE` en PostgreSQL) antes de leer las rutas vigentes. Dos sustituciones del mismo tenant se serializan; el rollback de una no puede retirar el marcador creado por otra ni perder incrementos de versión.
- Los errores públicos de almacenamiento son genéricos; las rutas internas del host nunca forman parte de la respuesta.

Google Wallet recibe una URL absoluta `https://<origen>/media/<clave>`. Apple Wallet lee el mismo objeto desde el adapter al generar el pase. Ambos conservan sus identificadores, barcode, saldos y módulos existentes. El origen público debe ser HTTPS estable antes de activar este backend; el HTTPS local no es consumible por Wallet.

No se implementa eliminación automática en 8E.1A. Una herramienta de mantenimiento futura solo podrá retirar una versión anterior cuando: (1) ya no sea la referencia actual; (2) su marcador pertenezca al mismo negocio; (3) la publicación Google correspondiente figure completada para una versión igual o posterior y se haya comprobado el proveedor aplicable; (4) hayan transcurrido al menos 90 días desde esa confirmación; y (5) exista un respaldo cifrado verificado. Si Apple se habilita y consume esa imagen, también deberá confirmarse su actualización antes de eliminarla. Con Wallet deshabilitado o una publicación fallida, la versión retenida no es elegible. La limpieza debe ser explícita, auditable e idempotente; nunca se ejecuta al guardar el formulario.

## Docker, respaldo y recuperación

`compose.selfhost.yml` usa el proyecto operativo persistente `j2m-cutover-ready` por defecto y declara el volumen lógico `j2m-media`; Docker lo materializa como `j2m-cutover-ready_j2m-media`. Un ensayo debe usar siempre otro nombre con `docker compose -p <proyecto-aislado> ...`, por lo que obtiene otro volumen y no puede compartir accidentalmente el operativo. No se fija un nombre global de volumen.

Solo `web`, que valida, escribe y sirve los medios, monta ese volumen con escritura; worker y migrador no lo montan. Un consumidor futuro que no escriba debe montarlo explícitamente como solo lectura. El filesystem del contenedor continúa read-only fuera de sus volúmenes declarados. No usar `docker compose down -v` ni eliminar el volumen operativo durante reconstrucciones. Esta fase no crea, modifica ni elimina el volumen productivo.

Antes de habilitar subidas reales se debe ampliar, revisar y activar por separado el sistema de respaldos para:

1. crear un snapshot consistente del volumen;
2. cifrarlo y autenticarlo antes de conservarlo o transferirlo;
3. registrar el SHA-256 y el identificador de la versión de aplicación;
4. restaurarlo semanalmente en un volumen aislado y comprobar que las claves referenciadas por la base existen;
5. restaurar base de datos y medios desde el mismo punto temporal cuando sea necesario.

Esta fase **no modifica las tareas instaladas**. `UPLOAD_STORAGE_BACKEND` controla únicamente nuevas escrituras, mientras `MEDIA_STORAGE_BACKEND` controla la lectura de recursos ya persistidos. Esto permite suspender cargas sin romper imágenes publicadas:

```ini
UPLOAD_STORAGE_BACKEND=disabled
MEDIA_STORAGE_BACKEND=local
MEDIA_STORAGE_ROOT=/app/instance/media
```

La combinación anterior solo debe activarse después de montar y validar el volumen existente. Con ambos valores en `disabled`, ni se cargan ni se sirven recursos administrados. Hasta aprobar y probar respaldo, origen HTTPS y recuperación, producción debe conservar ambos en `disabled`. En desarrollo o ensayos aislados se habilitan con:

```ini
UPLOAD_STORAGE_BACKEND=local
MEDIA_STORAGE_BACKEND=local
MEDIA_STORAGE_ROOT=/app/instance/media
```

La activación futura debe comprobar permisos del volumen, respaldo cifrado reciente, restauración aislada y disponibilidad de `/media/...` mediante el origen HTTPS definitivo.
