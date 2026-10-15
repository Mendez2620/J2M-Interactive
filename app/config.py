import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the application
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / ".env")


class Config:
    """Base application configuration."""
    SECRET_KEY = os.getenv("SECRET_KEY", "default-dev-secret-key-j2m")
    
    # Database URI: defaults to SQLite if DATABASE_URL is not set or empty
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL", f"sqlite:///{BASE_DIR / 'j2m_loyalty.db'}"
    )
    # If DATABASE_URL starts with postgres:// (e.g. from Heroku/older providers), fix to postgresql://
    if SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace(
            "postgres://", "postgresql://", 1
        )

    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # CORS
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")
    
    # JWT
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "default-jwt-secret-key")
    JWT_ACCESS_TOKEN_EXPIRES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRES", 900))
    JWT_REFRESH_TOKEN_EXPIRES = int(os.getenv("JWT_REFRESH_TOKEN_EXPIRES", 2592000))
    JWT_ISSUER = os.getenv("JWT_ISSUER", "j2m-interactive")
    JWT_AUDIENCE = os.getenv("JWT_AUDIENCE", "j2m-staff")
    AUTH_RATE_LIMIT_ATTEMPTS = int(os.getenv("AUTH_RATE_LIMIT_ATTEMPTS", 5))
    AUTH_RATE_LIMIT_WINDOW = int(os.getenv("AUTH_RATE_LIMIT_WINDOW", 300))
    MAX_TRANSACTION_AMOUNT = float(os.getenv("MAX_TRANSACTION_AMOUNT", 1000000))
    WALLET_TOKEN_MAX_AGE = int(os.getenv("WALLET_TOKEN_MAX_AGE", 900))
    RECOVERY_CODE_MAX_AGE = int(os.getenv("RECOVERY_CODE_MAX_AGE", 600))
    RECOVERY_SESSION_MAX_AGE = int(os.getenv("RECOVERY_SESSION_MAX_AGE", 900))
    RECOVERY_MAX_ATTEMPTS = int(os.getenv("RECOVERY_MAX_ATTEMPTS", 5))
    RECOVERY_REQUEST_LIMIT_EMAIL = int(os.getenv("RECOVERY_REQUEST_LIMIT_EMAIL", 3))
    RECOVERY_REQUEST_LIMIT_IP = int(os.getenv("RECOVERY_REQUEST_LIMIT_IP", 10))
    RECOVERY_RATE_WINDOW = int(os.getenv("RECOVERY_RATE_WINDOW", 900))
    RECOVERY_CHALLENGE_RETENTION = int(os.getenv("RECOVERY_CHALLENGE_RETENTION", 86400))
    RECOVERY_MAIL_BACKEND = os.getenv("RECOVERY_MAIL_BACKEND", "smtp")
    SMTP_HOST = os.getenv("SMTP_HOST")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USERNAME = os.getenv("SMTP_USERNAME")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
    SMTP_FROM = os.getenv("SMTP_FROM")
    SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    # Two 5 MB images plus multipart form overhead.
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 12 * 1024 * 1024))
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    PERMANENT_SESSION_LIFETIME = 3600
    CSRF_ENABLED = True


class DevelopmentConfig(Config):
    """Development environment configuration."""
    DEBUG = True


class ProductionConfig(Config):
    """Production environment configuration."""
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


class TestingConfig(Config):
    """Testing environment configuration."""
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    CSRF_ENABLED = False
    UPLOAD_FOLDER = os.path.join(os.getenv("TEMP", "."), "j2m-test-uploads")
    JWT_SECRET_KEY = "testing-only-jwt-secret-key-longer-than-32-bytes"
    RECOVERY_MAIL_BACKEND = "memory"


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "default": DevelopmentConfig,
}
