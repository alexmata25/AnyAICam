"""Purpose-specific stream selection from independently probed profiles."""


def select_stream_profile(profiles, purpose):
    """Select a profile without conflating recording and browser needs.

    Browser/WebRTC prefers a *verified* H.264 substream, then a verified
    H.264 main stream. Local live view uses that same compatibility order
    to reduce bandwidth. Recording prefers the main profile regardless of
    codec, preserving source quality. Unknown/unverified metadata never
    overrides the configured legacy ONVIF endpoint.
    """
    profiles = list(profiles or [])
    def observed_h264(profile):
        return str(profile.get('observed_codec') or '').lower() == 'h264'
    def role(profile):
        return str(profile.get('stream_role') or '').lower()
    if purpose in ('browser_webrtc', 'remote_webrtc', 'local_live'):
        verified = [item for item in profiles if observed_h264(item)]
        return (next((item for item in verified if role(item) == 'substream'), None)
                or next((item for item in verified if role(item) == 'main'), None)
                or next(iter(verified), None))
    if purpose == 'recording':
        return next((item for item in profiles if role(item) == 'main'), None)
    raise ValueError('Unsupported camera stream purpose.')
