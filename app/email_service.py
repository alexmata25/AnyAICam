import json
import logging
import smtplib
import ssl
from abc import ABC,abstractmethod
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from cloud_config import settings

logger=logging.getLogger("anyaicam.email_service")

EMAIL_TYPES={
    'invitation','password_reset','onboarding','appliance_alert','quote_delivery','notification_test',
    # Provisioning Phase 6: post-purchase customer notifications -- see
    # purchase_notifications.py. Additive only; the six types above are
    # unchanged.
    'account_ready','setup_required','plan_updated','plan_cancelled','hardware_order_confirmation',
    # Provisioning Phase 8: hardware fulfillment/return lifecycle -- see
    # hardware_fulfillment.py, hardware_returns.py, purchase_
    # notifications.py. Additive only; every type above is unchanged.
    'hardware_shipped','hardware_cancellation','return_authorized','return_received','refund_processed','getting_started',
    # Account Controls: admin-initiated customer email change -- see
    # cloud_features.py's change_customer_account_email(). Additive only;
    # every type above is unchanged.
    'account_email_changed',
    # Admin portal pass (2026-09-26): Customer accounts' payment reminder,
    # previously sent through a separate raw-SMTP path in main.py.
    'payment_reminder',
    # Direct self-service signup (2026-10-02, direct_onboarding.py): the
    # confirm-your-address link, and the note sent instead when the address
    # already has an account. Additive only.
    'email_verification',
}


class EmailBackend(ABC):
    @abstractmethod
    def send(self,message_type,to,subject,text,html=None,metadata=None,images=None): ...


class PreviewEmail(EmailBackend):
    def __init__(self,root=None): self.root=Path(root or settings.email_preview_dir); self.root.mkdir(parents=True,exist_ok=True)
    def send(self,message_type,to,subject,text,html=None,metadata=None,images=None):
        if message_type not in EMAIL_TYPES: raise ValueError('Unsupported email type.')
        identifier=datetime.now().strftime('%Y%m%d-%H%M%S-%f'); record={'id':identifier,'type':message_type,'to':to,'from':settings.email_from,'subject':subject,'text':text,'html':html,'metadata':metadata or {},'images':[{'cid':cid,'bytes':len(data)} for cid,data in (images or [])],'created_at':datetime.now().isoformat(),'status':'preview'}; path=self.root/f'{identifier}.json'; path.write_text(json.dumps(record,indent=2),encoding='utf-8'); return record


class SMTPEmail(EmailBackend):
    def send(self,message_type,to,subject,text,html=None,metadata=None,images=None):
        if settings.email_backend!='smtp' or not settings.smtp_host: raise RuntimeError('SMTP email is disabled or incomplete.')
        message=EmailMessage(); message['From']=settings.email_from; message['To']=to; message['Subject']=subject; message.set_content(text)
        if html:
            message.add_alternative(html,subtype='html')
            # Inline images (2026-09-27, alert thumbnails): attached to the
            # HTML part as multipart/related, referenced as cid:<name>.
            html_part=message.get_payload()[-1]
            for cid,data in images or []:
                html_part.add_related(data,maintype='image',subtype='jpeg',cid=f'<{cid}>',filename=f'{cid}.jpg')
        context=ssl.create_default_context()
        try:
            with smtplib.SMTP(settings.smtp_host,settings.smtp_port,timeout=20) as client:
                client.starttls(context=context)
                if settings.smtp_username: client.login(settings.smtp_username,settings.smtp_password)
                client.send_message(message)
        except (smtplib.SMTPException,OSError) as error:
            # A real, previously-uncaught failure mode (found 2026-09-17
            # against real staging with a real bad Gmail app-password):
            # every caller of send() -- both password-reset routes,
            # quote delivery, invitations, appliance alerts -- assumed
            # this either succeeds or the caller's own code handles a
            # raised exception. None of them did, so a real SMTP auth/
            # connection failure crashed the whole request with an
            # unhandled 500 instead of the degraded-but-recorded outcome
            # this project's own notification_retry_worker already
            # expects and retries on (status='failed' is not a new
            # concept here -- every caller already just stores/returns
            # whatever status this method reports).
            logger.warning("email_service.send_failed type=%s to=%s error=%s",message_type,to,error)
            return {'type':message_type,'to':to,'status':'failed','error':str(error),'created_at':datetime.now().isoformat()}
        return {'type':message_type,'to':to,'status':'sent','created_at':datetime.now().isoformat()}


def get_email_service(): return SMTPEmail() if settings.email_backend=='smtp' else PreviewEmail()


def email_error_fields(message) -> dict:
    """{'error': <reason>} for a failed send, else {} (2026-09-27). The
    reason is the provider's own reply -- an SMTP status code and server
    text such as (535, b'5.7.8 Username and Password not accepted') --
    never a credential. Stored with every email record so a delivery
    outage is diagnosable from the product, not only from container logs."""
    if isinstance(message, dict) and message.get('status') == 'failed' and message.get('error'):
        return {'error': str(message['error'])[:300]}
    return {}
