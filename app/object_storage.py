import hashlib
import mimetypes
import os
from abc import ABC,abstractmethod
from pathlib import Path,PurePosixPath

from cloud_config import settings

ALLOWED_CATEGORIES={'snapshots','thumbnails','clips','documents','partner-materials'}


def safe_key(category: str,key: str) -> str:
    if category not in ALLOWED_CATEGORIES: raise ValueError('Unsupported storage category.')
    path=PurePosixPath(key)
    if path.is_absolute() or '..' in path.parts: raise ValueError('Unsafe storage key.')
    return str(PurePosixPath(category)/path)


class StorageBackend(ABC):
    @abstractmethod
    def put(self,category,key,data,content_type=None): ...
    @abstractmethod
    def get(self,category,key): ...
    @abstractmethod
    def delete(self,category,key): ...
    @abstractmethod
    def url(self,category,key,expires_seconds=900): ...


class LocalStorage(StorageBackend):
    def __init__(self,root=None): self.root=Path(root or settings.local_storage_root); self.root.mkdir(parents=True,exist_ok=True)
    def path(self,category,key):
        destination=(self.root/safe_key(category,key)).resolve()
        if self.root.resolve() not in destination.parents: raise ValueError('Unsafe local storage path.')
        return destination
    def put(self,category,key,data,content_type=None):
        destination=self.path(category,key); destination.parent.mkdir(parents=True,exist_ok=True); temporary=destination.with_suffix(destination.suffix+'.tmp'); temporary.write_bytes(data); temporary.replace(destination); return {'key':safe_key(category,key),'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'backend':'local'}
    def get(self,category,key): return self.path(category,key).read_bytes()
    def delete(self,category,key): self.path(category,key).unlink(missing_ok=True)
    def url(self,category,key,expires_seconds=900): return '/storage/'+safe_key(category,key)


class S3Storage(StorageBackend):
    def __init__(self):
        if settings.storage_backend!='s3': raise RuntimeError('S3 storage is disabled.')
        if not settings.s3_endpoint or not settings.s3_bucket: raise RuntimeError('S3 storage credentials/configuration are incomplete.')
        try: import boto3
        except ImportError as error: raise RuntimeError('Install boto3 to enable S3-compatible storage.') from error
        self.bucket=settings.s3_bucket; self.client=boto3.client('s3',endpoint_url=settings.s3_endpoint,region_name=settings.s3_region)
    def put(self,category,key,data,content_type=None):
        object_key=safe_key(category,key); self.client.put_object(Bucket=self.bucket,Key=object_key,Body=data,ContentType=content_type or mimetypes.guess_type(key)[0] or 'application/octet-stream'); return {'key':object_key,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'backend':'s3'}
    def get(self,category,key): return self.client.get_object(Bucket=self.bucket,Key=safe_key(category,key))['Body'].read()
    def delete(self,category,key): self.client.delete_object(Bucket=self.bucket,Key=safe_key(category,key))
    def url(self,category,key,expires_seconds=900): return self.client.generate_presigned_url('get_object',Params={'Bucket':self.bucket,'Key':safe_key(category,key)},ExpiresIn=expires_seconds)


def get_storage(): return S3Storage() if settings.storage_backend=='s3' else LocalStorage()
