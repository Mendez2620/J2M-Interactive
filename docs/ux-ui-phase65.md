# Fase 6.5: sistema visual y experiencia

## Sistema visual

La interfaz usa Tailwind compilado localmente, tipografías del sistema y componentes reutilizables para botones, campos, tarjetas, estados de foco, carga y deshabilitado. La iconografía procede de `app/static/icons.svg`; el helper Jinja solo admite identificadores incluidos en su catálogo y no acepta SVG aportado por usuarios. No se requieren fuentes ni scripts remotos.

Los negocios pueden elegir los temas Moderno, Elegante o Minimalista; tipografía Moderna, Clásica o Geométrica; y texto Compacto, Estándar o Grande. Los valores son presets cerrados. Los colores se validan como `#RRGGBB` y el texto de la página pública selecciona blanco o negro para mejorar el contraste sin modificar el color de marca guardado. Los negocios existentes reciben Moderno/Moderna/Estándar mediante una migración incremental.

La portada pública usa recorte proporcional y el logo usa ajuste sin deformación. Las vistas están preparadas para teléfono, tableta y escritorio; las tablas conservan desplazamiento horizontal contenido cuando no es posible convertirlas a tarjetas.

## Registro público y teléfono

`/join/<slug>` muestra un hero acotado, marca, descripción y formulario. El selector incluye México (+52) de forma predeterminada, Estados Unidos/Canadá (+1), Colombia (+57), Argentina (+54) y España (+34). El servidor elimina separadores permitidos, valida prefijo y longitud, y guarda nuevos números en E.164. Los números históricos no se reescriben.

## Enlaces y búsquedas

Los enlaces públicos se construyen con `PUBLIC_BASE_URL` cuando está configurado y con el origen de la solicitud en desarrollo. El directorio permite abrir y copiar. Las búsquedas del directorio, clientes y movimientos usan `fetch`, debounce de 300 ms, cancelación de solicitudes anteriores, paginación, URL sincronizada y botón Atrás. El servidor sigue haciendo filtros y paginación bajo la autenticación y el tenant existentes. Sin JavaScript, los formularios funcionan de forma tradicional.

## Usuarios y canales

La acción Gestionar usuario reúne permisos, perfil, canales, estado, contraseña y zona de peligro. Los endpoints y permisos existentes siguen siendo la autoridad. Un usuario con movimientos históricos no puede borrarse físicamente: debe desactivarse para conservar transacciones y auditoría.

Los controles del negocio suspenden por completo el portal web o la aplicación móvil para ese tenant. Los controles individuales no sustituyen esa habilitación: ambos niveles deben permitir el canal. Los cambios sensibles conservan revocación de sesiones y auditoría.

## Acceso del gerente

El formulario normal solicita correo y contraseña. El correo se normaliza para la búsqueda. Si las credenciales verificadas corresponden a varias cuentas autorizadas, se guarda durante cinco minutos una lista de identificadores en la sesión firmada y solo entonces se muestran los negocios para elegir. Una contraseña incorrecta no revela cuentas. El login móvil y JWT no cambian.

## Lealtad, Wallet, estadísticas y reportes

`points_per_currency` significa puntos otorgados por cada $1 MXN; una compra de $100 con valor 2 otorga 200 puntos. En sellos, cada operación elegible acumula un sello hasta la meta configurada. El cooldown es el tiempo mínimo entre acumulaciones del mismo cliente y no una prohibición general de canjes. Cambiar el tipo no convierte saldos históricos.

J2M Interactive es la fuente de identidad y textos. Apple Wallet y Google Wallet deciden la presentación final según sus capacidades. La ubicación es opcional y no implica rastreo ni geocerca antifraude. Emisión real, apariencia final y disponibilidad dependen de credenciales, certificados, consolas y dispositivos externos; los modos mock se mantienen.

El dashboard acepta últimas 24 horas, 7 días y 30 días. Usa intervalos horarios para 24 horas y diarios para los demás períodos, incluyendo intervalos vacíos y evitando divisiones por cero. Las compras asociadas a lealtad no se presentan como ingresos totales. Los reportes conservan rutas, permisos, MIME, nombres, aislamiento y sanitización existentes.

## Actualización

Ejecutar `flask db upgrade` para aplicar `e2f3a4b5c6d7`, después `npm ci` y `npm run build:css` si se reconstruyen assets. La reversión está bloqueada porque eliminar las preferencias visuales no es una operación segura. Validar siempre en una copia de la base antes de producción.
