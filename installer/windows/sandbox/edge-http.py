"""One HTTP request to the installed AnyAiCam VMS for the functional Sandbox scenarios (Blue, 2026-10-21).
Test tooling only: never packaged. Standard library only (runs on the bundled embedded Python).

Windows PowerShell 5.1 cannot keep a session cookie across a 303 without following it, and strips embedded double
quotes from native arguments, so the functional harness makes its edge requests through this helper:

    python edge-http.py --jar C:\\F\\owner.cookies METHOD /path [--json-file body.json] [--out file] [--header K=V]

* Never follows a redirect (the sign-in answer is 303 + Set-Cookie); the cookie jar file is loaded and saved.
* Prints ONE JSON line: {"status", "location", "content_type", "length", "body"} -- body is the parsed JSON, or the
  first 4000 characters of text, or null when --out saved the bytes to a file.
"""
import argparse
import http.cookiejar
import json
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('method')
    parser.add_argument('path')
    parser.add_argument('--base', default='http://127.0.0.1:8000')
    parser.add_argument('--jar')
    parser.add_argument('--json-file')
    parser.add_argument('--out')
    parser.add_argument('--header', action='append', default=[])
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    jar = http.cookiejar.LWPCookieJar(args.jar) if args.jar else http.cookiejar.CookieJar()
    if args.jar:
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (FileNotFoundError, http.cookiejar.LoadError):
            pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect())
    data = None
    headers = {}
    if args.json_file:
        with open(args.json_file, 'rb') as handle:
            data = handle.read()
        headers['Content-Type'] = 'application/json'
    for item in args.header:
        key, _, value = item.partition('=')
        headers[key] = value
    request = urllib.request.Request(args.base + args.path, data=data, method=args.method.upper(), headers=headers)
    try:
        response = opener.open(request, timeout=args.timeout)
        status, reply = response.status, response
    except urllib.error.HTTPError as error:
        status, reply = error.code, error
    except (urllib.error.URLError, OSError) as error:
        print(json.dumps({'status': 0, 'error': str(error)}))
        return 0
    payload = reply.read()
    content_type = reply.headers.get('Content-Type', '')
    result = {'status': status, 'location': reply.headers.get('Location'), 'content_type': content_type,
              'length': len(payload), 'body': None}
    if args.out:
        with open(args.out, 'wb') as handle:
            handle.write(payload)
    elif 'json' in content_type:
        try:
            result['body'] = json.loads(payload.decode('utf-8'))
        except ValueError:
            result['body'] = payload[:4000].decode('utf-8', 'replace')
    else:
        result['body'] = payload[:4000].decode('utf-8', 'replace')
    if args.jar:
        jar.save(ignore_discard=True, ignore_expires=True)
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
