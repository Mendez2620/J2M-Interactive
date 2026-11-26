# Reconciliación administrativa de Google Wallet

Las cookies de Super Admin emitidas antes de instalar la migración no tienen
una sesión persistente asociada. Después de un despliegue autorizado, los
administradores deberán iniciar sesión nuevamente.

La ejecución está deshabilitada por defecto. Antes de iniciar una escritura
administrativa, el transporte comprueba la autorización y la propiedad de la
reclamación. Una revocación posterior a ese punto no cancela una petición ya
autorizada o recibida por el proveedor.

Una reclamación interrumpida no se vuelve a encolar. El diagnóstico del negocio
muestra su estado y fecha aun cuando la sesión original haya vencido. El
detalle de la selección y su confirmación siguen vinculados a la sesión que
creó la revisión.
