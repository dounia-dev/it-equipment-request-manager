from flask import Flask, render_template, request, redirect, url_for, flash
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user
)
from models import db, User, Ticket, TicketHistory, Comment
from flask_wtf.csrf import CSRFProtect
app = Flask(__name__)
app.config["SECRET_KEY"] = "change-me-later"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///app.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
csrf = CSRFProtect(app)
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.init_app(app)

STATUSES = ["OFFEN", "VALIDIERT", "GELIEFERT", "ABGELEHNT"]
PRIORITIES = ["NIEDRIG", "MITTEL", "HOCH"]

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def seed_users():
    if User.query.count() > 0:
        return
    test_users = [
        ("U001", "Test User", "user123", "USER"),
        ("A001", "Test Admin", "admin123", "ADMIN"),
        ("S001", "Test Supplier", "supplier123", "SUPPLIER"),
    ]
    for matricule, name, password, role in test_users:
        u = User(matricule=matricule, name=name, role=role)
        u.set_password(password)
        db.session.add(u)
    db.session.commit()


def visible_query():
    """Welche Tickets darf der aktuelle Benutzer sehen?"""
    q = Ticket.query
    if current_user.role == "USER":
        q = q.filter_by(user_id=current_user.id)
    elif current_user.role == "SUPPLIER":
        q = q.filter(Ticket.status.in_(["VALIDIERT", "GELIEFERT"]))
    return q


def log_history(ticket, old_status, new_status):
    h = TicketHistory(
        ticket_id=ticket.id,
        changed_by_id=current_user.id,
        old_status=old_status,
        new_status=new_status,
    )
    db.session.add(h)


def change_status(ticket_id, allowed_role, from_status, to_status):
    """Aendert den Status nur, wenn Rolle und aktueller Status stimmen."""
    if current_user.role != allowed_role:
        flash("Keine Berechtigung.")
        return
    t = db.session.get(Ticket, ticket_id)
    if t is None:
        flash("Ticket nicht gefunden.")
    elif t.status != from_status:
        flash("Ungültiger Statuswechsel.")
    else:
        t.status = to_status
        log_history(t, from_status, to_status)
        db.session.commit()
        flash(f"Ticket #{t.id}: {to_status}")


@app.route("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        matricule = request.form["matricule"].strip()
        password = request.form["password"]
        user = User.query.filter_by(matricule=matricule).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for("dashboard"))
        flash("Matricule oder Passwort falsch.")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    base = visible_query()
    counts = {s: base.filter_by(status=s).count() for s in STATUSES}

    current_filter = request.args.get("status", "")
    current_priority = request.args.get("priority", "")
    search = request.args.get("q", "").strip()

    q = base
    if current_filter in STATUSES:
        q = q.filter_by(status=current_filter)
    if current_priority in PRIORITIES:
        q = q.filter_by(priority=current_priority)
    if search:
        like = f"%{search}%"
        q = q.filter(
            db.or_(
                Ticket.equipment_type.ilike(like),
                Ticket.description.ilike(like),
            )
        )

    prio_order = db.case(
        {"HOCH": 0, "MITTEL": 1, "NIEDRIG": 2},
        value=Ticket.priority,
    )
    tickets = q.order_by(prio_order, Ticket.id.desc()).all()

    return render_template(
        "dashboard.html",
        tickets=tickets,
        counts=counts,
        current_filter=current_filter,
        current_priority=current_priority,
        search=search,
        statuses=STATUSES,
        priorities=PRIORITIES,
    )


@app.route("/tickets/new", methods=["GET", "POST"])
@login_required
def new_ticket():
    if current_user.role != "USER":
        flash("Nur normale Benutzer können Tickets erstellen.")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        t = Ticket(
            user_id=current_user.id,
            kind=request.form["kind"],
            equipment_type=request.form["equipment_type"],
            priority=request.form.get("priority", "MITTEL"),
            description=request.form["description"].strip(),
        )
        db.session.add(t)
        db.session.flush()          # damit t.id existiert
        log_history(t, None, "OFFEN")
        db.session.commit()
        flash("Ticket wurde erstellt.")
        return redirect(url_for("dashboard"))

    return render_template("new_ticket.html")


@app.route("/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    t = visible_query().filter_by(id=ticket_id).first()
    if t is None:
        flash("Ticket nicht gefunden oder keine Berechtigung.")
        return redirect(url_for("dashboard"))
    return render_template("ticket_detail.html", t=t)


@app.route("/tickets/<int:ticket_id>/validate", methods=["POST"])
@login_required
def validate_ticket(ticket_id):
    change_status(ticket_id, "ADMIN", "OFFEN", "VALIDIERT")
    return redirect(url_for("dashboard"))


@app.route("/tickets/<int:ticket_id>/reject", methods=["POST"])
@login_required
def reject_ticket(ticket_id):
    change_status(ticket_id, "ADMIN", "OFFEN", "ABGELEHNT")
    return redirect(url_for("dashboard"))


@app.route("/tickets/<int:ticket_id>/deliver", methods=["POST"])
@login_required
def deliver_ticket(ticket_id):
    change_status(ticket_id, "SUPPLIER", "VALIDIERT", "GELIEFERT")
    return redirect(url_for("dashboard"))

@app.route("/tickets/<int:ticket_id>/comment", methods=["POST"])
@login_required
def add_comment(ticket_id):
    t = visible_query().filter_by(id=ticket_id).first()
    if t is None:
        flash("Ticket nicht gefunden oder keine Berechtigung.")
        return redirect(url_for("dashboard"))

    text = request.form.get("text", "").strip()
    if text:
        db.session.add(Comment(
            ticket_id=t.id,
            author_id=current_user.id,
            text=text,
        ))
        db.session.commit()
    return redirect(url_for("ticket_detail", ticket_id=t.id))

with app.app_context():
    db.create_all()
    seed_users()


if __name__ == "__main__":
    app.run(debug=True)