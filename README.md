# J2M Interactive — Plataforma SaaS de Tarjetas de Lealtad Digitales

Plataforma SaaS Multi-Tenant para la emisión, gestión y sincronización en tiempo real de **Tarjetas de Lealtad Digitales** compatibles con **Apple Wallet** (`.pkpass`) y **Google Wallet**.

---

## 🚀 Características Principales

- 🏢 **Multi-Tenant / Marca Blanca**: Cada negocio personaliza su nombre, logo, colores hexadecimales primarios/secundarios, coordenadas geográficas y reglas de lealtad.
- 🏷️ **Soporte Dual de Programas**:
  - **Sellos / Estampas (`stamps`)**: Acumulación por visita y canje automático de recompensas.
  - **Puntos (`points`)**: Multiplicador por monto de compra ($) y canje flexible de puntos.
- 📱 **Pases Nativos de Wallet**:
  - **Google Wallet**: Generación de JWTs firmados con **RS256** y llamadas a la REST API de Google Wallet.
  - **Apple Wallet**: Empaquetado binario `.pkpass` en memoria con firma digital PKCS7, manifest SHA-1 y activos gráficos.
- 🌐 **Web Service Oficial de Apple Wallet (`/api/apple/v1`)**:
  - Registro de dispositivos (`pushToken`).
  - Sincronización automática de saldos en tiempo real mediante notificaciones push APNs silenciosas.
  - Entrega del pase `.pkpass` actualizado en la pantalla de bloqueo de iOS.
- 📍 **Geofencing & Notificaciones de Proximidad**: Muestra la tarjeta automáticamente en la pantalla bloqueada del cliente cuando se aproxima a la sucursal física.
- 🎂 **Registro Público con Cumpleaños (`/join/<slug>`)**: Onboarding responsivo para clientes con captura de fecha de nacimiento y botones directos de guardado en Wallet.
- 📊 **Panel Super Admin (`/admin`)**: Dashboard con KPIs consolidados, volumen de ventas, métricas por sucursal, distribución demográfica por edades y gestión de usuarios (Gerentes y Cajeros).
- 📷 **Web App de Escaneo Adaptativa (`/scanner`)**: Terminal móvil con lector de cámara QR (`html5-qrcode`), entrada manual / NFC, y botones de transacción adaptados al programa del negocio.

---

## 📂 Arquitectura del Proyecto

```text
J2M Interactive/
├── app/
│   ├── __init__.py                 # Application Factory (create_app)
│   ├── config.py                   # Configuraciones por entorno (.env)
│   ├── extensions.py               # SQLAlchemy, Migrate, CORS
│   ├── models/                     # Modelos de Base de Datos (ORM)
│   │   ├── __init__.py
│   │   ├── apple_device.py         # Registro de dispositivos Apple para APNs
│   │   ├── business.py             # Tenant / Negocio y Geocercas
│   │   ├── customer.py             # Cuentas de clientes, saldos y cumpleaños
│   │   ├── staff.py                # Usuarios Staff (Admin, Manager, Cashier)
│   │   └── transaction.py          # Historial de acumulación y canjes
│   ├── routes/                     # Controladores y Endpoints
│   │   ├── __init__.py             # Registro de Blueprints y Health Check
│   │   ├── admin.py                # Dashboard y Gestión Super Admin
│   │   ├── apple_webservice.py     # Protocolo REST API de Apple Wallet
│   │   ├── auth.py                 # Módulo de Autenticación
│   │   ├── business.py             # Registro público /join/<slug>
│   │   ├── customer.py             # Descarga y enlaces de pases
│   │   └── scanner.py              # Web App y API de Escaneo
│   ├── services/                   # Lógica de Integración con Wallets
│   │   ├── __init__.py
│   │   ├── apple_wallet.py         # PKCS7, pass.json y .pkpass ZIP builder
│   │   └── google_wallet.py        # RS256 JWTs y Google Wallet REST API
│   ├── templates/                  # Vistas HTML (Tailwind CSS)
│   │   ├── base.html               # Layout base responsivo
│   │   ├── join.html               # Formulario de registro público
│   │   ├── scanner.html            # Web app móvil del escáner
│   │   └── admin/
│   │       └── dashboard.html      # Panel Super Admin y métricas
│   └── utils/
│       └── __init__.py
├── tests/                          # Suite completa de pruebas unitarias
│   ├── test_apple_wallet.py        # Tests de generación .pkpass y firmas
│   ├── test_google_wallet.py       # Tests de JWTs y esquemas Google Wallet
│   ├── test_phase3.py              # Tests de registro, admin y transacciones
│   └── test_phase4.py              # Tests de Web Service Apple y parches REST
├── .env.example                    # Plantilla de variables de entorno
├── .gitignore                      # Exclusiones de Git (venv, keys, .pkpass)
├── requirements.txt                # Dependencias Python
└── run.py                          # Punto de entrada de la aplicación
```

---

## 🛠️ Instalación y Ejecución Local

### 1. Prerrequisitos
- Python 3.10+
- Git

### 2. Clonar el repositorio y crear el entorno virtual
```bash
git clone https://github.com/Mendez2620/J2M-Interactive.git
cd "J2M Interactive"

# Crear entorno virtual
python -m venv venv

# Activar en Windows (PowerShell)
.\venv\Scripts\Activate.ps1
# O en Linux/macOS:
# source venv/bin/activate
```

### 3. Instalar dependencias
```bash
pip install -r requirements.txt
```

### 4. Configurar variables de entorno
Copia la plantilla `.env.example` a `.env`:
```bash
cp .env.example .env
```

En desarrollo local, el sistema utiliza **SQLite** (`sqlite:///j2m_loyalty.db`) y genera automáticamente claves criptográficas transitorias si no se configuran certificados reales.

### 5. Iniciar la aplicación
```bash
python run.py
```
La aplicación estará disponible en:
- **Página de Escáner**: `http://127.0.0.1:5000/scanner`
- **Panel Super Admin**: `http://127.0.0.1:5000/admin`
- **Health Check**: `http://127.0.0.1:5000/api/health`

---

## 🧪 Ejecución de Pruebas Unitarias

El proyecto cuenta con una suite completa de **18 pruebas unitarias e integración**:

```bash
python -m unittest discover tests
```

---

## 🔗 Rutas y Endpoints Principales

| Método | Endpoint | Descripción |
| :--- | :--- | :--- |
| `GET` | `/api/health` | Health Check de la aplicación |
| `GET` | `/join/<slug>` | Página pública de registro de clientes |
| `POST` | `/join/<slug>` | Procesa registro, guarda cumpleaños y retorna enlaces a Wallets |
| `GET` | `/admin` | Panel de Control Super Admin con métricas demográficas |
| `POST` | `/admin/business/<id>/staff` | Creación de Gerentes y Cajeros |
| `GET` | `/scanner` | Terminal web de escaneo QR adaptativa |
| `POST` | `/api/scanner/lookup` | Búsqueda de cliente por QR token, ID o teléfono |
| `POST` | `/api/scanner/process` | Procesa abono/canje y dispara sincronización en tiempo real |
| `GET` | `/api/customers/<id>/wallet/google` | Obtiene URL directa para Google Wallet |
| `GET` | `/api/customers/<id>/wallet/apple` | Descarga directa del pase nativo `.pkpass` |
| `POST` | `/api/apple/v1/devices/.../registrations/...` | Registro oficial de dispositivo Apple para push APNs |
| `GET` | `/api/apple/v1/passes/<pass_type>/<serial>` | Entrega del `.pkpass` actualizado tras transacción |

---

## 🔐 Configuración para Producción

### Google Wallet
1. En **Google Cloud Console**, habilita la **Google Wallet API**.
2. Crea una **Service Account** y descarga su clave en formato JSON.
3. En **Google Pay & Wallet Console**, crea tu **Issuer Account** y vincula el email de la Service Account.
4. Configura en tu `.env` de producción:
   ```env
   GOOGLE_ISSUER_ID=3388000000000000000
   GOOGLE_SA_EMAIL=tu-sa@tu-proyecto.iam.gserviceaccount.com
   GOOGLE_SA_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"
   ```

### Apple Wallet
1. En el **Apple Developer Portal**, genera un **Pass Type ID** (ej. `pass.com.tuempresa.loyalty`).
2. Genera el **Certificado de Pase** (`Pass Type ID Certificate`), exporta la llave y cert en formato PEM.
3. Descarga el certificado **Apple WWDR G4**.
4. Coloca los archivos en la carpeta `certs/` y configura en tu `.env`:
   ```env
   APPLE_PASS_TYPE_ID=pass.com.tuempresa.loyalty
   APPLE_TEAM_ID=TU_TEAM_ID_APPLE
   APPLE_CERT_PATH=certs/passcert.pem
   APPLE_KEY_PATH=certs/passkey.pem
   APPLE_WWDR_PATH=certs/wwdr.pem
   APPLE_WEB_SERVICE_URL=https://api.tudominio.com/api/apple/v1
   ```

---

## 📄 Licencia
Propiedad de **J2M Interactive**. Todos los derechos reservados.
