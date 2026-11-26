# Inventario operativo de transición (preparación parcial)

Este comando solo observa el checkout local, Docker y archivos de respaldo.
No contacta Supabase ni Google, no ejecuta Docker Compose, no cambia el pin
y no autoriza la transición. Ejecutarlo desde un checkout limpio y actualizado
después de fusionar el código, con la configuración privada autorizada:

    python -m scripts.selfhost_transition_preflight --approved-sha <SHA-main> --env-file .env.selfhost --backup-dir selfhost-backups/hourly --key-file <ruta-DPAPI-privada>

El SHA debe coincidir con HEAD y origin/main **local**. Confirmar el remoto y
su CI por separado. El inventario compara el tag legado y su ID inmutable,
las imágenes activas, la red, los modos Wallet, los puertos loopback y la
exclusividad del worker local por etiquetas. No demuestra ausencia de
escritores externos, ni prueba identidad HTTPS o permisos efectivos.

El conjunto integral candidato se selecciona por fecha de archivo; si no
autentica, se bloquea sin sustituirlo silenciosamente por uno antiguo.
La antigüedad (máximo 90 minutos) se comprueba contra el índice cifrado,
no contra mtime. Se verifica el índice y los dos componentes mediante
verify_integral_set. Se informa el hash del índice exacto. No es una prueba
de la revisión Alembic del dump ni de una restauración integral reciente.
El inventario no adquiere el bloqueo de respaldo: repetir la verificación
bajo exclusión durante la ventana futura.

Salida 1: alguna observación falló. Salida 2: observaciones completadas,
**NO-GO operativo**. El campo transition_authorized siempre es false.
No se imprime el contenido de variables privadas, respuestas Docker ni
rutas de archivos privados.

## Acciones aún necesarias

- Autorizar por separado restaurar el conjunto exacto en Docker aislado
  con restore-integral, bajo el bloqueo de respaldo existente, comprobar
  su hash antes y después, revisión y medios, sin restaurar sobre Supabase.
- Confirmar afinidad efectiva del pooler mediante sonda autorizada.
- Demostrar exclusión de escritores locales y externos y migradores con
  una barrera de acceso efectiva; la inspección local no basta. Coordinar
  tareas programadas y mantener mantenimiento tras una interrupción.
- Integrar atestación HTTPS, respaldo/restauración, barrera y diario en
  el flujo operativo antes de habilitar cualquier comando de transición.
  Mantener el bloqueo general de la imagen legada. No se autoriza una
  excepción mediante un JSON o un argumento genérico.


## Sonda de afinidad independiente (no ejecutada)

El propietario debe confirmar primero que la URL privada corresponde al
Session pooler (puerto 5432), el hostname exacto y la referencia del
proyecto, y autorizar expresamente dos conexiones TLS y un lock de
diagnóstico de sesión. No ejecutar como parte del preflight.

    python -m scripts.probe_authorized_pooler --env-file .env.selfhost --expected-host <hostname-session-pooler> --expected-project-ref <project-ref> --authorize-session-lock-probe

Se rechazan otros destinos, puertos, parámetros de conexión inesperados
y variables PG heredadas. Se exige CA local y verify-full. Se usa una
clave bigint negativa aleatoria, no los namespaces de dos enteros de
la aplicación. No se escriben tablas. Se comprueban los dos PID tras
COMMIT, exclusión y liberación. En caso de pérdida de afinidad, error
de transporte o liberación no confirmada, se cierran las conexiones y
se informa NO-GO; un lock de diagnóstico podría permanecer en una sesión
del pooler hasta que esta termine. No se matan sesiones ni se libera
ninguna otra clave. El resultado positivo no autoriza el despliegue.


## Restauración exacta independiente (no ejecutada)

Después de obtener el hash del índice y el ID inmutable del legado en
el inventario, solicitar autorización explícita para restaurar esa copia
real en una red y volumen Docker aislados. Reutiliza restore_integral_isolated,
con el bloqueo de respaldo existente y el índice exacto; no usa una
conexión a Supabase. La restauración ejecuta la imagen por ID inmutable,
verifica el índice y componentes bajo el bloqueo y exige la revisión
inicial. Revalida el conjunto y el tag al finalizar.

    python -m scripts.verify_transition_restore --index <archivo-integral-exacto> --key-file <ruta-DPAPI-privada> --expected-index-sha256 <hash> --expected-legacy-image-id <sha256:id> --authorize-isolated-restore

No ejecutar sin aprobación expresa ni utilizar el resultado como permiso
de transición. La evidencia no se conserva automáticamente; no acredita
una barrera de escritores ni el estado de la base después de la copia.
