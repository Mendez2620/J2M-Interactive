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
