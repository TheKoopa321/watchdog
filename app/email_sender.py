from __future__ import annotations

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import EmailChannel


def _html_alert_down(
    check_name: str,
    error: str | None,
    consecutive_failures: int,
    down_since: datetime | None,
    triggered_at: datetime,
) -> str:
    down_since_str = down_since.strftime("%Y-%m-%d %H:%M:%S UTC") if down_since else "—"
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:#fee2e2;border-left:4px solid #dc2626;padding:16px;border-radius:4px">
    <h2 style="margin:0 0 8px;color:#991b1b">🔴 Service DOWN — {check_name}</h2>
    <p style="margin:4px 0"><strong>Erreur :</strong> {error or "inconnu"}</p>
    <p style="margin:4px 0"><strong>Down depuis :</strong> {down_since_str}</p>
    <p style="margin:4px 0"><strong>Échecs consécutifs :</strong> {consecutive_failures}</p>
    <p style="margin:4px 0;color:#6b7280;font-size:12px">Alerte générée le {triggered_at.strftime("%Y-%m-%d %H:%M:%S UTC")}</p>
  </div>
</body></html>
"""


def _html_alert_recovered(
    check_name: str,
    down_since: datetime | None,
    recovered_at: datetime,
) -> str:
    if down_since:
        delta = recovered_at - down_since
        total_secs = int(delta.total_seconds())
        hours, rem = divmod(total_secs, 3600)
        mins, secs = divmod(rem, 60)
        downtime = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
    else:
        downtime = "—"
    return f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:#dcfce7;border-left:4px solid #16a34a;padding:16px;border-radius:4px">
    <h2 style="margin:0 0 8px;color:#15803d">🟢 Service RECOVERED — {check_name}</h2>
    <p style="margin:4px 0"><strong>Durée de l'interruption :</strong> {downtime}</p>
    <p style="margin:4px 0;color:#6b7280;font-size:12px">Rétabli le {recovered_at.strftime("%Y-%m-%d %H:%M:%S UTC")}</p>
  </div>
</body></html>
"""


def _html_daily_summary(
    checks_summary: list[dict],
    generated_at: datetime,
) -> str:
    rows = ""
    for c in checks_summary:
        status_icon = "🟢" if c["status"] == "up" else ("🔴" if c["status"] == "down" else "🟡")
        uptime = f"{c.get('uptime_24h', 0):.1f}%" if c.get("uptime_24h") is not None else "—"
        rows += f"<tr><td>{status_icon} {c['name']}</td><td>{uptime}</td><td>{c.get('error') or '—'}</td></tr>"

    return f"""
<html><body style="font-family:sans-serif;max-width:700px;margin:0 auto;padding:20px">
  <h2 style="color:#1e40af">📊 Watchdog — Résumé quotidien</h2>
  <p style="color:#6b7280">Généré le {generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")}</p>
  <table style="width:100%;border-collapse:collapse;margin-top:16px">
    <thead>
      <tr style="background:#f1f5f9">
        <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Service</th>
        <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Uptime 24h</th>
        <th style="padding:8px;text-align:left;border-bottom:2px solid #e2e8f0">Dernière erreur</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</body></html>
"""


def send_email(
    cfg: EmailChannel,
    subject: str,
    html_body: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{cfg.from_name} <{cfg.from_email}>"
    msg["To"] = cfg.to
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as server:
        server.ehlo()
        if cfg.smtp_starttls:
            server.starttls()
            server.ehlo()
        if cfg.smtp_user and cfg.smtp_password:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.sendmail(cfg.from_email, [cfg.to], msg.as_string())


def send_alert_down(
    cfg: EmailChannel,
    check_name: str,
    error: str | None,
    consecutive_failures: int,
    down_since: datetime | None,
) -> None:
    now = datetime.utcnow()
    send_email(
        cfg,
        subject=f"[Watchdog] 🔴 DOWN — {check_name}",
        html_body=_html_alert_down(check_name, error, consecutive_failures, down_since, now),
    )


def send_alert_recovered(
    cfg: EmailChannel,
    check_name: str,
    down_since: datetime | None,
) -> None:
    now = datetime.utcnow()
    send_email(
        cfg,
        subject=f"[Watchdog] 🟢 RECOVERED — {check_name}",
        html_body=_html_alert_recovered(check_name, down_since, now),
    )


def send_daily_summary(
    cfg: EmailChannel,
    checks_summary: list[dict],
) -> None:
    now = datetime.utcnow()
    send_email(
        cfg,
        subject=f"[Watchdog] 📊 Résumé quotidien — {now.strftime('%Y-%m-%d')}",
        html_body=_html_daily_summary(checks_summary, now),
    )
