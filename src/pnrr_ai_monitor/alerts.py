from __future__ import annotations

import html as _html
import smtplib
from email.message import EmailMessage

from .config import Settings
from .models import Alert

_CONFIDENCE_IT = {"high": "alta", "medium": "media", "low": "bassa"}
_CONFIDENCE_COLOR = {
    "high": ("#e1f5ee", "#0f6e56"),
    "medium": ("#faeeda", "#854f0b"),
    "low": ("#f1efe8", "#5f5e5a"),
}
_UNKNOWN_DEADLINE = {"", "unknown", "sconosciuto", "non specificata", "non specificato"}


def _has_deadline(deadline: str) -> bool:
    return deadline.strip().lower() not in _UNKNOWN_DEADLINE


class AlertService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build_message(self, alerts: list[Alert]) -> EmailMessage:
        """Render ONE Italian multipart email for a group of notices that share a
        school/CUP. One notice -> a single-item email; several -> all listed
        together so one project never produces a flood of separate emails.
        Pure (no network) so it can be tested and previewed."""
        project = alerts[0].candidate.project
        n = len(alerts)

        subject = f"Opportunità formatore IA PNRR: {project.school_name}"
        if n > 1:
            subject += f" — {n} avvisi"
        elif _has_deadline(alerts[0].verification.deadline):
            subject += f" (scade {alerts[0].verification.deadline})"

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.settings.sender_email
        if self.settings.receiver_emails:
            message["To"] = ", ".join(self.settings.receiver_emails)
        if self.settings.cc_emails:
            message["Cc"] = ", ".join(self.settings.cc_emails)
        if self.settings.bcc_emails:
            message["Bcc"] = ", ".join(self.settings.bcc_emails)

        message.set_content(self._plain_body(project, alerts))
        message.add_alternative(self._html_body(project, alerts), subtype="html")
        return message

    @staticmethod
    def _plain_body(project, alerts: list[Alert]) -> str:
        header = (
            f"Nuova opportunità di formazione PNRR sull'intelligenza artificiale confermata.\n\n"
            f"Scuola: {project.school_name}\n"
            f"Codice meccanografico: {project.school_code}\n"
            f"Regione: {project.region}\n"
            f"CUP: {project.cup}\n"
            f"CLP: {project.clp}\n"
            f"Importo: {project.amount}\n\n"
            f"Avvisi trovati ({len(alerts)}):\n"
        )
        blocks = []
        for i, alert in enumerate(alerts, 1):
            c, v = alert.candidate, alert.verification
            conf = _CONFIDENCE_IT.get(v.confidence.value, v.confidence.value)
            blocks.append(
                f"\n{i}. {c.title}\n"
                f"   Link: {c.url}\n"
                f"   Tipo: {v.opportunity_type} | Scadenza: {v.deadline} | "
                f"Affidabilità: {conf} | Verifica IA: {'sì' if v.ai_used else 'no'}\n"
                f"   Perché: {v.reason}\n"
            )
        return header + "".join(blocks)

    def _html_body(self, project, alerts: list[Alert]) -> str:
        esc = _html.escape
        rows = "".join(
            f'<tr><td style="color:#666;padding:5px 0;width:150px">{label}</td>'
            f'<td style="color:#111;padding:5px 0{mono}">{esc(value)}</td></tr>'
            for label, value, mono in [
                ("Scuola", project.school_name, ""),
                ("Codice / Regione", f"{project.school_code} · {project.region}", ""),
                ("CUP", project.cup, ";font-family:monospace"),
                ("CLP", project.clp, ";font-family:monospace"),
                ("Importo", f"€ {project.amount}", ""),
            ]
        )
        notices = "".join(self._notice_html(a) for a in alerts)
        return f"""<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;border:1px solid #e5e5e5;border-radius:12px;overflow:hidden">
  <div style="padding:16px 20px;border-bottom:1px solid #eee">
    <span style="display:inline-block;background:#e6f1fb;color:#185fa5;font-size:12px;padding:3px 10px;border-radius:999px">{len(alerts)} avviso/i per questo progetto (CUP {esc(project.cup)})</span>
  </div>
  <div style="padding:20px">
    <table style="width:100%;font-size:14px;border-collapse:collapse">{rows}</table>
    {notices}
  </div>
  <div style="padding:12px 20px;border-top:1px solid #eee;font-size:12px;color:#999">Monitor Opportunità PNRR IA · ricevi questa email perché l'avviso corrisponde a una scuola finanziata monitorata.</div>
</div>"""

    @staticmethod
    def _notice_html(alert: Alert) -> str:
        esc = _html.escape
        c, v = alert.candidate, alert.verification
        conf = _CONFIDENCE_IT.get(v.confidence.value, v.confidence.value)
        bg, fg = _CONFIDENCE_COLOR.get(v.confidence.value, _CONFIDENCE_COLOR["low"])
        deadline_pill = (
            f'<span style="font-size:12px;background:#faeeda;color:#854f0b;padding:3px 10px;'
            f'border-radius:999px;margin-right:6px">⏱ Scade {esc(v.deadline)}</span>'
            if _has_deadline(v.deadline) else ""
        )
        return f"""<div style="background:#f7f7f5;border-radius:8px;padding:14px 16px;margin:14px 0">
      <div style="font-size:14px;color:#111;line-height:1.6;margin-bottom:10px">{esc(c.title)}</div>
      <div style="margin-bottom:12px">{deadline_pill}<span style="font-size:12px;background:{bg};color:{fg};padding:3px 10px;border-radius:999px">affidabilità {conf}</span></div>
      <a href="{esc(c.url)}" style="display:inline-block;background:#185fa5;color:#fff;text-decoration:none;font-size:14px;padding:9px 16px;border-radius:8px">Vedi avviso &rarr;</a>
      <div style="border-left:3px solid #378add;padding:4px 0 4px 14px;margin-top:12px">
        <div style="font-size:12px;color:#999;margin-bottom:3px">Perché è stato segnalato</div>
        <div style="font-size:14px;color:#555;line-height:1.6">{esc(v.reason)}</div>
      </div>
    </div>"""

    def send(self, alerts: list[Alert], dry_run: bool = False) -> bool:
        if not alerts:
            return False
        if dry_run:
            return True
        if not self.settings.email_enabled:
            return False
        message = self.build_message(alerts)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(self.settings.sender_email, self.settings.email_password)
            smtp.send_message(message)
        return True
