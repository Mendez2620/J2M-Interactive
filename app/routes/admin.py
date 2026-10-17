from datetime import date, datetime
from flask import Blueprint, jsonify, render_template, request, send_file, redirect, current_app
from io import BytesIO
import math
import os
import re
import uuid
import warnings
from PIL import Image, UnidentifiedImageError
from werkzeug.utils import secure_filename
from app.extensions import db
from app.models import Business, BusinessCategory, Customer, PlatformAdmin, Staff, Transaction
from app.security import current_platform_admin, platform_admin_required, valid_email, validate_password
from app.services import ExportService
from app.staff_auth import revoke_staff_sessions
from app.access_audit import access_audit, staff_access_state
from app.permissions import (
    detect_profile, permissions_for_profile, public_catalog, validate_permissions,
)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}
VISUAL_THEMES = {"modern", "elegant", "minimal"}
FONT_STYLES = {"modern", "classic", "geometric"}
TEXT_SIZES = {"compact", "standard", "large"}
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def validate_image(upload):
    """Fully decode an uploaded image with Pillow and return safe in-memory data."""
    if not upload or not upload.filename:
        return None
    safe_name = secure_filename(upload.filename)
    extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if extension == "jpeg":
        extension = "jpg"
    if extension not in set(ALLOWED_IMAGE_FORMATS.values()):
        raise ValueError("Solo se permiten imágenes PNG, JPG y WEBP.")
    content = upload.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Cada imagen debe pesar 5 MB o menos.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                detected_format = image.format
                image.verify()
            with Image.open(BytesIO(content)) as image:
                image.load()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("El contenido del archivo no corresponde a una imagen válida.")
    detected_extension = ALLOWED_IMAGE_FORMATS.get(detected_format)
    if detected_extension != extension:
        raise ValueError("La extensión no coincide con el formato real de la imagen.")
    return content, extension


def persist_images(files):
    """Validate the whole batch before writing, preventing partial orphan files."""
    prepared = {
        field: validate_image(files.get(field))
        for field in ("logo_file", "cover_file")
    }
    upload_dir = current_app.config.get("UPLOAD_FOLDER")
    if not upload_dir:
        upload_dir = os.path.join(current_app.root_path, "static", "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    paths = {}
    created_files = []
    try:
        for field, prepared_image in prepared.items():
            if not prepared_image:
                paths[field] = None
                continue
            content, extension = prepared_image
            filename = f"{uuid.uuid4().hex}.{extension}"
            absolute_path = os.path.join(upload_dir, filename)
            with open(absolute_path, "xb") as image_file:
                image_file.write(content)
            created_files.append(absolute_path)
            paths[field] = f"uploads/{filename}"
    except Exception:
        cleanup_created_files(created_files)
        raise
    return paths, created_files


def cleanup_created_files(paths):
    for path in paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def parse_loyalty(data, current=None):
    loyalty_type = (data.get("loyalty_type") or (current.loyalty_type if current else "stamps")).strip().lower()
    if loyalty_type not in {"stamps", "points"}:
        raise ValueError("El esquema de lealtad debe ser 'stamps' o 'points'.")
    stamps = current.stamps_reward_limit if current else 10
    points = current.points_per_currency if current else 1.0
    if loyalty_type == "stamps":
        raw_stamps = data.get("stamps_reward_limit", stamps)
        try:
            if isinstance(raw_stamps, bool):
                raise ValueError
            if isinstance(raw_stamps, float) and (not math.isfinite(raw_stamps) or not raw_stamps.is_integer()):
                raise ValueError
            if isinstance(raw_stamps, str) and not re.fullmatch(r"[+-]?\d+", raw_stamps.strip()):
                raise ValueError
            stamps = int(raw_stamps)
        except (TypeError, ValueError):
            raise ValueError("El límite de sellos debe ser un entero positivo.")
        if stamps <= 0:
            raise ValueError("El límite de sellos debe ser un entero positivo.")
    else:
        try:
            points = float(data.get("points_per_currency", points))
        except (TypeError, ValueError):
            raise ValueError("Los puntos por moneda deben ser un número positivo.")
        if not math.isfinite(points) or points <= 0:
            raise ValueError("Los puntos por moneda deben ser un número positivo.")
    return loyalty_type, points, stamps


def parse_visual_preferences(data, current=None):
    """Validate the bounded presentation presets and palette."""
    values = {
        "visual_theme": data.get("visual_theme", current.visual_theme if current else "modern"),
        "font_style": data.get("font_style", current.font_style if current else "modern"),
        "text_size": data.get("text_size", current.text_size if current else "standard"),
        "primary_color": data.get("primary_color", current.primary_color if current else "#1E3A8A"),
        "secondary_color": data.get("secondary_color", current.secondary_color if current else "#FFFFFF"),
    }
    values = {key: str(value).strip().lower() for key, value in values.items()}
    if values["visual_theme"] not in VISUAL_THEMES:
        raise ValueError("El tema visual seleccionado no es válido.")
    if values["font_style"] not in FONT_STYLES:
        raise ValueError("El estilo tipográfico seleccionado no es válido.")
    if values["text_size"] not in TEXT_SIZES:
        raise ValueError("El tamaño de texto seleccionado no es válido.")
    for key in ("primary_color", "secondary_color"):
        if not HEX_COLOR_RE.fullmatch(values[key]):
            raise ValueError("Los colores deben usar el formato hexadecimal #RRGGBB.")
        values[key] = values[key].upper()
    return values


def calculate_age_distribution():
    """Calculates customer demographics into standard age buckets based on birthdate."""
    today = date.today()
    customers = Customer.query.all()
    
    buckets = {
        "<18": 0,
        "18-24": 0,
        "25-34": 0,
        "35-49": 0,
        "50+": 0,
        "desconocido": 0,
    }

    for c in customers:
        if not c.birthdate:
            buckets["desconocido"] += 1
            continue
        try:
            age = today.year - c.birthdate.year - (
                (today.month, today.day) < (c.birthdate.month, c.birthdate.day)
            )
            if age < 18:
                buckets["<18"] += 1
            elif 18 <= age <= 24:
                buckets["18-24"] += 1
            elif 25 <= age <= 34:
                buckets["25-34"] += 1
            elif 35 <= age <= 49:
                buckets["35-49"] += 1
            else:
                buckets["50+"] += 1
        except Exception:
            buckets["desconocido"] += 1

    return buckets


@admin_bp.route("", methods=["GET"])
@admin_bp.route("/dashboard", methods=["GET"])
@platform_admin_required
def dashboard():
    """Super Admin Clean Dashboard with business search, category filter, and pagination."""
    page = request.args.get("page", 1, type=int)
    per_page = 10
    search = (request.args.get("q") or "").strip()
    category_id = request.args.get("category_id", type=int)

    query = Business.query
    if search:
        query = query.filter(Business.name.ilike(f"%{search}%"))
    if category_id:
        query = query.filter(Business.category_id == category_id)

    query = query.order_by(Business.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    businesses = pagination.items

    total_businesses = Business.query.count()
    total_customers = Customer.query.count()
    total_transactions = Transaction.query.count()
    categories = BusinessCategory.query.order_by(BusinessCategory.name).all()

    # Preserved JSON response for testing / API clients
    if request.is_json or request.headers.get("Accept") == "application/json":
        all_businesses = Business.query.order_by(Business.created_at.desc()).all()
        total_volume_query = db.session.query(db.func.sum(Transaction.purchase_amount)).scalar()
        total_purchase_volume = float(total_volume_query or 0.0)
        age_stats = calculate_age_distribution()
        staff_list = Staff.query.order_by(Staff.created_at.desc()).all()
        return jsonify({
            "status": "success",
            "metrics": {
                "total_businesses": total_businesses,
                "total_customers": total_customers,
                "total_transactions": total_transactions,
                "total_purchase_volume": total_purchase_volume,
                "age_distribution": age_stats,
            },
            "businesses": [b.to_dict() for b in all_businesses],
            "staff": [s.to_dict() for s in staff_list],
            "categories": [c.to_dict() for c in categories],
        }), 200

    return render_template(
        "admin/dashboard.html",
        businesses=businesses,
        pagination=pagination,
        categories=categories,
        total_businesses=total_businesses,
        total_customers=total_customers,
        total_transactions=total_transactions,
        current_search=search,
        current_category=category_id,
        platform_admins=PlatformAdmin.query.order_by(PlatformAdmin.name).all(),
    )


@admin_bp.route("/business/create", methods=["POST"])
@platform_admin_required
def create_business():
    """CRUD Endpoint to create a new Business / Tenant, handling file uploads for logo and cover images."""
    # Determine request type
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        data = request.form.to_dict()
        files = request.files
    else:
        data = request.get_json(silent=True) or {}
        files = None

    name = (data.get("name") or "").strip()
    slug = (data.get("slug") or "").strip().lower()

    if not name or not slug:
        return jsonify({"status": "error", "message": "Nombre y slug son obligatorios."}), 400

    # Ensure slug uniqueness
    existing = Business.query.filter_by(slug=slug).first()
    if existing:
        return jsonify({"status": "error", "message": f"El slug '{slug}' ya está en uso."}), 409

    # Parse optional fields
    category_id = data.get("category_id")
    if category_id:
        try:
            category_id = int(category_id)
        except (ValueError, TypeError):
            category_id = None

    try:
        loyalty_type, points_per_currency, stamps_reward_limit = parse_loyalty(data)
        visual = parse_visual_preferences(data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    logo_url = data.get("logo_url") or None
    latitude = float(data.get("latitude")) if data.get("latitude") else None
    longitude = float(data.get("longitude")) if data.get("longitude") else None
    cooldown_minutes = int(data.get("cooldown_minutes") or 60)
    reward_description = (data.get("reward_description") or "Recompensa de lealtad").strip()
    wallet_terms = (data.get("wallet_terms") or "").strip() or None
    is_active = str(data.get("is_active", "true")).lower() in ("true", "1", "t")

    created_files = []
    try:
        image_paths, created_files = persist_images(files) if files else ({}, [])
        logo_path = image_paths.get("logo_file")
        cover_path = image_paths.get("cover_file")
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    business = Business(
        name=name,
        slug=slug,
        category_id=category_id,
        logo_url=logo_url,
        logo_path=logo_path,
        cover_path=cover_path,
        primary_color=visual["primary_color"],
        secondary_color=visual["secondary_color"],
        visual_theme=visual["visual_theme"],
        font_style=visual["font_style"],
        text_size=visual["text_size"],
        loyalty_type=loyalty_type,
        points_per_currency=points_per_currency,
        stamps_reward_limit=stamps_reward_limit,
        latitude=latitude,
        longitude=longitude,
        cooldown_minutes=cooldown_minutes,
        reward_description=reward_description,
        wallet_terms=wallet_terms,
        is_active=is_active,
    )
    try:
        db.session.add(business)
        db.session.commit()
    except Exception:
        db.session.rollback()
        cleanup_created_files(created_files)
        raise

    return jsonify({
        "status": "success",
        "message": f"Negocio '{business.name}' creado exitosamente.",
        "business": business.to_dict(),
    }), 201


@admin_bp.route("/business/<int:business_id>", methods=["GET"])
@platform_admin_required
def get_business_details(business_id: int):
    """Detailed modular view with tabs for a specific business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    customers = business.customers.order_by(Customer.created_at.desc()).all()
    transactions = business.transactions.order_by(Transaction.timestamp.desc()).all()
    staff_members = business.staff_members.order_by(Staff.created_at.desc()).all()
    total_volume = sum(t.purchase_amount for t in transactions)
    categories = BusinessCategory.query.order_by(BusinessCategory.name).all()

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "business": business.to_dict(),
            "stats": {
                "total_customers": len(customers),
                "total_staff": len(staff_members),
                "total_transactions": len(transactions),
                "total_purchase_volume": float(total_volume),
            },
            "customers": [c.to_dict() for c in customers],
            "transactions": [t.to_dict() for t in transactions],
            "staff": [s.to_dict() for s in staff_members],
        }), 200

    return render_template(
        "admin/business_detail.html",
        business=business,
        categories=categories,
        customers=customers,
        transactions=transactions,
        staff_members=staff_members,
        total_volume=total_volume,
    )


@admin_bp.route("/business/<int:business_id>/update", methods=["POST"])
@platform_admin_required
def update_business(business_id: int):
    """Updates general, wallet, or loyalty rules for a business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    data = request.get_json(silent=True) or request.form.to_dict()

    try:
        visual = parse_visual_preferences(data, business)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    if "name" in data and data["name"].strip():
        business.name = data["name"].strip()
    if "slug" in data and data["slug"].strip():
        new_slug = data["slug"].strip().lower()
        existing = Business.query.filter(Business.slug == new_slug, Business.id != business.id).first()
        if existing:
            return jsonify({"status": "error", "message": f"El slug '{new_slug}' ya está en uso."}), 409
        business.slug = new_slug
    if "category_id" in data:
        try:
            business.category_id = int(data["category_id"]) if data["category_id"] else None
        except (ValueError, TypeError):
            business.category_id = None
    if "logo_url" in data:
        business.logo_url = data["logo_url"].strip() or None
    business.primary_color = visual["primary_color"]
    business.secondary_color = visual["secondary_color"]
    business.visual_theme = visual["visual_theme"]
    business.font_style = visual["font_style"]
    business.text_size = visual["text_size"]
    if "latitude" in data:
        try:
            business.latitude = float(data["latitude"]) if str(data["latitude"]).strip() != "" else None
        except (ValueError, TypeError):
            business.latitude = None
    if "longitude" in data:
        try:
            business.longitude = float(data["longitude"]) if str(data["longitude"]).strip() != "" else None
        except (ValueError, TypeError):
            business.longitude = None
    if "wallet_terms" in data:
        business.wallet_terms = data["wallet_terms"].strip() or None
    if any(key in data for key in ("loyalty_type", "points_per_currency", "stamps_reward_limit")):
        try:
            loyalty_type, points, stamps = parse_loyalty(data, business)
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(exc)}), 400
        business.loyalty_type = loyalty_type
        business.points_per_currency = points
        business.stamps_reward_limit = stamps
    if "reward_description" in data:
        business.reward_description = data["reward_description"].strip() or None
    if "cooldown_minutes" in data:
        try:
            business.cooldown_minutes = max(0, int(data["cooldown_minutes"]))
        except (ValueError, TypeError):
            pass
    if "is_active" in data:
        business.is_active = True if str(data["is_active"]).lower() in ("true", "1", "t", "on") else False
    # File upload handling for logo and cover (if multipart/form-data)
    if request.content_type and request.content_type.startswith('multipart/form-data'):
        created_files = []
        try:
            image_paths, created_files = persist_images(request.files)
            logo_path = image_paths.get("logo_file")
            cover_path = image_paths.get("cover_file")
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"status": "error", "message": str(exc)}), 400
        if logo_path:
            business.logo_path = logo_path
        if cover_path:
            business.cover_path = cover_path

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        cleanup_created_files(locals().get("created_files", []))
        raise

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "message": "Negocio actualizado correctamente.",
            "business": business.to_dict(),
        }), 200

    return redirect(f"/admin/business/{business.id}")


@admin_bp.route("/business/<int:business_id>/toggle-status", methods=["POST"])
@platform_admin_required
def toggle_business_status(business_id: int):
    """Toggles business is_active status (activate/suspend)."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    business.is_active = not business.is_active
    if not business.is_active:
        for member in business.staff_members:
            revoke_staff_sessions(member)
    db.session.commit()

    status_str = "activado" if business.is_active else "suspendido"
    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "message": f"Negocio '{business.name}' {status_str} exitosamente.",
            "is_active": business.is_active,
        }), 200

    next_url = request.args.get("next") or request.referrer or f"/admin/business/{business.id}"
    return redirect(next_url)


@admin_bp.route("/business/<int:business_id>/export/excel", methods=["GET"])
@platform_admin_required
def export_business_excel(business_id: int):
    """Exports business customer list and transaction audit log to Excel workbook (.xlsx)."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    excel_stream = ExportService.generate_excel_report(business)
    filename = f"{business.slug}_reporte_clientes.xlsx"

    return send_file(
        excel_stream,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@admin_bp.route("/business/<int:business_id>/export/pdf", methods=["GET"])
@platform_admin_required
def export_business_pdf(business_id: int):
    """Exports business CRM metrics and recent tables to a PDF report."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    pdf_stream = ExportService.generate_pdf_report(business)
    filename = f"{business.slug}_reporte_gerencial.pdf"

    return send_file(
        pdf_stream,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@admin_bp.route("/business/<int:business_id>/staff", methods=["POST"])
@platform_admin_required
def create_staff(business_id: int):
    """Creates a new Staff member (Manager, Cashier, Admin) for a given business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    data = request.get_json(silent=True) or request.form.to_dict()
    username = (data.get("username") or "").strip()
    password = data.get("password")
    role = (data.get("role") or "cashier").lower().strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None

    if not username or not password:
        return jsonify({"status": "error", "message": "Usuario y contraseña son requeridos."}), 400

    if role not in ("manager", "cashier"):
        return jsonify({"status": "error", "message": "Rol inválido. Debe ser 'manager' o 'cashier'."}), 400

    password_error = validate_password(password)
    if password_error:
        return jsonify({"status": "error", "message": password_error}), 400

    existing_staff = Staff.query.filter_by(business_id=business_id, username=username).first()
    if existing_staff:
        return jsonify({"status": "error", "message": f"El usuario '{username}' ya existe para este negocio."}), 409

    staff_member = Staff(
        business_id=business.id,
        name=name,
        username=username,
        email=email,
        phone=phone,
        role=role,
        is_active=True,
        permissions="view_stats" if role == "manager" else "scanner_operate",
        web_access_enabled=False,
        mobile_access_enabled=False,
    )
    staff_member.set_password(password)

    db.session.add(staff_member)
    db.session.flush()
    created_state = staff_access_state(staff_member) | {
        "username": staff_member.username, "role": staff_member.role,
    }
    db.session.add(access_audit(
        "staff_created", staff_member, new=created_state,
        platform_admin_id=current_platform_admin().id,
    ))
    db.session.commit()

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "message": f"Usuario de staff '{username}' ({role}) creado exitosamente.",
            "staff": staff_member.to_dict(),
        }), 201

    return redirect(f"/admin/business/{business.id}?tab=staff")


@admin_bp.route("/staff/<int:staff_id>/toggle-status", methods=["POST"])
@platform_admin_required
def toggle_staff_status(staff_id: int):
    """Activates or deactivates a staff member."""
    staff = db.session.get(Staff, staff_id)
    if not staff:
        return jsonify({"status": "error", "message": "Usuario no encontrado"}), 404

    previous = staff_access_state(staff)
    staff.is_active = not staff.is_active
    revoke_staff_sessions(staff)
    db.session.add(access_audit(
        "staff_status_updated", staff, previous, staff_access_state(staff),
        platform_admin_id=current_platform_admin().id,
    ))
    db.session.commit()

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "message": "Estado de usuario actualizado.",
            "is_active": staff.is_active,
        }), 200

    next_url = request.args.get("next") or request.referrer or f"/admin/business/{staff.business_id}?tab=staff"
    return redirect(next_url)


@admin_bp.route("/staff/<int:staff_id>/delete", methods=["POST"])
@platform_admin_required
def delete_staff(staff_id: int):
    """Deletes a staff member."""
    staff = db.session.get(Staff, staff_id)
    if not staff:
        return jsonify({"status": "error", "message": "Usuario no encontrado"}), 404
    if staff.transactions.count():
        return jsonify({
            "status": "error",
            "message": "Este usuario conserva movimientos históricos. Desactívalo para preservar la trazabilidad.",
        }), 409

    business_id = staff.business_id
    previous = staff_access_state(staff) | {"username": staff.username, "role": staff.role}
    db.session.add(access_audit(
        "staff_deleted", staff, previous=previous,
        platform_admin_id=current_platform_admin().id,
    ))
    db.session.delete(staff)
    db.session.commit()

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({"status": "success", "message": "Usuario eliminado."}), 200

    next_url = request.args.get("next") or request.referrer or f"/admin/business/{business_id}?tab=staff"
    return redirect(next_url)


@admin_bp.route("/staff/<int:staff_id>/reset-password", methods=["POST"])
@platform_admin_required
def reset_staff_password(staff_id: int):
    """Replace a tenant staff password without changing its identity or history."""
    staff = db.session.get(Staff, staff_id)
    if not staff:
        return jsonify({"status": "error", "message": "Usuario no encontrado."}), 404
    data = request.get_json(silent=True) or request.form.to_dict()
    password = data.get("password") or ""
    confirmation = data.get("password_confirmation") or ""
    if password != confirmation:
        return jsonify({"status": "error", "message": "Las contraseñas no coinciden."}), 400
    password_error = validate_password(password)
    if password_error:
        return jsonify({"status": "error", "message": password_error}), 400
    staff.set_password(password)
    revoke_staff_sessions(staff)
    db.session.add(access_audit(
        "password_reset", staff, platform_admin_id=current_platform_admin().id,
        new={"password_reset": True},
    ))
    db.session.commit()
    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({"status": "success", "message": "Contraseña actualizada correctamente.", "staff": staff.to_dict()}), 200
    return redirect(f"/admin/business/{staff.business_id}?tab=staff")


@admin_bp.route("/business/<int:business_id>/access/<channel>", methods=["POST"])
@platform_admin_required
def toggle_business_access(business_id: int, channel: str):
    if channel not in {"web", "mobile"}:
        return jsonify({"status": "error", "message": "Canal inválido."}), 400
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado."}), 404
    previous = {
        "web_access_enabled": business.web_access_enabled,
        "mobile_access_enabled": business.mobile_access_enabled,
    }
    field = f"{channel}_access_enabled"
    setattr(business, field, not getattr(business, field))
    if not getattr(business, field):
        for member in business.staff_members:
            revoke_staff_sessions(member)
    new_state = {
        "web_access_enabled": business.web_access_enabled,
        "mobile_access_enabled": business.mobile_access_enabled,
    }
    db.session.add(access_audit(
        "business_channel_updated", previous=previous, new=new_state,
        platform_admin_id=current_platform_admin().id, business_id=business.id,
    ))
    db.session.commit()
    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({"status": "success", "channel": channel, "enabled": getattr(business, field)}), 200
    return redirect(f"/admin/business/{business.id}?tab=staff")


@admin_bp.route("/staff/<int:staff_id>/access/<channel>", methods=["POST"])
@platform_admin_required
def toggle_staff_access(staff_id: int, channel: str):
    if channel not in {"web", "mobile"}:
        return jsonify({"status": "error", "message": "Canal inválido."}), 400
    staff = db.session.get(Staff, staff_id)
    if not staff:
        return jsonify({"status": "error", "message": "Usuario no encontrado."}), 404
    previous = staff_access_state(staff)
    field = f"{channel}_access_enabled"
    setattr(staff, field, not getattr(staff, field))
    revoke_staff_sessions(staff)
    db.session.add(access_audit(
        "staff_channel_updated", staff, previous, staff_access_state(staff),
        platform_admin_id=current_platform_admin().id,
    ))
    db.session.commit()
    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({"status": "success", "channel": channel, "enabled": getattr(staff, field)}), 200
    return redirect(f"/admin/business/{staff.business_id}?tab=staff")


@admin_bp.route("/business/<int:business_id>/staff/<int:staff_id>/permissions", methods=["GET"])
@platform_admin_required
def get_staff_permissions(business_id: int, staff_id: int):
    staff = db.session.get(Staff, staff_id)
    if not staff or staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Empleado no encontrado en este negocio."}), 404
    return jsonify({
        "status": "success",
        "staff": staff.to_dict(),
        "business": {"id": staff.business.id, "name": staff.business.name,
                     "web_access_enabled": staff.business.web_access_enabled,
                     "mobile_access_enabled": staff.business.mobile_access_enabled},
        "profile": detect_profile(staff),
        "catalog": public_catalog(staff.role),
    }), 200


@admin_bp.route("/business/<int:business_id>/staff/<int:staff_id>/permissions", methods=["PUT"])
@platform_admin_required
def update_staff_permissions(business_id: int, staff_id: int):
    staff = db.session.get(Staff, staff_id)
    if not staff or staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Empleado no encontrado en este negocio."}), 404
    if staff.role not in {"manager", "cashier"}:
        return jsonify({"status": "error", "message": "El rol no admite permisos configurables."}), 400
    data = request.get_json(silent=True) or {}
    if "role" in data or "business_id" in data or "id" in data:
        return jsonify({"status": "error", "message": "No se permite modificar identidad, rol o negocio."}), 400
    if not isinstance(data.get("web_access_enabled"), bool) or not isinstance(data.get("mobile_access_enabled"), bool):
        return jsonify({"status": "error", "message": "Los estados de canal deben ser booleanos."}), 400
    try:
        validate_permissions(staff.role, data.get("permissions", []))
        permissions = permissions_for_profile(
            staff.role, data.get("profile", "custom"), data.get("permissions", [])
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400

    previous = staff_access_state(staff)
    new_state = {
        "is_active": staff.is_active,
        "web_access_enabled": data["web_access_enabled"],
        "mobile_access_enabled": data["mobile_access_enabled"],
        "permissions": sorted(permissions),
    }
    if previous != new_state:
        staff.web_access_enabled = new_state["web_access_enabled"]
        staff.mobile_access_enabled = new_state["mobile_access_enabled"]
        staff.permissions = ",".join(new_state["permissions"])
        revoke_staff_sessions(staff)
        db.session.add(access_audit(
            "permissions_updated", staff, previous, new_state,
            platform_admin_id=current_platform_admin().id,
        ))
        db.session.commit()
    return jsonify({
        "status": "success", "message": "Permisos actualizados.",
        "staff": staff.to_dict(), "profile": detect_profile(staff),
    }), 200


@admin_bp.route("/staff", methods=["GET"])
@platform_admin_required
def list_all_staff():
    """Lists all staff members across businesses."""
    staff_members = Staff.query.order_by(Staff.created_at.desc()).all()
    return jsonify({
        "status": "success",
        "staff": [s.to_dict() for s in staff_members],
    }), 200


@admin_bp.route("/users/create", methods=["POST"])
@platform_admin_required
def create_admin_user():
    """Creates a new admin staff user (global, not tied to a business)."""
    data = request.get_json(silent=True) or request.form.to_dict()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    phone = (data.get("phone") or "").strip()
    password = data.get("password")

    confirmation = data.get("password_confirmation")
    if not name or not valid_email(email) or not password:
        return jsonify({"status": "error", "message": "Nombre, correo válido y contraseña son obligatorios."}), 400
    if confirmation is not None and password != confirmation:
        return jsonify({"status": "error", "message": "Las contraseñas no coinciden."}), 400
    password_error = validate_password(password)
    if password_error:
        return jsonify({"status": "error", "message": password_error}), 400

    # Ensure email uniqueness
    if PlatformAdmin.query.filter_by(email=email).first():
        return jsonify({"status": "error", "message": f"El correo '{email}' ya está en uso."}), 409

    admin_user = PlatformAdmin(
        name=name,
        email=email,
        phone=phone or None,
        is_active=True,
    )
    admin_user.set_password(password)
    db.session.add(admin_user)
    db.session.commit()

    return jsonify({
        "status": "success",
        "message": f"Usuario administrador '{name}' creado exitosamente.",
        "admin": admin_user.to_dict(),
    }), 201


@admin_bp.route("/users/edit/<int:staff_id>", methods=["POST"])
@platform_admin_required
def edit_admin_user(staff_id: int):
    """Edits an existing admin staff user. Only admins can be edited via this endpoint."""
    admin_user = db.session.get(PlatformAdmin, staff_id)
    if not admin_user:
        return jsonify({"status": "error", "message": "Usuario no encontrado."}), 404
    data = request.get_json(silent=True) or request.form.to_dict()
    name = (data.get("name") or admin_user.name).strip()
    email = (data.get("email") or admin_user.email).strip().lower()
    phone = (data.get("phone") if "phone" in data else (admin_user.phone or "")).strip()
    password = data.get("password")

    # If email is changed, ensure uniqueness
    if not name or not valid_email(email):
        return jsonify({"status": "error", "message": "Nombre y correo válido son obligatorios."}), 400
    if email != admin_user.email and PlatformAdmin.query.filter_by(email=email).first():
        return jsonify({"status": "error", "message": f"El correo '{email}' ya está en uso."}), 409

    admin_user.name = name
    admin_user.email = email
    admin_user.phone = phone or None
    if password:
        password_error = validate_password(password)
        if password_error:
            return jsonify({"status": "error", "message": password_error}), 400
        admin_user.set_password(password)

    db.session.commit()

    return jsonify({
        "status": "success",
        "message": f"Usuario administrador actualizado.",
        "admin": admin_user.to_dict(),
    }), 200


@admin_bp.route("/categories", methods=["GET", "POST"])
@platform_admin_required
def manage_categories():
    """Lists or creates business categories."""
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        name = (data.get("name") or "").strip()
        slug = (data.get("slug") or "").strip().lower() or name.lower().replace(" ", "-")

        if not name:
            return jsonify({"status": "error", "message": "El nombre de la categoría es requerido."}), 400

        category = BusinessCategory(name=name, slug=slug)
        db.session.add(category)
        db.session.commit()
        return jsonify({"status": "success", "category": category.to_dict()}), 201

    categories = BusinessCategory.query.order_by(BusinessCategory.name).all()
    return jsonify({
        "status": "success",
        "categories": [c.to_dict() for c in categories],
    }), 200
