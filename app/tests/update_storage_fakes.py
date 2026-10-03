"""A fake S3 client for Software Update storage tests (no network): stores
objects in memory and returns presigned-style https URLs."""


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        self.calls.append(("put_object", Bucket, Key))
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get_object", Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise KeyError(Key)
        data = self.objects[(Bucket, Key)]

        class _Body:
            def read(self_inner):
                return data
        return {"Body": _Body()}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.calls.append(("presign", Params["Bucket"], Params["Key"], ExpiresIn))
        return f"https://{Params['Bucket']}.s3.amazonaws.com/{Params['Key']}?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=fake"


def use_fake_s3_update_storage(monkeypatch, bucket="anyaicam2026"):
    """ANYAICAM_UPDATE_STORAGE_BACKEND=s3 with an in-memory client."""
    import updates_storage
    client = FakeS3Client()
    monkeypatch.setenv("ANYAICAM_UPDATE_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("ANYAICAM_UPDATE_S3_BUCKET", bucket)
    original = updates_storage.UpdateS3Storage

    def make(*, client_override=None):
        return original(client=client)
    monkeypatch.setattr(updates_storage, "UpdateS3Storage", lambda: make())
    return client
