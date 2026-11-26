# Panel de reconciliación con datos sintéticos

Capturas de Microsoft Edge con Playwright. La aplicación se ejecutó en
loopback con SQLite en memoria. Los estados de resultado se cargaron como
datos sintéticos y se renderizaron mediante las rutas normales del panel.
La captura no inicia reconciliaciones ni conecta con el proveedor.

| Ancho | Diagnóstico | Selección | Deshabilitada | Confirmación | Resultado confirmado | Resultado parcial |
| --- | --- | --- | --- | --- | --- | --- |
| 1440 px | [Ver](1440-diagnostico.png) | [Ver](1440-seleccion.png) | [Ver](1440-deshabilitada.png) | [Ver](1440-confirmacion.png) | [Ver](1440-confirmado.png) | [Ver](1440-parcial.png) |
| 768 px | [Ver](768-diagnostico.png) | [Ver](768-seleccion.png) | [Ver](768-deshabilitada.png) | [Ver](768-confirmacion.png) | [Ver](768-confirmado.png) | [Ver](768-parcial.png) |
| 390 px | [Ver](390-diagnostico.png) | [Ver](390-seleccion.png) | [Ver](390-deshabilitada.png) | [Ver](390-confirmacion.png) | [Ver](390-confirmado.png) | [Ver](390-parcial.png) |
| 320 px | [Ver](320-diagnostico.png) | [Ver](320-seleccion.png) | [Ver](320-deshabilitada.png) | [Ver](320-confirmacion.png) | [Ver](320-confirmado.png) | [Ver](320-parcial.png) |

La prueba verifica anchos, errores de consola, selección con la barra
espaciadora, etiquetas accesibles, contador y confirmación sin preselección.
Se reproduce desde la raíz del repositorio con
`python -m scripts.capture_wallet_review`, Playwright y Microsoft Edge.
