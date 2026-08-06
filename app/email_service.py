import json
import smtplib
import ssl
from abc import ABC,abstractmethod
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from cloud_config import settings

EMAIL_TYPES={'invitation','password_reset','onboarding','appliance_alert','quote_delivery'}


class EmailBackend(ABC):
    @abstractmethod
    def send(self,message_type,to,subject,text,html=None,metadata=None): ...


class PreviewEmail(EmailBackend):
    def __init__(self,root=None): self.root=Path(root or settings.email_preview_dir); self.root.mkdir(parents=True,exist_ok=True)
    def send(self,message_type,to,subject,text,html=None,metadata=None):
        if message_type not in EMAIL_TYPES: raise ValueError('Unsupported email type.')
        identifier=datetime.now().strftime('%Y%m%d-%H%M%S-%f'); record={'id':identifier,'type':message_type,'to':to,'from':settings.email_from,'subject':subject,'text':text,'html':html,'metadata':metadata or {},'created_at':datetime.now().isoformat(),'status':'preview'}; path=self.root/f'{identifier}.json'; path.write_text(json.dumps(record,indent=2),encoding='utf-8'); return record


class SMTPEmail(EmailBackend):
    def send(self,message_type,to,subject,text,html=None,metadata=None):
        if settings.email_backend!='smtp' or not settings.smtp_host: raise RuntimeError('SMTP email is disabled or incomplete.')
        message=EmailMessage(); message['From']=settings.email_from; message['To']=to; message['Subject']=subject; message.set_content(text)
        if html: message.add_alternative(html,subtype='html')
        context=ssl.create_default_context()
        with smtplib.SMTP(settings.smtp_host,settings.smtp_port,timeout=20) as client:
            client.starttls(context=context)
            if settings.smtp_username: client.login(settings.smtp_username,settings.smtp_password)
            client.send_message(message)
        return {'type':message_type,'to':to,'status':'sent','created_at':datetime.now().isoformat()}


def get_email_service(): return SMTPEmail() if settings.email_backend=='smtp' else PreviewEmail()
