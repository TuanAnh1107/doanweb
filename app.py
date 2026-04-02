from flask import Flask
from config import Config
from routes.assignments import assignments_bp
from routes.attendance import attendance_bp
from routes.auth import auth_bp
from routes.classes import classes_bp

app = Flask(__name__)
app.config.from_object(Config)

app.register_blueprint(auth_bp)
app.register_blueprint(classes_bp)
app.register_blueprint(attendance_bp)
app.register_blueprint(assignments_bp)

if __name__ == "__main__":
    app.run(debug=app.config.get("DEBUG", False))
