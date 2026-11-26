# Transición histórica de Google Wallet

El 26 de noviembre de 2026, el diagnóstico autorizado confirmó que los cuatro
objetos históricos están `ACTIVE` y que el color explícito local y remoto
normaliza a `#1E3A8A`. La diferencia anterior era de representación.
Los cuatro conservaban identidad, saldo y QR en la comprobación previa.
No se realizaron escrituras. No reutilice este emisor con otra base que
pueda asignar los mismos identificadores.

## Evidencia reproducible

- CI ejecuta `python tests/wallet_egress_ci.py`, además del ensayo Nginx
  existente. Squid y un servidor TLS ficticio usan dos redes internas.
  Los dos nombres permitidos apuntan exclusivamente al servidor ficticio.
  Se comprueba `CONNECT` en 443, validación del certificado del destino y
  rechazo de nombres similares, subdominios, IP literal, otros puertos
  y peticiones directas. La topología no incorpora una red de producción.
- En la laptop con la imagen histórica original disponible, ejecute
  `python tests/legacy_wallet_transition_docker.py`. Prepare primero la
  imagen de ensayo revisada bajo `j2m-interactive:wallet-demo-synthetic`.
  El ensayo resuelve la imagen legada por ID inmutable y usa PostgreSQL
  desechable. Comprueba login, compra, canje, ledger, trabajo histórico,
  secuencia de clientes, esquema y una reserva incierta sintética.
  Verifica el ID de web y worker, y correlaciona cada petición HTTPS
  con el upstream efectivo en Nginx antes y después de recrear web.
  Se conservan las imágenes originales y los volúmenes operativos.
- La prueba de login acredita que se puede emitir una sesión en ambos
  esquemas con la versión anterior. Presupueste reautenticación de los
  usuarios en el corte. La compatibilidad de cookies entre versiones
  y el comportamiento de reservas con Wallet real requieren pruebas
  separadas.

## Ventana futura desde dae140e

1. Exija autorización específica, respaldo integral reciente y una
   restauración verificada en otra base. Registre el ID inmutable de cada
   imagen y contenedor, las revisiones Alembic, el pin y las reservas
   activas. Tome como referencia el recuento de movimientos y el ledger.
2. Confirme la evidencia del ensayo legado sobre la misma pareja de
   imágenes y esquema. Prepare un mecanismo de verificación operativa
   que correlacione un nonce impredecible en HTTPS con el upstream
   concreto en un log de Nginx de confianza y con la IP del contenedor
   autorizado. Exija una correlación para health y ready.
3. Con ambos Wallet deshabilitados, entre en mantenimiento y detenga
   escrituras de web y worker, deje terminar las transacciones en curso
   y compruebe que no hay otro escritor. Migre una vez de `9d0e1f2a3b4c`
   a `c3d4e5f6a7b8`. Valide el esquema y compare movimientos, ledger
   y reservas antes de reabrir.
4. Verifique imagen, red, sesión administrativa, HTTPS y un solo worker
   antes de reabrir tráfico. Ante un fallo, mantenga la pausa. Si la
   compatibilidad quedó demostrada, recupere imagen y pin anteriores
   conservando la base migrada y exija nueva evidencia de identidad.
   Si no puede certificarla, mantenga mantenimiento y corrija hacia
   adelante. No restaure una copia antigua sobre los movimientos nuevos.
5. El bloqueo general conserva su vigencia. Este procedimiento exige
   una implementación operativa revisada de la evidencia antes de cualquier
   excepción específica. No implica un permiso de activación.

## Egreso futuro

El proxy solo discrimina host, puerto y CONNECT. OAuth, GET y escrituras
futuras deben quedar separados en el transporte de aplicación. El
diagnóstico autorizado usa destinos exactos, TLS verificado y rechaza
redirecciones y reintentos. No instale certificados de interceptación.
Antes de abrir egreso, demuestre en el entorno real la ausencia de otras
interfaces, rutas directas y acceso del gateway hacia Supabase. Una
red interna de ensayo no certifica la topología operativa.

## Ensayo ejecutable y límites de operación

Con ambas imágenes disponibles localmente, desde el checkout limpio:

```powershell
$revision = (git rev-parse HEAD).Trim()
docker build --label "org.opencontainers.image.revision=$revision" -t "j2m-interactive:transition-$revision" .
python tests/legacy_wallet_transition_docker.py --preflight-only
python tests/legacy_wallet_transition_docker.py --journal (Join-Path $env:TEMP "j2m-transition-$revision.jsonl")
```

El script usa una red interna nueva y PostgreSQL 16 efímero. Crea un
dump sintético en memoria, lo restaura en otra base de la instancia aislada,
verifica la revisión inicial y exige que solo el contenedor de PostgreSQL
permanezca conectado antes de migrar. El flujo comprueba el esquema final,
prueba ambos backends y simula la pérdida del backend objetivo antes de
recuperar la imagen legada sin bajar la revisión de la base. Su diario
registra las etapas e identidades. Un archivo previo no se reutiliza.
La validación del diario comprueba la secuencia, procedencia y vigencia.
No es una autorización, firma ni sustituto de pruebas directas.

### Condiciones aún exigidas para operación

El flujo operativo y el pin no cambian en este PR. Antes de solicitar una
ventana real, integre el preflight con `verify_integral_set` y una
restauración integral aislada del mismo conjunto reciente, incluida la
revisión del respaldo, cifrado, manifiesto, medios, clave DPAPI y capacidad
de disco. El dump ficticio del ensayo no demuestra todo eso.

Acredite una pausa de todos los escritores, incluido acceso directo al
web, servicios externos, migradores y tareas de respaldo. Ante PostgreSQL
inaccesible, estado Alembic incierto, fallo del proxy o interrupción,
mantenga mantenimiento. Revalide revisión e integridad antes de decidir si
recuperar el legado con el esquema nuevo o corregir hacia adelante. No
reanude a partir del diario, no haga downgrade y no restaure una copia
antigua sobre movimientos posteriores.

Para la futura afinidad del pooler, autorice una sonda separada que use dos
conexiones independientes con TLS. Compare la identidad de sesión antes y
después de COMMIT y pruebe exclusión y liberación con una clave reservada
para esa sonda. Cierre ambas conexiones aunque falle y no termine sesiones
ajenas. La sonda existente solo admite PostgreSQL sintético. En esta fase no
se ejecuta esa prueba sobre Supabase.
