from abc import ABC,abstractmethod

from email_service import get_email_service


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


class DisabledSMS(NotificationChannel):
    def send(self,notification,recipient): return {'channel':'sms','status':'disabled','provider':'disabled'}


CHANNELS={'in_app':InAppChannel(),'email':EmailChannel(),'web_push':WebPushPreparation(),'sms':DisabledSMS()}
