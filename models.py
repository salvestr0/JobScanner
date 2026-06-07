import uuid
from datetime import datetime, timezone

import bcrypt
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _uuid():
    return str(uuid.uuid4())


def _now():
    return datetime.now(timezone.utc)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id             = db.Column(db.String(36), primary_key=True, default=_uuid)
    email          = db.Column(db.String(255), unique=True, nullable=False)
    password_hash  = db.Column(db.String(255), nullable=True)
    google_id      = db.Column(db.String(255), unique=True, nullable=True)
    created_at     = db.Column(db.DateTime(timezone=True), default=_now)

    # Per-user API credentials (optional — fall back to env vars if blank)
    gemini_api_key = db.Column(db.String(255))

    # Email verification
    email_verified       = db.Column(db.Boolean, default=False)
    email_verify_token   = db.Column(db.String(64), nullable=True)

    # Password reset
    reset_token         = db.Column(db.String(64), nullable=True)
    reset_token_expires = db.Column(db.DateTime(timezone=True), nullable=True)

    # Billing
    stripe_customer_id  = db.Column(db.String(255))
    subscription_status = db.Column(db.String(32), default="trialing")  # trialing | active | past_due | cancelled
    trial_ends_at       = db.Column(db.DateTime(timezone=True))

    # Relationships
    profile   = db.relationship("UserProfile",  back_populates="user", uselist=False, cascade="all, delete-orphan")
    settings  = db.relationship("UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    jobs         = db.relationship("Job",               back_populates="user", cascade="all, delete-orphan")
    statuses     = db.relationship("ApplicationStatus", back_populates="user", cascade="all, delete-orphan")
    seen         = db.relationship("SeenJob",            back_populates="user", cascade="all, delete-orphan")
    modes        = db.relationship("SearchMode",         back_populates="user", cascade="all, delete-orphan")
    scan_history = db.relationship("ScanHistory",        back_populates="user", cascade="all, delete-orphan")

    def set_password(self, plain: str):
        self.password_hash = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

    def check_password(self, plain: str) -> bool:
        if not self.password_hash:
            return False
        return bcrypt.checkpw(plain.encode(), self.password_hash.encode())


class UserProfile(db.Model):
    __tablename__ = "user_profiles"

    user_id            = db.Column(db.String(36), db.ForeignKey("users.id"), primary_key=True)
    name               = db.Column(db.String(255))
    email              = db.Column(db.String(255))
    phone              = db.Column(db.String(64))
    education          = db.Column(db.Text)
    experience_summary = db.Column(db.Text)
    technical_skills   = db.Column(db.JSON, default=list)
    soft_skills        = db.Column(db.JSON, default=list)
    work_history       = db.Column(db.JSON, default=list)
    certifications     = db.Column(db.JSON, default=list)
    projects           = db.Column(db.JSON, default=list)

    user = db.relationship("User", back_populates="profile")

    def to_dict(self) -> dict:
        return {
            "name":               self.name or "",
            "email":              self.email or "",
            "phone":              self.phone or "",
            "education":          self.education or "",
            "experience_summary": self.experience_summary or "",
            "technical_skills":   self.technical_skills or [],
            "soft_skills":        self.soft_skills or [],
            "work_history":       self.work_history or [],
            "certifications":     self.certifications or [],
            "projects":           self.projects or [],
        }


class UserSettings(db.Model):
    __tablename__ = "user_settings"

    user_id                   = db.Column(db.String(36), db.ForeignKey("users.id"), primary_key=True)
    min_salary                = db.Column(db.Integer, default=2200)
    max_salary                = db.Column(db.Integer, default=4000)
    min_score_threshold       = db.Column(db.Integer, default=30)
    max_jobs_per_notification = db.Column(db.Integer, default=20)
    email_enabled             = db.Column(db.Boolean, default=False)
    email_to                  = db.Column(db.String(255), default="")
    preferred_location        = db.Column(db.String(255), default="Sengkang")
    target_titles             = db.Column(db.JSON, default=list)
    preferred_keywords        = db.Column(db.JSON, default=list)
    negative_keywords         = db.Column(db.JSON, default=list)
    location_keywords         = db.Column(db.JSON, default=list)
    schedule_enabled          = db.Column(db.Boolean, default=False)
    schedule_time             = db.Column(db.String(8), default="09:00")
    daily_scan_count          = db.Column(db.Integer, default=0)
    last_scan_date            = db.Column(db.String(10), default="")

    user = db.relationship("User", back_populates="settings")

    def to_dict(self) -> dict:
        return {
            "min_salary":                self.min_salary,
            "max_salary":                self.max_salary,
            "min_score_threshold":       self.min_score_threshold,
            "max_jobs_per_notification": self.max_jobs_per_notification,
            "email_enabled":             self.email_enabled,
            "email_to":                  self.email_to or "",
            "preferred_location":        self.preferred_location or "Sengkang",
            "target_titles":             self.target_titles or [],
            "preferred_keywords":        self.preferred_keywords or [],
            "negative_keywords":         self.negative_keywords or [],
            "location_keywords":         self.location_keywords or [],
            "schedule_enabled":          self.schedule_enabled,
            "schedule_time":             self.schedule_time or "09:00",
        }


class Job(db.Model):
    __tablename__ = "jobs"
    __table_args__ = (
        db.UniqueConstraint("user_id", "source_job_id", name="uq_user_job"),
    )

    id            = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id       = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    source_job_id = db.Column(db.String(255), nullable=False)
    title         = db.Column(db.String(255))
    company       = db.Column(db.String(255))
    location      = db.Column(db.String(255))
    source        = db.Column(db.String(64))
    url           = db.Column(db.Text)
    posted_date   = db.Column(db.String(64))
    salary_min    = db.Column(db.Integer)
    salary_max    = db.Column(db.Integer)
    score         = db.Column(db.Integer, default=0)
    match_reasons = db.Column(db.Text)
    closing_date  = db.Column(db.String(32))
    hidden        = db.Column(db.Boolean, default=False)
    scan_date     = db.Column(db.DateTime(timezone=True), default=_now)

    user = db.relationship("User", back_populates="jobs")

    def to_dict(self) -> dict:
        return {
            "id":            self.source_job_id,
            "title":         self.title or "",
            "company":       self.company or "",
            "location":      self.location or "",
            "source":        self.source or "",
            "url":           self.url or "",
            "posted_date":   self.posted_date or "",
            "salary_min":    self.salary_min or "",
            "salary_max":    self.salary_max or "",
            "score":         self.score or 0,
            "match_reasons": self.match_reasons or "",
            "closing_date":  self.closing_date or "",
            "hidden":        bool(self.hidden),
            "scan_date":     self.scan_date.strftime("%Y-%m-%d %H:%M") if self.scan_date else "",
        }


class ApplicationStatus(db.Model):
    __tablename__ = "application_statuses"

    user_id       = db.Column(db.String(36), db.ForeignKey("users.id"), primary_key=True)
    job_source_id = db.Column(db.String(255), primary_key=True)
    status        = db.Column(db.String(32))
    title         = db.Column(db.String(255))
    company       = db.Column(db.String(255))
    url           = db.Column(db.Text)
    notes         = db.Column(db.Text)
    interview_date = db.Column(db.String(32))
    interview_time = db.Column(db.String(32))
    updated_at    = db.Column(db.DateTime(timezone=True), default=_now, onupdate=_now)

    user = db.relationship("User", back_populates="statuses")

    def to_dict(self) -> dict:
        return {
            "status":         self.status or "",
            "title":          self.title or "",
            "company":        self.company or "",
            "url":            self.url or "",
            "notes":          self.notes or "",
            "interview_date": self.interview_date or "",
            "interview_time": self.interview_time or "",
            "updated_at":     self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else "",
        }


class SeenJob(db.Model):
    __tablename__ = "seen_jobs"

    user_id       = db.Column(db.String(36), db.ForeignKey("users.id"), primary_key=True)
    job_source_id = db.Column(db.String(255), primary_key=True)
    first_seen_at = db.Column(db.DateTime(timezone=True), default=_now)

    user = db.relationship("User", back_populates="seen")


class ScanHistory(db.Model):
    __tablename__ = "scan_history"

    id          = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id     = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    mode        = db.Column(db.String(64))
    started_at  = db.Column(db.DateTime(timezone=True), default=_now)
    finished_at = db.Column(db.DateTime(timezone=True), nullable=True)
    job_count   = db.Column(db.Integer, default=0)
    status      = db.Column(db.String(16), default="running")  # running | done | failed

    user = db.relationship("User", back_populates="scan_history")

    def to_dict(self) -> dict:
        duration = None
        if self.started_at and self.finished_at:
            duration = int((self.finished_at - self.started_at).total_seconds())
        return {
            "id":          self.id,
            "mode":        self.mode or "",
            "started_at":  self.started_at.strftime("%Y-%m-%d %H:%M") if self.started_at else "",
            "finished_at": self.finished_at.strftime("%Y-%m-%d %H:%M") if self.finished_at else "",
            "job_count":   self.job_count or 0,
            "status":      self.status or "done",
            "duration_s":  duration,
        }


class SearchMode(db.Model):
    __tablename__ = "search_modes"
    __table_args__ = (
        db.UniqueConstraint("user_id", "name", name="uq_user_mode"),
    )

    id         = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id    = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)
    name       = db.Column(db.String(64), nullable=False)
    config     = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=_now)

    user = db.relationship("User", back_populates="modes")
