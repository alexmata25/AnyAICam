from abc import ABC,abstractmethod

from email_service import get_email_service
from sms_service import get_sms_service


class NotificationChannel(ABC):
    @abstractmethod
    def send(self,notification,recipient): ...


class InAppChannel(NotificationChannel):
    def send(self,notification,recipient): return {'channel':'in_app','status':'stored','provider':'local'}


class EmailChannel(NotificationChannel):
    def send(self,notification,recipient):
        message=get_email_service().send('appliance_alert',recipient,notification['title'],notification.get('message') or notification['title'],metadata={'notification_id':notification['id']}); return {'channel':'email','status':message['status'],'provider':'configured_email'}


class WebPushPreparation(NotificationChannel):
    def send(self,notification,recipient): return {'channel':'web_push','status':'prepared','provider':'not_configured'}


class SmsChannel(NotificationChannel):
    # Wires the real, already-built Twilio integration (sms_service.py)
    # -- get_sms_service() itself fails closed to Preview/Unavailable
    # until ANYAICAM_SMS_BACKEND=twilio is explicitly set with all three
    # Twilio settings present, so an unconfigured deployment behaves
    # exactly as before (never claims a real send). This class used to
    # be DisabledSMS, hardcoding status='disabled' unconditionally --
    # notification_settings_page.py's own "Test SMS" button already
    # called get_sms_service() directly and worked correctly; only the
    # real, event-triggered fanout path here never did.
    def send(self,notification,recipient):
        result=get_sms_service().send('appliance_alert',recipient,notification.get('message') or notification['title'])
        return {'channel':'sms','status':result['status'],'provider':'twilio' if result['status']=='sent' else 'configured_sms','error':result.get('detail')}


CHANNELS={'in_app':InAppChannel(),'email':EmailChannel(),'web_push':WebPushPreparation(),'sms':SmsChannel()}
